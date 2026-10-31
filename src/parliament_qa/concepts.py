"""Resumable exact-concept discovery and promotion for parliamentary Q&A."""
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

from .adapters import ls, rs
from .core import ALLOWED_HOSTS, HEADERS, file_hash, header_question_numbers
from .corpus import (add_document, audit, build_index, corpus_open, export_corpus,
                     export_voyant, extract, json_text, normalized_type, now,
                     segment, upsert_question)

SCHEMA_VERSION = 1


def load_concept_config(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("concepts"):
        raise ValueError("concept config must define concepts")
    for key, value in data["concepts"].items():
        if not value.get("patterns") or not value.get("queries"):
            raise ValueError(f"concept {key} requires patterns and queries")
        value["compiled_patterns"] = [re.compile(pattern, re.I) for pattern in value["patterns"]]
    return data


def normalize_concept_text(value) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<(?:br\s*/?|/p)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    # Preserve the meaningful mother-tongue compound when a PDF wraps it at
    # the hyphen; generic line-break hyphenation below joins split words.
    text = re.sub(r"(?i)\bmother-\s*\n\s*tongue", "mother tongue", text)
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def exact_hits(config: dict, fields: dict[str, str]) -> list[dict]:
    hits = []
    for concept, spec in config["concepts"].items():
        for field, raw in fields.items():
            text = normalize_concept_text(raw)
            for pattern_text, pattern in zip(spec["patterns"], spec["compiled_patterns"]):
                matches = list(pattern.finditer(text))
                if not matches:
                    continue
                match = matches[0]
                hits.append({
                  "concept":concept, "label":spec["label"], "pattern":pattern_text,
                  "observed":match.group(0), "field":field, "occurrences":len(matches),
                  "context":text[max(0,match.start()-180):match.end()+220],
                })
    return hits


def discovery_open(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root/"discovery.sqlite3", timeout=90)
    con.row_factory = sqlite3.Row
    con.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stage_runs(stage TEXT PRIMARY KEY,state TEXT NOT NULL,
      started_at TEXT,completed_at TEXT,detail TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS source_requests(
      request_id TEXT PRIMARY KEY,house TEXT NOT NULL,scope TEXT NOT NULL,
      year INTEGER,session TEXT,concept TEXT,url TEXT NOT NULL,state TEXT NOT NULL,
      http_status INTEGER,sha256 TEXT,raw_path TEXT,returned INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',retrieved_at TEXT,query_sha256 TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS sessions(
      house TEXT NOT NULL,house_number TEXT NOT NULL DEFAULT '',session TEXT NOT NULL,
      first_date TEXT,last_date TEXT,state TEXT NOT NULL DEFAULT 'pending',
      records_scanned INTEGER NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(house,house_number,session));
    CREATE TABLE IF NOT EXISTS candidates(
      candidate_id TEXT PRIMARY KEY,house TEXT NOT NULL,source_id TEXT NOT NULL DEFAULT '',
      identity_key TEXT NOT NULL,house_number TEXT NOT NULL DEFAULT '',session TEXT NOT NULL DEFAULT '',
      answer_date TEXT NOT NULL,question_number TEXT NOT NULL,question_type TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '',members TEXT NOT NULL DEFAULT '',ministry TEXT NOT NULL DEFAULT '',
      question_text TEXT NOT NULL DEFAULT '',answer_text TEXT NOT NULL DEFAULT '',
      official_url TEXT NOT NULL DEFAULT '',item_api_url TEXT NOT NULL DEFAULT '',
      document_urls_json TEXT NOT NULL DEFAULT '{}',source_channels TEXT NOT NULL DEFAULT '',
      metadata_json TEXT NOT NULL DEFAULT '{}',verification_status TEXT NOT NULL DEFAULT 'pending',
      verification_note TEXT NOT NULL DEFAULT '',promotion_status TEXT NOT NULL DEFAULT 'pending',
      UNIQUE(house,identity_key));
    CREATE TABLE IF NOT EXISTS hits(
      hit_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,concept TEXT NOT NULL,label TEXT NOT NULL,
      pattern TEXT NOT NULL,observed TEXT NOT NULL,field TEXT NOT NULL,occurrences INTEGER NOT NULL,
      context TEXT NOT NULL,source_request_id TEXT NOT NULL DEFAULT '',
      UNIQUE(candidate_id,concept,pattern,field,source_request_id));
    CREATE TABLE IF NOT EXISTS documents(
      document_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,language TEXT NOT NULL,url TEXT NOT NULL,
      local_path TEXT NOT NULL DEFAULT '',sha256 TEXT NOT NULL DEFAULT '',mime_type TEXT NOT NULL DEFAULT '',
      byte_size INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'pending',
      identity_status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',
      UNIQUE(candidate_id,language,url));
    CREATE TABLE IF NOT EXISTS backfill_queue(
      queue_id TEXT PRIMARY KEY,house TEXT NOT NULL,source_id TEXT NOT NULL,year INTEGER NOT NULL,
      session TEXT NOT NULL DEFAULT '',raw_json TEXT NOT NULL,url TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
      sha256 TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS coverage(
      house TEXT NOT NULL,year INTEGER NOT NULL,session TEXT NOT NULL DEFAULT '',concept TEXT NOT NULL,
      scope TEXT NOT NULL,status TEXT NOT NULL,queries INTEGER NOT NULL DEFAULT 0,
      returned INTEGER NOT NULL DEFAULT 0,candidates INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT '',PRIMARY KEY(house,year,session,concept,scope));
    CREATE TABLE IF NOT EXISTS issues(
      issue_id TEXT PRIMARY KEY,house TEXT,candidate_id TEXT,kind TEXT NOT NULL,severity TEXT NOT NULL,
      detail TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open',created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_candidates_lookup ON candidates(house,answer_date,session,question_number);
    CREATE INDEX IF NOT EXISTS idx_hits_candidate ON hits(candidate_id,concept);
    CREATE INDEX IF NOT EXISTS idx_documents_candidate ON documents(candidate_id,state);
    """)
    columns={row[1] for row in con.execute("PRAGMA table_info(source_requests)")}
    if "query_sha256" not in columns:
        con.execute("ALTER TABLE source_requests ADD COLUMN query_sha256 TEXT NOT NULL DEFAULT ''")
    con.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)",(str(SCHEMA_VERSION),))
    con.commit(); return con


def _stage_start(con, stage):
    con.execute("""INSERT INTO stage_runs VALUES(?,'running',?,NULL,'')
      ON CONFLICT(stage) DO UPDATE SET state='running',started_at=excluded.started_at,completed_at=NULL,detail=''""",
      (stage,now())); con.commit()


def _stage_finish(con, stage, detail):
    con.execute("UPDATE stage_runs SET state='complete',completed_at=?,detail=? WHERE stage=?",
                (now(),json_text(detail),stage)); con.commit()


def _issue(con, house, candidate_id, kind, severity, detail):
    iid="CI-"+hashlib.sha256(f"{house}|{candidate_id}|{kind}|{detail}".encode()).hexdigest()[:24]
    con.execute("INSERT OR IGNORE INTO issues VALUES(?,?,?,?,?,?,?,?)",
                (iid,house or None,candidate_id or None,kind,severity,detail,"open",now()))


def _archive_response(root: Path, response) -> tuple[str,str]:
    body=response.content; digest=hashlib.sha256(body).hexdigest()
    rel=Path("raw")/(digest+".json.gz"); dest=root/rel; dest.parent.mkdir(parents=True,exist_ok=True)
    if not dest.exists():
        descriptor,temp_name=tempfile.mkstemp(prefix=digest+"-",suffix=".part",dir=dest.parent)
        os.close(descriptor);temp=Path(temp_name)
        try:
            with gzip.open(temp,"wb") as stream: stream.write(body)
            if dest.exists():temp.unlink()
            else:os.replace(temp,dest)
        finally:
            temp.unlink(missing_ok=True)
    return digest,str(rel)


def _load_raw(root: Path, rel: str):
    with gzip.open(root/rel,"rb") as stream: return json.loads(stream.read())


def _request_json(root: Path, con, request_id: str, house: str, scope: str, url: str,
                  params: dict | None = None, year: int | None = None,
                  session: str = "", concept: str = ""):
    prior=con.execute("SELECT * FROM source_requests WHERE request_id=? AND state='complete'",(request_id,)).fetchone()
    if prior: return _load_raw(root,prior["raw_path"]),prior["url"]
    query_sha=hashlib.sha256(json_text({"url":url,"params":params or {}}).encode()).hexdigest()
    try:
        parsed=urlparse(url)
        if parsed.scheme!="https" or parsed.hostname not in ALLOWED_HOSTS: raise ValueError("unapproved official URL")
        response=requests.get(url,params=params or {},headers=HEADERS,timeout=(15,180))
        response.raise_for_status(); data=response.json(); digest,rel=_archive_response(root,response)
        returned=len(data) if isinstance(data,list) else 0
        con.execute("""INSERT OR REPLACE INTO source_requests
          (request_id,house,scope,year,session,concept,url,state,http_status,sha256,
           raw_path,returned,error,retrieved_at,query_sha256)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (request_id,house,scope,year,session,concept,response.url,"complete",response.status_code,
           digest,rel,returned,"",now(),query_sha));con.commit();return data,response.url
    except (requests.RequestException,ValueError,json.JSONDecodeError) as exc:
        con.execute("""INSERT OR REPLACE INTO source_requests
          (request_id,house,scope,year,session,concept,url,state,http_status,sha256,
           raw_path,returned,error,retrieved_at,query_sha256)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (request_id,house,scope,year,session,concept,url,"failed",None,"","",0,str(exc),now(),query_sha))
        con.commit();raise


def _date(value) -> str:
    text=str(value or "").strip()
    for fmt in ("%Y-%m-%d","%d.%m.%Y","%d/%m/%Y"):
        try:return datetime.strptime(text[:10],fmt).date().isoformat()
        except ValueError:pass
    return ""


def _number(value) -> str:
    try:return str(int(float(str(value))))
    except (TypeError,ValueError):return str(value or "").strip()


def _candidate_id(house, source_id, identity):
    return "C-"+hashlib.sha256(f"{house}|{source_id or identity}".encode()).hexdigest()[:24]


def _upsert_candidate(con, row: dict, hits: list[dict], request_id: str, channel: str) -> str:
    house=row["house"]; source_id=str(row.get("source_id") or "")
    qtype=normalized_type(row.get("question_type") or "")
    identity="|".join((house,str(row.get("house_number") or ""),str(row.get("session") or ""),
                       row["answer_date"],qtype,str(row["question_number"])))
    existing=con.execute("""SELECT candidate_id,source_channels,metadata_json FROM candidates
      WHERE house=? AND identity_key=?""",(house,identity)).fetchone()
    cid=existing["candidate_id"] if existing else _candidate_id(house,source_id,identity)
    channels=set((existing["source_channels"] if existing else "").split(" | "))-{""};channels.add(channel)
    metadata=dict(row.get("metadata") or {})
    if existing:
        try: old=json.loads(existing["metadata_json"] or "{}");old.update(metadata);metadata=old
        except json.JSONDecodeError:pass
    con.execute("""INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(candidate_id) DO UPDATE SET
      source_id=COALESCE(NULLIF(excluded.source_id,''),candidates.source_id),
      title=COALESCE(NULLIF(excluded.title,''),candidates.title),
      members=COALESCE(NULLIF(excluded.members,''),candidates.members),
      ministry=COALESCE(NULLIF(excluded.ministry,''),candidates.ministry),
      question_text=COALESCE(NULLIF(excluded.question_text,''),candidates.question_text),
      answer_text=COALESCE(NULLIF(excluded.answer_text,''),candidates.answer_text),
      official_url=COALESCE(NULLIF(excluded.official_url,''),candidates.official_url),
      item_api_url=COALESCE(NULLIF(excluded.item_api_url,''),candidates.item_api_url),
      document_urls_json=CASE WHEN excluded.document_urls_json<>'{}' THEN excluded.document_urls_json ELSE candidates.document_urls_json END,
      source_channels=excluded.source_channels,metadata_json=excluded.metadata_json""",
      (cid,house,source_id,identity,str(row.get("house_number") or ""),str(row.get("session") or ""),
       row["answer_date"],str(row["question_number"]),qtype,row.get("title") or "",row.get("members") or "",
       row.get("ministry") or "",row.get("question_text") or "",row.get("answer_text") or "",
       row.get("official_url") or "",row.get("item_api_url") or "",json_text(row.get("document_urls") or {}),
       " | ".join(sorted(channels)),json_text(metadata),"pending","","pending"))
    for hit in hits:
        hid="H-"+hashlib.sha256(f"{cid}|{hit['concept']}|{hit['pattern']}|{hit['field']}|{request_id}".encode()).hexdigest()[:24]
        con.execute("INSERT OR IGNORE INTO hits VALUES(?,?,?,?,?,?,?,?,?,?)",
          (hid,cid,hit["concept"],hit["label"],hit["pattern"],hit["observed"],hit["field"],
           hit["occurrences"],hit["context"],request_id))
    con.commit();return cid


def _ls_sessions(root,con,config,start,end):
    url=config["sources"]["lok_sabha"]["sessions"]
    data,_=_request_json(root,con,"LS:sessions","LS","sessions",url)
    sessions=[]
    for lok in data if isinstance(data,list) else []:
        for sess in lok.get("sessions",[]):
            dates=[_date(x) for x in sess.get("dates",[])];dates=[x for x in dates if x]
            if not dates or max(dates)<start or min(dates)>end:continue
            row=("LS",str(lok.get("loksabha") or ""),str(sess.get("sessionNo") or ""),min(dates),max(dates))
            con.execute("INSERT OR IGNORE INTO sessions(house,house_number,session,first_date,last_date) VALUES(?,?,?,?,?)",row)
            sessions.append(dict(house_number=row[1],session=row[2],first_date=row[3],last_date=row[4]))
    con.commit();return sessions


def _ls_rows(payload):
    if isinstance(payload,dict):return [x for x in payload.get("listOfQuestions",[]) if isinstance(x,dict)]
    if isinstance(payload,list) and payload and isinstance(payload[0],dict):
        return [x for x in payload[0].get("listOfQuestions",[]) if isinstance(x,dict)]
    return []


def _discover_ls_sessions(root,con,config,start,end):
    sessions=_ls_sessions(root,con,config,start,end);api=config["sources"]["lok_sabha"]["questions"]
    page_size=int(config["policy"].get("page_size",1000));summary=Counter()

    def scan(sess):
        worker_con=discovery_open(root)
        scanned=page=0;state="searched";note=""
        try:
            while True:
                page+=1;rid=f"LS:session:{sess['house_number']}:{sess['session']}:{page}"
                data,url=_request_json(root,worker_con,rid,"LS","session",api,
                  {"loksabhaNo":sess["house_number"],"sessionNumber":sess["session"],"pageNo":page,
                   "pageSize":page_size,"locale":"en"},session=sess["session"])
                rows=_ls_rows(data)
                if not rows:break
                scanned+=len(rows)
                for raw in rows:
                    day=_date(raw.get("date"))
                    if not day or not(start<=day<=end):continue
                    fields={"title":raw.get("subjects"),"question":raw.get("questionText"),"answer":raw.get("answerText")}
                    hits=exact_hits(config,fields)
                    if not hits:continue
                    row={"house":"LS","source_id":f"LS{raw.get('lokNo')}-S{raw.get('sessionNo')}-{raw.get('type')}-{raw.get('quesNo')}-{day}",
                      "house_number":raw.get("lokNo"),"session":raw.get("sessionNo"),"answer_date":day,
                      "question_number":_number(raw.get("quesNo")),"question_type":raw.get("type"),
                      "title":normalize_concept_text(raw.get("subjects")),
                      "members":" | ".join(normalize_concept_text(x) for x in raw.get("member"))
                                if isinstance(raw.get("member"), list)
                                else normalize_concept_text(raw.get("member")),
                      "ministry":normalize_concept_text(raw.get("ministry")),
                      "question_text":normalize_concept_text(raw.get("questionText")),
                      "answer_text":normalize_concept_text(raw.get("answerText")),"official_url":url,
                      "document_urls":{"en":raw.get("questionsFilePath") or "","hi":raw.get("questionsFilePathHindi") or ""},
                      "metadata":{"api_request_url":url}}
                    _upsert_candidate(worker_con,row,hits,rid,"ls_structured_api")
                if len(rows)<page_size:break
                if page>100:raise ValueError("session pagination exceeded 100 pages")
        except Exception as exc:
            state="failed";note=str(exc);_issue(worker_con,"LS","","session_failed","high",f"LS{sess['house_number']} S{sess['session']}: {exc}")
        worker_con.execute("UPDATE sessions SET state=?,records_scanned=?,note=? WHERE house='LS' AND house_number=? AND session=?",
                    (state,scanned,note,sess["house_number"],sess["session"]))
        for concept in config["concepts"]:
            n=worker_con.execute("""SELECT count(DISTINCT c.candidate_id) FROM candidates c JOIN hits h ON h.candidate_id=c.candidate_id
              WHERE c.house='LS' AND c.house_number=? AND c.session=? AND h.concept=?""",
              (sess["house_number"],sess["session"],concept)).fetchone()[0]
            years=range(int(sess["first_date"][:4]),int(sess["last_date"][:4])+1)
            for year in years:
                worker_con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("LS",year,sess["session"],concept,"session",state,page,scanned,n,note))
        worker_con.commit();worker_con.close()
        return sess,state,scanned

    workers=max(1,int(config["policy"].get("workers",1)))
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="ls-concepts") as executor:
        futures=[executor.submit(scan,sess) for sess in sessions]
        for index,future in enumerate(as_completed(futures),1):
            sess,state,scanned=future.result();summary["records_scanned"]+=scanned
            print(f"LS sessions: {index}/{len(sessions)} {sess['house_number']}/{sess['session']} {state}",flush=True)
    summary["candidates"]=con.execute("SELECT count(*) FROM candidates WHERE house='LS'").fetchone()[0]
    return dict(summary|Counter(sessions=len(sessions),workers=workers))


def _ls_metadata(item,key):
    return " | ".join(str(x.get("value","")) for x in item.get("metadata",{}).get(key,[]) if x.get("value"))


def _discover_ls_elibrary(root,con,config,start,end):
    source=config["sources"]["lok_sabha"];api=source["elibrary"];scope=source["scope"];summary=Counter()
    for year in range(int(start[:4]),int(end[:4])+1):
      for concept,spec in config["concepts"].items():
        seen=set();queries=returned=0;failed=False
        for qi,query in enumerate(spec["queries"]):
          page=0
          while True:
            rid=f"LS:elibrary:{year}:{concept}:{qi}:{page}";queries+=1
            try:
              data,_=_request_json(root,con,rid,"LS","year_fallback",api,
                {"scope":scope,"query":f'"{query}"',"f.dateIssued":f"{year},equals","size":100,"page":page},
                year=year,concept=concept)
              objects,total_pages=ls.page(data);returned+=len(objects)
              for obj in objects:
                item=obj.get("_embedded",{}).get("indexableObject",{});uid=str(item.get("uuid") or "")
                day=_ls_metadata(item,"dc.date.issued")[:10]
                if not uid or not day.startswith(str(year)) or not(start<=day<=end):continue
                seen.add(uid)
                row={"house":"LS","source_id":uid,"house_number":_ls_metadata(item,"dc.identifier.loksabhanumber"),
                  "session":_ls_metadata(item,"dc.identifier.sessionnumber"),"answer_date":day,
                  "question_number":_ls_metadata(item,"dc.identifier.questionnumber"),
                  "question_type":_ls_metadata(item,"dc.identifier.questiontype"),
                  "title":_ls_metadata(item,"dc.title") or item.get("name","") ,
                  "members":_ls_metadata(item,"dc.contributor.members"),"ministry":_ls_metadata(item,"dc.relation.ministry"),
                  "official_url":_ls_metadata(item,"dc.identifier.uri") or ("https://elibrary.sansad.in/handle/"+str(item.get("handle") or "")),
                  "item_api_url":obj.get("_links",{}).get("indexableObject",{}).get("href","") ,
                  "metadata":{"elibrary_query":query,"requires_document_exact_scan":True}}
                _upsert_candidate(con,row,[],rid,"ls_elibrary_index");summary["index_candidates"]+=1
              if page+1>=total_pages:break
              page+=1
            except Exception as exc:
              failed=True;_issue(con,"LS","","elibrary_query_failed","high",f"{year} {query}: {exc}");break
        status="failed" if failed else "searched"
        con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?,?,?,?)",
          ("LS",year,"",concept,"year_fallback",status,queries,returned,len(seen),
           "Exact quoted e-Library index; candidates require document scan"))
        con.commit()
    return dict(summary)


def _rs_question(raw):
    q=rs.question(raw)
    return {"house":"RS","source_id":q["source_id"],"session":q["session"],"answer_date":q["date"],
      "question_number":q["question_number"],"question_type":q["question_type"],"title":normalize_concept_text(q["title"]),
      "members":normalize_concept_text(q["members"]),"ministry":normalize_concept_text(q["ministry"]),
      "question_text":normalize_concept_text(q["question_text"]),"answer_text":normalize_concept_text(q["answer_text"]),
      "official_url":q["official_url"],"document_urls":{"en":raw.get("files") or "","hi":raw.get("hindifiles") or ""},
      "metadata":{"legacy_api_row":raw}}


def _rs_phrase_clause(query: str) -> str:
    value=query.replace("'","''").replace("[","[[]").replace("%","[%]").replace("_","[_]")
    value="%".join(value.split())
    return "("+" OR ".join(f"{field} LIKE '%{value}%'" for field in ("qtitle","qn_text","ans_text"))+")"


def _rs_query_range(root,con,config,begin,end,concept,query,query_index,scope="year"):
    api=config["sources"]["rajya_sabha"]["questions"]
    rid=f"RS:{scope}:{begin}:{end}:{concept}:{query_index}"
    where=f"adate >= '{begin}' AND adate < '{end+timedelta(days=1)}' AND {_rs_phrase_clause(query)}"
    try:
        data,_=_request_json(root,con,rid,"RS",scope,api,{"whereclause":where},year=begin.year,concept=concept)
        if not isinstance(data,list):raise ValueError("RS response was not a list")
        if any(not(begin.isoformat()<=_date(x.get("adate") or x.get("ans_date"))<=end.isoformat()) for x in data):
            raise ValueError("RS endpoint ignored date boundaries")
    except Exception as exc:
        # Retry malformed/oversized annual responses in bounded ranges, but do
        # not recurse down to individual days after a persistent source error.
        if (end-begin).days > 31:
            mid=begin+(end-begin)//2
            return (_rs_query_range(root,con,config,begin,mid,concept,query,query_index,scope)+
                    _rs_query_range(root,con,config,mid+timedelta(days=1),end,concept,query,query_index,scope))
        _issue(con,"RS","","rs_query_failed","high",f"{begin} {concept} {query}: {exc}");return []
    for raw in data:
        row=_rs_question(raw);hits=exact_hits(config,{"title":row["title"],"question":row["question_text"],"answer":row["answer_text"]})
        hits=[h for h in hits if h["concept"]==concept]
        if hits:_upsert_candidate(con,row,hits,rid,"rs_legacy_api")
    return data


def _rs_session_rows(root,con,config,sess):
    api=config["sources"]["rajya_sabha"]["questions"]
    begin=date.fromisoformat(sess["first_date"]);end=date.fromisoformat(sess["last_date"])
    rid=f"RS:session-all:{sess['session']}:{begin}:{end}"
    where=(f"ses_no={sess['session']} AND adate >= '{begin}' "
           f"AND adate < '{end+timedelta(days=1)}'")
    try:
        data,_=_request_json(root,con,rid,"RS","session",api,{"whereclause":where},
                             session=sess["session"])
        if not isinstance(data,list):raise ValueError("RS session response was not a list")
        if any(str(x.get("ses_no"))!=sess["session"] or
               not(begin.isoformat()<=_date(x.get("adate") or x.get("ans_date"))<=end.isoformat()) for x in data):
            raise ValueError("RS session/date filter ignored")
        return data
    except Exception as exc:
        _issue(con,"RS","","session_failed","high",f"RS {sess['session']}: {exc}")
        return None


def _rs_sessions(root,con,config,start,end):
    api=config["sources"]["rajya_sabha"]["sessions"]
    data,_=_request_json(root,con,"RS:sessions","RS","sessions",api)
    source=data.get("response",data) if isinstance(data,dict) else data;sessions=[]
    for row in source if isinstance(source,list) else []:
        dates=[_date(x) for x in row.get("sittingDates",[])];dates=[x for x in dates if x]
        if not dates or max(dates)<start or min(dates)>end:continue
        session=str(row.get("session") or row.get("sessionNo") or "")
        if not session:continue
        con.execute("INSERT OR IGNORE INTO sessions(house,session,first_date,last_date) VALUES('RS',?,?,?)",
                    (session,min(dates),max(dates)))
        sessions.append({"session":session,"first_date":min(dates),"last_date":max(dates)})
    con.commit();return sessions


def _discover_rs(root,con,config,start,end):
    summary=Counter();sessions=_rs_sessions(root,con,config,start,end)
    # Failures from the superseded combined-family URL format remain in the
    # immutable request log but no longer represent the active query design.
    con.execute("UPDATE issues SET state='superseded' WHERE kind='rs_query_failed' AND state='open'")
    con.commit()
    jobs=[]
    for year in range(int(start[:4]),int(end[:4])+1):
      begin=date(year,1,1);finish=min(date(year,12,31),date.fromisoformat(end))
      for concept,spec in config["concepts"].items():
        jobs.extend((year,begin,finish,concept,query,index)
                    for index,query in enumerate(spec["queries"]))

    def run_year(job):
      year,begin,finish,concept,query,index=job;worker_con=discovery_open(root)
      rows=_rs_query_range(root,worker_con,config,begin,finish,concept,query,index)
      worker_con.close();return year,concept,len(rows)

    totals=Counter();workers=max(1,int(config["policy"].get("workers",1)))
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="rs-concepts") as executor:
      futures=[executor.submit(run_year,job) for job in jobs]
      for done,future in enumerate(as_completed(futures),1):
        year,concept,count=future.result();totals[(year,concept)]+=count
        if done==1 or done%25==0 or done==len(jobs):
          print(f"RS year/phrase queries: {done}/{len(jobs)}",flush=True)
    for year in range(int(start[:4]),int(end[:4])+1):
      for concept,spec in config["concepts"].items():
        failed=con.execute("""SELECT count(*) FROM source_requests WHERE house='RS' AND scope='year'
          AND year=? AND concept=? AND state='failed' AND request_id GLOB 'RS:year:*:*:*:[0-9]*'""",
          (year,concept)).fetchone()[0]
        found=con.execute("""SELECT count(DISTINCT c.candidate_id) FROM candidates c JOIN hits h
          ON h.candidate_id=c.candidate_id WHERE c.house='RS' AND substr(c.answer_date,1,4)=? AND h.concept=?""",
          (str(year),concept)).fetchone()[0]
        con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?,?,?,?)",
          ("RS",year,"",concept,"year","gap" if failed else "searched",len(spec["queries"]),
           totals[(year,concept)],found,"One bounded official request per exact phrase"))
    con.commit()
    # Session-level reconciliation uses the exact same families.
    for i,sess in enumerate(sessions,1):
      data=_rs_session_rows(root,con,config,sess);state="searched" if data is not None else "failed"
      scanned=len(data or []);note="" if data is not None else "Official session audit failed"
      found=defaultdict(set)
      for raw in data or []:
        row=_rs_question(raw);hits=exact_hits(config,{"title":row["title"],"question":row["question_text"],"answer":row["answer_text"]})
        if hits:
          cid=_upsert_candidate(con,row,hits,f"RS:session-all:{sess['session']}","rs_session_audit")
          for hit in hits:found[hit["concept"]].add(cid)
      for concept in config["concepts"]:
        for year in range(int(sess["first_date"][:4]),int(sess["last_date"][:4])+1):
          con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("RS",year,sess["session"],concept,"session",state,1,scanned,len(found[concept]),note))
      con.execute("UPDATE sessions SET state=?,records_scanned=?,note=? WHERE house='RS' AND session=?",(state,scanned,note,sess["session"]))
      con.commit();print(f"RS sessions: {i}/{len(sessions)} {sess['session']} {state}",flush=True)
    # Exhaustive blank-answer queue, split by month to avoid oversized responses.
    if config["policy"].get("scan_blank_answer_documents",True):
      for year in range(int(start[:4]),int(end[:4])+1):
        for month in range(1,13):
          begin=date(year,month,1)
          if begin>date.fromisoformat(end):break
          finish=(date(year+1,1,1) if month==12 else date(year,month+1,1))
          finish=min(finish,date.fromisoformat(end)+timedelta(days=1))
          rid=f"RS:blank:{year}:{month:02}";where=f"adate >= '{begin}' AND adate < '{finish}' AND ans_text IS NULL"
          try:data,_=_request_json(root,con,rid,"RS","blank_answer",api,{"whereclause":where},year=year)
          except Exception as exc:_issue(con,"RS","","blank_answer_query_failed","high",f"{year}-{month:02}: {exc}");continue
          if not isinstance(data,list):continue
          for raw in data:
            url=str(raw.get("files") or "").strip();source_id=str(raw.get("qslno") or "")
            if not url or not source_id:continue
            qid="BQ-"+hashlib.sha256(f"RS|{source_id}|{url}".encode()).hexdigest()[:24]
            con.execute("INSERT OR IGNORE INTO backfill_queue VALUES(?,?,?,?,?,?,?,?,?,?)",
              (qid,"RS",source_id,year,str(raw.get("ses_no") or ""),json_text(raw),url,"pending","",""))
          con.commit()
    return {"sessions":len(sessions),"candidates":con.execute("SELECT count(*) FROM candidates WHERE house='RS'").fetchone()[0],
            "backfill_queued":con.execute("SELECT count(*) FROM backfill_queue WHERE house='RS'").fetchone()[0]}


def export_discovery(root: Path,con) -> dict:
    out=root/"exports";out.mkdir(exist_ok=True)
    def csv_out(name,query):
        rows=con.execute(query).fetchall();fields=[x[0] for x in con.execute(query+" LIMIT 0").description]
        temp=out/(name+".part")
        with temp.open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(dict(x) for x in rows)
        os.replace(temp,out/name);return len(rows)
    counts={
      "candidates":csv_out("candidates.csv","SELECT * FROM candidates ORDER BY house,answer_date,question_number"),
      "hits":csv_out("hits.csv","SELECT * FROM hits ORDER BY candidate_id,concept,field"),
      "documents":csv_out("documents.csv","SELECT * FROM documents ORDER BY candidate_id,language,url"),
      "coverage":csv_out("coverage.csv","SELECT * FROM coverage ORDER BY house,year,session,concept,scope"),
      "sessions":csv_out("sessions.csv","SELECT * FROM sessions ORDER BY house,first_date,session"),
      "issues":csv_out("issues.csv","SELECT * FROM issues ORDER BY state,severity,kind"),
    }
    manifest={"schema_version":SCHEMA_VERSION,"exported_at":now(),"counts":counts,
      "acceptance_passed":con.execute("SELECT count(*) FROM issues WHERE state='open' AND severity='high'").fetchone()[0]==0}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    return manifest


def discover_concepts(root: Path, config_path: Path, houses=("LS","RS")) -> dict:
    root.mkdir(parents=True,exist_ok=True)
    free=shutil.disk_usage(root).free
    if free < 10 * 1024**3:
        raise ValueError(f"concept workspace requires at least 10 GiB free; found {free / 1024**3:.1f} GiB")
    config=load_concept_config(config_path);con=discovery_open(root);_stage_start(con,"discover")
    start=str(config["snapshot"]["from"]);end=str(config["snapshot"]["to"]);detail={}
    archived_config=root/"config"/config_path.name;archived_config.parent.mkdir(exist_ok=True)
    if not archived_config.exists() or file_hash(archived_config)!=file_hash(config_path):
        shutil.copy2(config_path,archived_config)
    con.execute("INSERT OR REPLACE INTO meta VALUES('config_path',?)",(str(archived_config.resolve()),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('config_sha256',?)",(file_hash(config_path),));con.commit()
    if "LS" in houses:
        detail["ls_sessions"]=_discover_ls_sessions(root,con,config,start,end)
        detail["ls_elibrary"]=_discover_ls_elibrary(root,con,config,start,end)
    if "RS" in houses:detail["rs"]=_discover_rs(root,con,config,start,end)
    detail["export"]=export_discovery(root,con);_stage_finish(con,"discover",detail);con.close();return detail


def _download(root: Path,url: str) -> tuple[Path,str,str,int]:
    parsed=urlparse(url)
    if parsed.scheme!="https" or parsed.hostname not in ALLOWED_HOSTS:raise ValueError("unapproved official document URL")
    response=requests.get(url,headers=HEADERS,timeout=(15,180),stream=True);response.raise_for_status()
    if urlparse(response.url).hostname not in ALLOWED_HOSTS:raise ValueError("unapproved redirect")
    tempdir=root/"staging";tempdir.mkdir(exist_ok=True);temp=Path(tempfile.mkstemp(prefix="doc-",dir=tempdir)[1])
    digest=hashlib.sha256();size=0
    with temp.open("wb") as stream:
      for block in response.iter_content(1024*1024):
        if block:stream.write(block);digest.update(block);size+=len(block)
    if not size:temp.unlink(missing_ok=True);raise ValueError("empty response")
    prefix=temp.read_bytes()[:16]
    if prefix.lstrip().startswith(b"%PDF-"):
        if size<100:temp.unlink(missing_ok=True);raise ValueError("truncated PDF")
        mime="application/pdf";suffix=".pdf"
    else:
        mime=response.headers.get("Content-Type","").split(";",1)[0] or mimetypes.guess_type(parsed.path)[0] or "application/octet-stream"
        suffix=Path(parsed.path).suffix.lower() or ".bin"
    dest=root/"documents"/(digest.hexdigest()+suffix);dest.parent.mkdir(exist_ok=True)
    if dest.exists():temp.unlink()
    else:os.replace(temp,dest)
    return dest,digest.hexdigest(),mime,size


def _document_text(path: Path,mime: str) -> str:
    if mime=="application/pdf":
        proc=subprocess.run(["pdftotext","-layout",str(path),"-"],capture_output=True,timeout=240)
        if proc.returncode:raise ValueError(proc.stderr.decode(errors="replace")[:300] or "pdftotext failed")
        return proc.stdout.decode("utf-8",errors="replace")
    if mime in {"text/html","text/plain"}:
        return normalize_concept_text(path.read_text(encoding="utf-8",errors="replace"))
    if mime.endswith("wordprocessingml.document") or path.suffix.lower()==".docx":
        with zipfile.ZipFile(path) as archive:return normalize_concept_text(archive.read("word/document.xml").decode(errors="replace"))
    return ""


def _resolve_ls_documents(root,con,candidate):
    if not candidate["item_api_url"]:return {}
    item,_=_request_json(root,con,"verify:item:"+candidate["candidate_id"],"LS","verify",candidate["item_api_url"])
    bundles_url=item.get("_links",{}).get("bundles",{}).get("href","");result={}
    if not bundles_url:return result
    data,_=_request_json(root,con,"verify:bundles:"+candidate["candidate_id"],"LS","verify",bundles_url,{"size":100})
    for bundle in data.get("_embedded",{}).get("bundles",[]):
      if str(bundle.get("name","")).upper()!="ORIGINAL":continue
      bits_url=bundle.get("_links",{}).get("bitstreams",{}).get("href","")
      if not bits_url:continue
      bits,_=_request_json(root,con,"verify:bits:"+candidate["candidate_id"],"LS","verify",bits_url,{"size":100})
      for bit in bits.get("_embedded",{}).get("bitstreams",[]):
        name=str(bit.get("name") or "");url=bit.get("_links",{}).get("content",{}).get("href","")
        if not url:continue
        lang="hi" if re.search(r"(?:^|[_\-.])(?:hin|hindi)(?:[_\-.]|$)",name,re.I) else "en"
        result.setdefault(lang,url)
    return result


def _verify_candidate(root,con,config,candidate) -> str:
    urls=json.loads(candidate["document_urls_json"] or "{}")
    if candidate["house"]=="LS" and candidate["item_api_url"]:
        resolved=_resolve_ls_documents(root,con,candidate);urls={**resolved,**{k:v for k,v in urls.items() if v}}
        con.execute("UPDATE candidates SET document_urls_json=? WHERE candidate_id=?",(json_text(urls),candidate["candidate_id"]))
    exact=con.execute("SELECT count(*) FROM hits WHERE candidate_id=?",(candidate["candidate_id"],)).fetchone()[0]>0
    verified_doc=False;errors=[]
    for language,url in urls.items():
      if not url:continue
      did="CD-"+hashlib.sha256(f"{candidate['candidate_id']}|{language}|{url}".encode()).hexdigest()[:24]
      prior=con.execute("SELECT * FROM documents WHERE document_id=? AND state='downloaded'",(did,)).fetchone()
      try:
        if prior:path=Path(prior["local_path"]);digest=prior["sha256"];mime=prior["mime_type"];size=prior["byte_size"]
        else:path,digest,mime,size=_download(root,url)
        identity="retained"
        if language=="en":
          text=_document_text(path,mime);doc_hits=exact_hits(config,{"full_document":text})
          for hit in doc_hits:
            hid="H-"+hashlib.sha256(f"{candidate['candidate_id']}|{hit['concept']}|{hit['pattern']}|full_document|{did}".encode()).hexdigest()[:24]
            con.execute("INSERT OR IGNORE INTO hits VALUES(?,?,?,?,?,?,?,?,?,?)",
              (hid,candidate["candidate_id"],hit["concept"],hit["label"],hit["pattern"],hit["observed"],"full_document",hit["occurrences"],hit["context"],did))
          exact=exact or bool(doc_hits)
          nums=header_question_numbers(text[:3000]);identity="verified" if (not nums or candidate["question_number"] in nums) else "conflict"
          verified_doc=verified_doc or identity=="verified"
        con.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (did,candidate["candidate_id"],language,url,str(path),digest,mime,size,"downloaded",identity,""))
      except Exception as exc:
        errors.append(f"{language}: {exc}")
        con.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (did,candidate["candidate_id"],language,url,"","","",0,"failed","pending",str(exc)))
    if not exact:status="excluded";note="No exact configured phrase in structured English text or English original"
    elif verified_doc:status="verified";note="Exact English hit and official document identity passed"
    else:status="needs_review";note="Exact hit found but no identity-verified English original; "+"; ".join(errors)
    con.execute("UPDATE candidates SET verification_status=?,verification_note=? WHERE candidate_id=?",(status,note,candidate["candidate_id"]));con.commit();return status


def _verify_backfill(root,con,config,row):
    try:
      path,digest,mime,size=_download(root,row["url"]);text=_document_text(path,mime);hits=exact_hits(config,{"full_document":text})
      if not hits:
        path.unlink(missing_ok=True);con.execute("UPDATE backfill_queue SET state='no_match',sha256=? WHERE queue_id=?",(digest,row["queue_id"]));con.commit();return "no_match"
      raw=json.loads(row["raw_json"]);candidate=_rs_question(raw);cid=_upsert_candidate(con,candidate,hits,"backfill:"+row["queue_id"],"rs_blank_answer_document")
      did="CD-"+hashlib.sha256(f"{cid}|en|{row['url']}".encode()).hexdigest()[:24]
      nums=header_question_numbers(text[:3000]);identity="verified" if (not nums or candidate["question_number"] in nums) else "conflict"
      con.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (did,cid,"en",row["url"],str(path),digest,mime,size,"downloaded",identity,""))
      con.execute("UPDATE candidates SET verification_status=?,verification_note=? WHERE candidate_id=?",
        ("verified" if identity=="verified" else "needs_review","Blank-answer document exact match",cid))
      con.execute("UPDATE backfill_queue SET state='matched',sha256=? WHERE queue_id=?",(digest,row["queue_id"]));con.commit();return "matched"
    except Exception as exc:
      con.execute("UPDATE backfill_queue SET state='failed',error=? WHERE queue_id=?",(str(exc),row["queue_id"]));con.commit();return "failed"


def verify_concepts(root: Path, workers: int = 3) -> dict:
    con=discovery_open(root);config_path=Path(con.execute("SELECT value FROM meta WHERE key='config_path'").fetchone()[0]);config=load_concept_config(config_path)
    _stage_start(con,"verify");counts=Counter()
    for candidate in con.execute("SELECT * FROM candidates WHERE verification_status='pending' ORDER BY house,answer_date,question_number").fetchall():
        counts[_verify_candidate(root,con,config,candidate)]+=1
    # Sequential DB writes keep the checkpoint safe; downloads remain resumable.
    for row in con.execute("SELECT * FROM backfill_queue WHERE state='pending' ORDER BY year,session,source_id").fetchall():
        counts["backfill_"+_verify_backfill(root,con,config,row)]+=1
    detail=dict(counts);detail["export"]=export_discovery(root,con);_stage_finish(con,"verify",detail);con.close();return detail


def promote_concepts(root: Path, ls_root: Path, rs_root: Path, voyant_root: Path,
                     profile_script: Path | None = None, workers: int = 4) -> dict:
    discovery=discovery_open(root);_stage_start(discovery,"promote");summary=Counter()
    corpora={"LS":(ls_root,corpus_open(ls_root)),"RS":(rs_root,corpus_open(rs_root))}
    for candidate in discovery.execute("SELECT * FROM candidates WHERE verification_status='verified' ORDER BY house,answer_date,question_number").fetchall():
      house=candidate["house"];corpus_root,con=corpora[house]
      hits=discovery.execute("SELECT DISTINCT concept,label FROM hits WHERE candidate_id=? ORDER BY concept",(candidate["candidate_id"],)).fetchall()
      metadata=json.loads(candidate["metadata_json"] or "{}");metadata.update({
        "matched_terms":" | ".join(x["label"].casefold() for x in hits),
        "concept_discovery_id":candidate["candidate_id"],"concept_discovery_workspace":str(root.resolve()),
        "concept_discovery_evidence":"comprehensive year/session exact-English retrieval",
      })
      row={"house":house,"source_id":candidate["source_id"],"term":candidate["house_number"],"session":candidate["session"],
        "date":candidate["answer_date"],"question_number":candidate["question_number"],"question_type":candidate["question_type"],
        "title":candidate["title"],"members":candidate["members"],"ministry":candidate["ministry"],
        "question_text":candidate["question_text"],"answer_text":candidate["answer_text"],"official_url":candidate["official_url"],**metadata}
      cid=upsert_question(con,row,house)
      # Preserve pre-existing discovery terms rather than replacing them.
      current=con.execute("SELECT metadata_json FROM questions WHERE corpus_id=?",(cid,)).fetchone()[0]
      try: merged=json.loads(current or "{}")
      except json.JSONDecodeError:merged={}
      old=[x.strip() for x in str(merged.get("matched_terms") or "").split("|") if x.strip()]
      for hit in hits:
        if hit["label"].casefold() not in old:old.append(hit["label"].casefold())
      merged.update(metadata);merged["matched_terms"]=" | ".join(old)
      con.execute("UPDATE questions SET metadata_json=? WHERE corpus_id=?",(json_text(merged),cid))
      for doc in discovery.execute("SELECT * FROM documents WHERE candidate_id=? AND state='downloaded'",(candidate["candidate_id"],)):
        add_document(con,cid,doc["url"],doc["language"],Path(doc["local_path"]).name,"",doc["local_path"],doc["sha256"])
      con.commit();discovery.execute("UPDATE candidates SET promotion_status='promoted' WHERE candidate_id=?",(candidate["candidate_id"],));discovery.commit();summary[house]+=1
    reports={}
    for house,(corpus_root,con) in corpora.items():
      reports[house]={"extract":extract(corpus_root,con,workers=workers),"segment":segment(corpus_root,con),
        "index":build_index(con),"audit":audit(con),"export":export_corpus(corpus_root,con)};con.close()
    reports["voyant"]=export_voyant(voyant_root,[ls_root,rs_root])
    if profile_script:
      proc=subprocess.run([str(profile_script.parent.parent.parent/".venv/bin/python"),str(profile_script)],capture_output=True,text=True,timeout=300)
      reports["profile"]={"returncode":proc.returncode,"stdout":proc.stdout,"stderr":proc.stderr}
    detail={"promoted":dict(summary),"reports":reports,"discovery_export":export_discovery(root,discovery)}
    _stage_finish(discovery,"promote",detail);discovery.close();return detail
