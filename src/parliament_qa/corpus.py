"""Resumable full-text corpus pipeline for Indian parliamentary Q&A records.

The source PDF is always authoritative.  This module stores immutable source
versions and page text separately from normalized full text and cleaned,
structured sections.  Every derived value therefore remains traceable to a
document version and page range.
"""
from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from .adapters import ls, rs
from .core import ALLOWED_HOSTS, HEADERS, file_hash, header_question_numbers, normalize, now

DEFAULT_FROM = "1999-01-01"
DEFAULT_TO = "2026-10-30"
SCHEMA_VERSION = 1
QUESTION_TYPES = {"STARRED", "UNSTARRED", "SHORT NOTICE", "POSTPONED", "UNKNOWN"}


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def corpus_open(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "corpus.sqlite3", timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript("""
    PRAGMA foreign_keys=ON;
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS corpus_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stage_runs(
      stage TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT, completed_at TEXT,
      cursor TEXT, detail TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS questions(
      corpus_id TEXT PRIMARY KEY, house TEXT NOT NULL CHECK(house IN ('LS','RS')),
      source_id TEXT, identity_key TEXT NOT NULL UNIQUE, term TEXT, session TEXT,
      answer_date TEXT NOT NULL, question_number TEXT NOT NULL, question_type TEXT NOT NULL,
      title TEXT, members TEXT, ministry TEXT, official_url TEXT,
      source_question_text TEXT NOT NULL DEFAULT '', source_answer_text TEXT NOT NULL DEFAULT '',
      metadata_json TEXT NOT NULL DEFAULT '{}', discovered_at TEXT NOT NULL,
      publication_status TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS documents(
      document_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES questions(corpus_id),
      source_url TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'unknown', name TEXT,
      repository_checksum TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
      UNIQUE(corpus_id,source_url)
    );
    CREATE TABLE IF NOT EXISTS source_requests(
      request_id TEXT PRIMARY KEY, stage TEXT NOT NULL, url TEXT NOT NULL, retrieved_at TEXT,
      http_status INTEGER, sha256 TEXT, raw_path TEXT, state TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS document_versions(
      version_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(document_id),
      sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
      retrieved_at TEXT NOT NULL, local_path TEXT NOT NULL, source_request_id TEXT,
      is_current INTEGER NOT NULL DEFAULT 1, UNIQUE(document_id,sha256)
    );
    CREATE TABLE IF NOT EXISTS pages(
      page_id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES document_versions(version_id),
      page_number INTEGER NOT NULL, raw_text TEXT NOT NULL, normalized_text TEXT NOT NULL,
      extraction_method TEXT NOT NULL, char_count INTEGER NOT NULL,
      confidence REAL NOT NULL, needs_review INTEGER NOT NULL,
      UNIQUE(version_id,page_number)
    );
    CREATE TABLE IF NOT EXISTS page_text_variants(
      variant_id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES pages(page_id),
      language TEXT NOT NULL, raw_text TEXT NOT NULL, normalized_text TEXT NOT NULL,
      method TEXT NOT NULL, confidence REAL NOT NULL, needs_review INTEGER NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(page_id,language,method)
    );
    CREATE TABLE IF NOT EXISTS sections(
      section_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES questions(corpus_id),
      version_id TEXT REFERENCES document_versions(version_id), section_type TEXT NOT NULL,
      sequence INTEGER NOT NULL, start_page INTEGER, end_page INTEGER, text TEXT NOT NULL,
      confidence REAL NOT NULL, method TEXT NOT NULL, review_status TEXT NOT NULL,
      UNIQUE(corpus_id,version_id,section_type,sequence)
    );
    CREATE TABLE IF NOT EXISTS annexures(
      annexure_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES questions(corpus_id),
      section_id TEXT REFERENCES sections(section_id), label TEXT, start_page INTEGER,
      end_page INTEGER, text TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS corrections(
      correction_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES questions(corpus_id),
      document_id TEXT REFERENCES documents(document_id), correction_date TEXT,
      correction_type TEXT NOT NULL DEFAULT 'correction', text TEXT NOT NULL DEFAULT '',
      supersedes_version_id TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS supplementary_links(
      link_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES questions(corpus_id),
      source_url TEXT NOT NULL, debate_date TEXT, page_start INTEGER, page_end INTEGER,
      note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS coverage(
      coverage_id TEXT PRIMARY KEY, house TEXT NOT NULL, term TEXT NOT NULL DEFAULT '',
      session TEXT NOT NULL DEFAULT '', sitting_date TEXT NOT NULL DEFAULT '',
      expected_count INTEGER, discovered_count INTEGER NOT NULL DEFAULT 0,
      downloaded_count INTEGER NOT NULL DEFAULT 0, extracted_count INTEGER NOT NULL DEFAULT 0,
      verified_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
      source_url TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
      UNIQUE(house,term,session,sitting_date)
    );
    CREATE TABLE IF NOT EXISTS issues(
      issue_id TEXT PRIMARY KEY, corpus_id TEXT, version_id TEXT, kind TEXT NOT NULL,
      severity TEXT NOT NULL, detail TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
      created_at TEXT NOT NULL, resolved_at TEXT
    );
    CREATE TABLE IF NOT EXISTS reviews(
      review_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES questions(corpus_id),
      version_id TEXT, reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
      superseded_values_json TEXT NOT NULL DEFAULT '{}', reviewed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_questions_lookup ON questions(house,answer_date,session,question_type,question_number);
    CREATE INDEX IF NOT EXISTS idx_documents_question ON documents(corpus_id,state);
    CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id,is_current);
    CREATE INDEX IF NOT EXISTS idx_pages_version ON pages(version_id,page_number);
    CREATE INDEX IF NOT EXISTS idx_page_variants_page ON page_text_variants(page_id,language);
    CREATE INDEX IF NOT EXISTS idx_sections_question ON sections(corpus_id,section_type,sequence);
    """)
    try:
        con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS question_fts USING fts5(
          corpus_id UNINDEXED, house UNINDEXED, answer_date UNINDEXED, session UNINDEXED,
          question_type UNINDEXED, language UNINDEXED, title, members, ministry,
          question_text, answer_text, annexure_text, correction_text, full_document_text,
          tokenize='unicode61 remove_diacritics 2'
        )""")
        con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
          corpus_id UNINDEXED, version_id UNINDEXED, page_number UNINDEXED,
          language UNINDEXED, text, tokenize='unicode61 remove_diacritics 2'
        )""")
    except sqlite3.OperationalError as exc:
        raise RuntimeError("SQLite was built without FTS5 support") from exc
    con.execute("INSERT OR REPLACE INTO corpus_meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    con.execute("INSERT OR IGNORE INTO corpus_meta VALUES('date_from',?)", (DEFAULT_FROM,))
    con.execute("INSERT OR IGNORE INTO corpus_meta VALUES('date_to',?)", (DEFAULT_TO,))
    con.commit()
    return con


def stage_start(con, stage: str):
    con.execute("""INSERT INTO stage_runs(stage,state,started_at,completed_at,cursor,detail)
                   VALUES(?,'running',?,NULL,NULL,'')
                   ON CONFLICT(stage) DO UPDATE SET state='running',started_at=excluded.started_at,
                   completed_at=NULL,detail=''""", (stage, now()))
    con.commit()


def stage_finish(con, stage: str, detail: dict):
    con.execute("UPDATE stage_runs SET state='complete',completed_at=?,detail=? WHERE stage=?",
                (now(), json_text(detail), stage))
    con.commit()


def normalized_type(value: str) -> str:
    text = re.sub(r"[_-]+", " ", str(value or "").upper()).strip()
    if text in {"US", "UNSTARRED QUESTION"}:
        return "UNSTARRED"
    if text in {"S", "STARRED QUESTION"}:
        return "STARRED"
    if "SHORT" in text and "NOTICE" in text:
        return "SHORT NOTICE"
    if "POSTPON" in text:
        return "POSTPONED"
    return text if text in QUESTION_TYPES else "UNKNOWN"


def iso_date(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value[:11], fmt).date().isoformat()
        except ValueError:
            pass
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


def stable_identity(house: str, source_id: str, term: str, session: str, day: str,
                    qtype: str, number: str) -> tuple[str, str]:
    house = house.upper()
    if source_id:
        identity = f"{house}:source:{source_id.strip()}"
    else:
        identity = "|".join((house, term.strip(), session.strip(), day, qtype, number.strip()))
        identity = f"{house}:composite:" + hashlib.sha256(identity.encode()).hexdigest()
    corpus_id = house + "-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    return corpus_id, identity


ALIASES = {
    "house": ("house", "House"), "source_id": ("source_id", "item_uuid", "qslno"),
    "term": ("term", "lok_sabha", "Lok Sabha"), "session": ("session", "Session"),
    "date": ("date", "answer_date", "Date"),
    "question_number": ("question_number", "Question No.", "qno"),
    "question_type": ("question_type", "Type", "type"),
    "title": ("title", "subject", "Subject"), "members": ("members", "member", "Member"),
    "ministry": ("ministry", "Ministry"),
    "official_url": ("official_url", "official_record_api_url", "question_permalink",
                     "permanent_url", "Question URL"),
    "source_url": ("source_url", "pdf_source_url", "url"),
    "local_path": ("local_path", "pdf_local_path", "path"),
    "sha256": ("sha256", "pdf_sha256"), "repository_checksum": ("repository_checksum",),
    "language": ("language", "item_language"), "name": ("name", "filename", "pdf_filename"),
    "question_text": ("question_text", "qn_text"), "answer_text": ("answer_text", "ans_text"),
}


def first(row: dict, key: str) -> str:
    for alias in ALIASES[key]:
        value = row.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def upsert_question(con, row: dict, default_house: str = "") -> str | None:
    house = (first(row, "house") or default_house).upper().replace("LOK SABHA", "LS").replace("RAJYA SABHA", "RS")
    if house not in {"LS", "RS"}:
        return None
    day = iso_date(first(row, "date"))
    number = first(row, "question_number")
    if not day or not number or not (DEFAULT_FROM <= day <= DEFAULT_TO):
        return None
    source_id, term, session = first(row, "source_id"), first(row, "term"), first(row, "session")
    raw_type = first(row, "question_type")
    if not raw_type:
        filename_hint = first(row, "name") or first(row, "local_path")
        match = re.search(r"(?:^|[_-])(UNSTARRED|STARRED|SHORT[_-]?NOTICE|POSTPONED)(?:[_-]|$)", filename_hint, re.I)
        raw_type = match.group(1).replace("_", " ") if match else ""
    qtype = normalized_type(raw_type)
    corpus_id, identity = stable_identity(house, source_id, term, session, day, qtype, number)
    same = con.execute("""SELECT corpus_id,identity_key,source_id FROM questions
      WHERE house=? AND answer_date=? AND question_number=? AND question_type=?
      ORDER BY CASE WHEN source_id<>'' THEN 0 ELSE 1 END LIMIT 1""",
      (house,day,number,qtype)).fetchone()
    if same and (not same["source_id"] or not source_id or same["source_id"] == source_id):
        # Preserve an already-issued corpus ID when a fallback record is later
        # enriched with its official source identifier.
        corpus_id, identity = same["corpus_id"], same["identity_key"]
    metadata = {k: v for k, v in row.items() if v not in (None, "")}
    con.execute("""INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(corpus_id) DO UPDATE SET
       term=COALESCE(NULLIF(excluded.term,''),questions.term),
       session=COALESCE(NULLIF(excluded.session,''),questions.session),
       title=COALESCE(NULLIF(excluded.title,''),questions.title),
       members=COALESCE(NULLIF(excluded.members,''),questions.members),
       ministry=COALESCE(NULLIF(excluded.ministry,''),questions.ministry),
       source_id=COALESCE(NULLIF(excluded.source_id,''),questions.source_id),
       official_url=COALESCE(NULLIF(excluded.official_url,''),questions.official_url),
       source_question_text=COALESCE(NULLIF(excluded.source_question_text,''),questions.source_question_text),
       source_answer_text=COALESCE(NULLIF(excluded.source_answer_text,''),questions.source_answer_text),
       metadata_json=excluded.metadata_json""",
      (corpus_id,house,source_id,identity,term,session,day,number,qtype,first(row,"title"),
       first(row,"members"),first(row,"ministry"),first(row,"official_url"),
       first(row,"question_text"),first(row,"answer_text"),json_text(metadata),now(),"pending"))
    source_url = first(row, "source_url")
    local_path = first(row, "local_path")
    if source_url or local_path:
        add_document(con, corpus_id, source_url or ("file:" + str(Path(local_path).resolve())),
                     first(row,"language") or "unknown", first(row,"name") or Path(local_path).name,
                     first(row,"repository_checksum"), local_path, first(row,"sha256"))
    return corpus_id


def add_document(con, corpus_id: str, url: str, language: str, name: str,
                 repository_checksum: str = "", local_path: str = "", supplied_sha: str = "") -> str:
    raw_language=str(language or "").strip().casefold()
    if re.search(r"(?:^|[_\-.])(?:hin|hindi)(?:[_\-.]|$)",name or url,re.I):
        language="hi"
    elif raw_language in {"hi","hin","hindi"}:
        language="hi"
    elif raw_language in {"en","eng","english","original","unknown",""}:
        language="en"
    document_id = "D-" + hashlib.sha256((corpus_id + "|" + url).encode()).hexdigest()[:24]
    con.execute("""INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                   language=excluded.language,name=excluded.name,
                   repository_checksum=excluded.repository_checksum""",
                (document_id,corpus_id,url,language or "unknown",name,repository_checksum,
                 "available" if local_path else "pending",""))
    if re.search(r"\b(?:corrigendum|correction|corrected|revised)[ _-]*(?:answer)?\b", name or url, re.I):
        correction_id = "X-" + hashlib.sha256(document_id.encode()).hexdigest()[:24]
        con.execute("""INSERT OR IGNORE INTO corrections
                       (correction_id,corpus_id,document_id,correction_type,metadata_json)
                       VALUES(?,?,?,'correction',?)""",
                    (correction_id,corpus_id,document_id,json_text({"source_url":url,"name":name})))
    if local_path:
        path = Path(local_path)
        if path.is_file():
            digest = file_hash(path)
            if supplied_sha and supplied_sha.lower() != digest.lower():
                add_issue(con, corpus_id, "", "seed_checksum_mismatch", "high", str(path))
            register_version(con, document_id, path, digest, "seed")
        else:
            add_issue(con, corpus_id, "", "seed_file_missing", "high", local_path)
    return document_id


def register_version(con, document_id: str, path: Path, digest: str, request_id: str = "") -> str:
    version_id = "V-" + hashlib.sha256((document_id + "|" + digest).encode()).hexdigest()[:24]
    with path.open("rb") as stream:
        prefix = stream.read(16)
    mime = "application/pdf" if prefix.lstrip().startswith(b"%PDF-") else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    con.execute("UPDATE document_versions SET is_current=0 WHERE document_id=?", (document_id,))
    con.execute("""INSERT OR IGNORE INTO document_versions VALUES(?,?,?,?,?,?,?,?,1)""",
                (version_id,document_id,digest,mime,path.stat().st_size,now(),str(path.resolve()),request_id))
    con.execute("UPDATE document_versions SET is_current=1 WHERE version_id=?", (version_id,))
    con.execute("UPDATE documents SET state='downloaded',error='' WHERE document_id=?", (document_id,))
    return version_id


def add_issue(con, corpus_id: str, version_id: str, kind: str, severity: str, detail: str):
    key = "I-" + hashlib.sha256((corpus_id+"|"+version_id+"|"+kind+"|"+detail).encode()).hexdigest()[:24]
    con.execute("INSERT OR IGNORE INTO issues VALUES(?,?,?,?,?,?,?,?,NULL)",
                (key,corpus_id or None,version_id or None,kind,severity,detail,"open",now()))


def _store_seed_file(root: Path, manifest: Path, local: str, storage: str) -> tuple[Path, str] | None:
    supplied=Path(local)
    candidates=[supplied] if supplied.is_absolute() else [
        (manifest.parent/supplied).resolve(),
        (Path.cwd()/supplied).resolve(),
    ]
    source=next((candidate for candidate in candidates if candidate.is_file()),candidates[0])
    if not source.is_file(): return None
    digest=file_hash(source)
    if storage == "reference": return source,digest
    suffix=source.suffix.lower() or ".bin"
    canonical=root/"documents"/"seed"/(digest+suffix)
    canonical.parent.mkdir(parents=True,exist_ok=True)
    if not canonical.exists():
        temp=canonical.with_suffix(canonical.suffix+".part")
        if storage == "hardlink":
            try: os.link(source,temp)
            except OSError: shutil.copy2(source,temp)
        else: shutil.copy2(source,temp)
        os.replace(temp,canonical)
    return canonical,digest


def seed_manifest(root: Path, con, path: Path, default_house: str = "",
                  required_role: str = "", storage: str = "copy") -> dict:
    if storage not in {"copy","hardlink","reference"}:
        raise ValueError("seed storage must be copy, hardlink, or reference")
    total = accepted = skipped = 0
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        for row in csv.DictReader(stream):
            total += 1
            if required_role and str(row.get("corpus_role") or "").strip() != required_role:
                skipped += 1
                continue
            local = first(row, "local_path")
            if local:
                stored=_store_seed_file(root,path,local,storage)
                if stored:
                    canonical,digest=stored
                    row["local_path"] = str(canonical)
                    row["sha256"] = digest
            corpus_id=upsert_question(con,row,default_house)
            if corpus_id:
                accepted += 1
                plural_paths=[x.strip() for x in str(row.get("pdf_local_paths") or "").split(" | ") if x.strip()]
                plural_urls=[x.strip() for x in str(row.get("pdf_source_urls") or "").split(" | ")]
                plural_names=[x.strip() for x in str(row.get("pdf_filenames") or "").split(" | ")]
                plural_hashes=[x.strip() for x in str(row.get("pdf_sha256") or "").split(" | ")]
                plural_checks=[x.strip() for x in str(row.get("pdf_repository_checksums") or "").split(" | ")]
                for index,local_path in enumerate(plural_paths):
                    stored=_store_seed_file(root,path,local_path,storage)
                    if not stored:
                        add_issue(con,corpus_id,"","seed_file_missing","high",local_path)
                        continue
                    canonical,digest=stored
                    def at(values): return values[index] if index < len(values) else ""
                    url=at(plural_urls) or ("file:"+str(canonical.resolve()))
                    add_document(con,corpus_id,url,first(row,"language") or "unknown",
                                 at(plural_names) or canonical.name,at(plural_checks),
                                 str(canonical),at(plural_hashes) or digest)
    con.commit()
    return {"path": str(path), "rows": total, "accepted": accepted,
            "skipped_by_role": skipped, "required_role": required_role,
            "seed_storage":storage}


def _archive_json(root: Path, con, request_id: str, stage: str, response) -> dict:
    body = response.content
    digest = hashlib.sha256(body).hexdigest()
    rel = Path("raw") / (digest + ".json")
    dest = root / rel; dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        temp = dest.with_suffix(".part"); temp.write_bytes(body); os.replace(temp,dest)
    con.execute("INSERT OR REPLACE INTO source_requests VALUES(?,?,?,?,?,?,?,?,?)",
                (request_id,stage,response.url,now(),response.status_code,digest,str(rel),"complete",""))
    return response.json()


def official_get(root: Path, con, request_id: str, stage: str, url: str, params: dict):
    host = urlparse(url).hostname
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError("unapproved official source URL")
    response = requests.get(url,params=params,headers=HEADERS,timeout=(15,120))
    response.raise_for_status()
    return _archive_json(root,con,request_id,stage,response)


def inventory_ls(root: Path, con, start: date, end: date):
    for year in range(start.year,end.year+1):
        page = 0
        while True:
            rid = f"inventory:LS:{year}:{page}"
            prior = con.execute("SELECT state FROM source_requests WHERE request_id=?",(rid,)).fetchone()
            if prior and prior[0] == "complete":
                saved = con.execute("SELECT raw_path FROM source_requests WHERE request_id=?",(rid,)).fetchone()[0]
                data = json.loads((root/saved).read_text(encoding="utf-8"))
            else:
                data = official_get(root,con,rid,"inventory",ls.API,
                    {"scope":ls.SCOPE,"query":"*","f.dateIssued":f"{year},equals","size":100,"page":page})
            objects,total_pages = ls.page(data)
            for obj in objects:
                q=ls.question(obj)
                upsert_question(con,{**q,"date":q["date"]},"LS")
            con.commit()
            if page + 1 >= total_pages:
                break
            page += 1


def _rs_range(root: Path, con, begin: date, end: date):
    rid=f"inventory:RS:{begin}:{end}"
    if con.execute("SELECT 1 FROM source_requests WHERE request_id=? AND state='complete'",(rid,)).fetchone():
        return
    end_exclusive=end+timedelta(days=1)
    try:
        data=official_get(root,con,rid,"inventory",rs.API,{"whereclause":f"adate >= '{begin}' AND adate < '{end_exclusive}'"})
        if not isinstance(data,list): raise ValueError("RS response is not a list")
    except (requests.RequestException,ValueError,json.JSONDecodeError) as exc:
        if begin < end:
            mid=begin+(end-begin)//2; _rs_range(root,con,begin,mid); _rs_range(root,con,mid+timedelta(days=1),end); return
        con.execute("INSERT OR REPLACE INTO source_requests VALUES(?,?,?,?,?,?,?,?,?)",
                    (rid,"inventory",rs.API,now(),None,"","","failed",str(exc)))
        add_issue(con,"","","inventory_failed","high",f"RS {begin}: {exc}"); con.commit(); return
    for row in data:
        q=rs.question(row)
        cid=upsert_question(con,q,"RS")
        if cid:
            for field,lang in (("files","en"),("hindifiles","hi")):
                url=str(row.get(field) or "").strip().replace("\\","/")
                if url: add_document(con,cid,url,lang,Path(urlparse(url).path).name)
    con.commit()


def inventory(root: Path, con, manifests: list[tuple[Path,str]], network: bool = True,
              houses: tuple[str, ...] = ("LS", "RS"), seed_role: str = "",
              seed_storage: str = "copy") -> dict:
    stage_start(con,"inventory")
    seeded=[seed_manifest(root,con,path,house,seed_role,seed_storage) for path,house in manifests]
    if network and "LS" in houses:
        inventory_ls(root,con,date.fromisoformat(DEFAULT_FROM),date.fromisoformat(DEFAULT_TO))
    if network and "RS" in houses:
        for year in range(1999,2027):
            lo=date(year,1,1); hi=min(date(year,12,31),date.fromisoformat(DEFAULT_TO))
            if lo <= hi: _rs_range(root,con,lo,hi)
    refresh_coverage(con)
    detail={"questions":con.execute("SELECT count(*) FROM questions").fetchone()[0],"seeded":seeded,"network":network,"houses":list(houses)}
    stage_finish(con,"inventory",detail); return detail


def _discover_ls_documents(root: Path, con):
    rows=con.execute("""SELECT * FROM questions q WHERE house='LS'
      AND NOT EXISTS(SELECT 1 FROM documents d WHERE d.corpus_id=q.corpus_id)
      ORDER BY corpus_id""").fetchall()
    for q in rows:
        metadata=json.loads(q["metadata_json"] or "{}")
        item_url=str(metadata.get("item_api_url") or "")
        if not item_url:
            add_issue(con,q["corpus_id"],"","no_document_link","high","Lok Sabha item API URL is unavailable")
            continue
        try:
            item=official_get(root,con,"acquire:item:"+q["corpus_id"],"acquire",item_url,{})
            bundles_url=item.get("_links",{}).get("bundles",{}).get("href","")
            if not bundles_url: raise ValueError("item has no bundles link")
            bundle_page=0
            while True:
                data=official_get(root,con,f"acquire:bundles:{q['corpus_id']}:{bundle_page}","acquire",bundles_url,{"page":bundle_page,"size":100})
                bundles=data.get("_embedded",{}).get("bundles",[]); info=data.get("page",{})
                for bundle in bundles:
                    if str(bundle.get("name","")).upper() != "ORIGINAL": continue
                    bits_url=bundle.get("_links",{}).get("bitstreams",{}).get("href","")
                    bit_page=0
                    while bits_url:
                        bits=official_get(root,con,f"acquire:bits:{q['corpus_id']}:{bit_page}","acquire",bits_url,{"page":bit_page,"size":100})
                        for bit in bits.get("_embedded",{}).get("bitstreams",[]):
                            name=str(bit.get("name") or ""); url=bit.get("_links",{}).get("content",{}).get("href","")
                            if not url: continue
                            check=bit.get("checkSum") or {}; checksum=""
                            if check.get("value"): checksum=str(check.get("checkSumAlgorithm", ""))+":"+str(check["value"])
                            lang="hi" if re.search(r"(?:^|[_\-.])(?:hin|hindi)(?:[_\-.]|$)",name,re.I) else "en"
                            add_document(con,q["corpus_id"],url,lang,name,checksum)
                        bit_info=bits.get("page",{}); bit_page+=1
                        if bit_page >= int(bit_info.get("totalPages",1)): break
                bundle_page+=1
                if bundle_page >= int(info.get("totalPages",1)): break
            if not con.execute("SELECT 1 FROM documents WHERE corpus_id=?",(q["corpus_id"],)).fetchone():
                add_issue(con,q["corpus_id"],"","no_document_link","high","No original Lok Sabha bitstream found")
        except (requests.RequestException,ValueError,KeyError,json.JSONDecodeError) as exc:
            add_issue(con,q["corpus_id"],"","document_discovery_failed","high",str(exc))
        con.commit()


def acquire(root: Path, con, refresh: bool = False) -> dict:
    stage_start(con,"acquire"); downloaded=failed=0
    _discover_ls_documents(root,con)
    states = "('pending','failed','downloaded')" if refresh else "('pending','failed')"
    for row in con.execute(f"SELECT * FROM documents WHERE state IN {states} ORDER BY document_id").fetchall():
        if row["source_url"].startswith("file:"):
            continue
        rid="acquire:"+row["document_id"]
        try:
            source_host=urlparse(row["source_url"]).hostname
            if urlparse(row["source_url"]).scheme != "https" or source_host not in ALLOWED_HOSTS:
                raise ValueError("unapproved official source URL")
            response=requests.get(row["source_url"],headers=HEADERS,timeout=(15,120),stream=True)
            response.raise_for_status()
            if urlparse(response.url).hostname not in ALLOWED_HOSTS:
                raise ValueError("document redirected to an unapproved host")
            suffix=Path(urlparse(row["name"] or row["source_url"]).path).suffix.lower() or ".bin"
            staging=root/"documents"/"staging"/(row["document_id"]+suffix); staging.parent.mkdir(parents=True,exist_ok=True)
            temp=staging.with_suffix(staging.suffix+".part"); digest=hashlib.sha256(); size=0
            with temp.open("wb") as out:
                for block in response.iter_content(1024*1024):
                    if block: out.write(block); digest.update(block); size+=len(block)
            if not size: raise ValueError("empty response")
            with temp.open("rb") as stream: prefix=stream.read(16)
            if prefix.lstrip().startswith(b"%PDF-"):
                if size < 100:
                    raise ValueError(f"truncated PDF response ({size} bytes)")
                suffix=".pdf"
            dest=root/"documents"/"versions"/(digest.hexdigest()+suffix); dest.parent.mkdir(parents=True,exist_ok=True)
            os.replace(temp,dest)
            con.execute("INSERT OR REPLACE INTO source_requests VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid,"acquire",response.url,now(),response.status_code,digest.hexdigest(),str(dest.relative_to(root)),"complete",""))
            register_version(con,row["document_id"],dest,digest.hexdigest(),rid); downloaded+=1
        except (requests.RequestException,OSError,ValueError) as exc:
            con.execute("UPDATE documents SET state='failed',error=? WHERE document_id=?",(str(exc),row["document_id"]))
            add_issue(con,row["corpus_id"],"","download_failed","high",str(exc)); failed+=1
        con.commit()
    refresh_coverage(con)
    detail={"downloaded":downloaded,"failed":failed,"refresh":refresh}; stage_finish(con,"acquire",detail); return detail


def _pdftotext(path: Path) -> list[str]:
    proc=subprocess.run(["pdftotext","-layout",str(path),"-"],capture_output=True,timeout=180)
    if proc.returncode: raise ValueError(proc.stderr.decode(errors="replace")[:500] or "pdftotext failed")
    pages=proc.stdout.decode("utf-8",errors="replace").split("\f")
    if pages and not pages[-1].strip(): pages.pop()
    return pages


def _ocr_page(path: Path, page_number: int, language: str = "eng") -> str:
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        raise FileNotFoundError("pdftoppm or tesseract unavailable")
    with tempfile.TemporaryDirectory() as tempdir:
        prefix=Path(tempdir)/"page"
        render=subprocess.run(["pdftoppm","-f",str(page_number),"-singlefile","-r","300","-png",str(path),str(prefix)],capture_output=True,timeout=180)
        if render.returncode: raise ValueError("pdftoppm failed")
        ocr=subprocess.run(["tesseract",str(prefix)+".png","stdout","-l",language,"--psm","1"],capture_output=True,timeout=240)
        if ocr.returncode: raise ValueError("tesseract failed")
        return ocr.stdout.decode("utf-8",errors="replace")


def normalize_page(text: str) -> str:
    text=text.replace("\r\n","\n").replace("\r","\n").replace("\u00ad","")
    text=re.sub(r"(?<=\w)-\n(?=[a-z])","",text)
    lines=[re.sub(r"[ \t]+"," ",line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _extract_pdf_task(row: dict, use_ocr: bool) -> dict:
    try:
        texts=_pdftotext(Path(row["local_path"]))
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        return {"row":row,"pages":[],"issues":[("extraction_failed","high",str(exc))]}
    if not texts:
        return {"row":row,"pages":[],"issues":[("empty_document","high","PDF contains no pages")]}
    pages=[]; issues=[]
    for number,raw in enumerate(texts,1):
        visible=len(re.sub(r"\s","",raw)); method="embedded"; confidence=.99
        if visible < 40:
            if use_ocr:
                try: raw=_ocr_page(Path(row["local_path"]),number); method="ocr_eng"; confidence=.75
                except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
                    method="no_text"; confidence=0
                    issues.append(("ocr_required","high",f"page {number}: {exc}"))
            else:
                method="no_text"; confidence=0
                issues.append(("ocr_required","high",f"page {number}"))
        normalized=normalize_page(raw); needs=int(method != "embedded" or len(normalized)<40)
        pages.append((number,raw,normalized,method,len(normalized),confidence,needs))
    return {"row":row,"pages":pages,"issues":issues}


def _tesseract_languages() -> set[str]:
    try:
        proc=subprocess.run(["tesseract","--list-langs"],capture_output=True,text=True,timeout=30)
    except (OSError,subprocess.TimeoutExpired):
        return set()
    return {line.strip() for line in (proc.stdout+"\n"+proc.stderr).splitlines()
            if re.fullmatch(r"[a-zA-Z0-9_+.-]+",line.strip())}


def _hindi_ocr_task(row: dict) -> dict:
    try:
        raw=_ocr_page(Path(row["local_path"]),row["page_number"],"hin+eng")
        normalized=normalize_page(raw)
        devanagari=len(re.findall(r"[\u0900-\u097f]",normalized))
        confidence=.9 if devanagari >= 20 else (.65 if devanagari else .35)
        return {"row":row,"raw":raw,"normalized":normalized,"confidence":confidence,
                "needs_review":int(devanagari<20),"error":""}
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        return {"row":row,"error":str(exc)}


def ocr_hindi(root: Path, con, workers: int = 3, all_pages: bool = False) -> dict:
    if workers < 1: raise ValueError("workers must be at least 1")
    if "hin" not in _tesseract_languages():
        raise ValueError("Hindi Tesseract data is unavailable; install tesseract-ocr-hin")
    stage_start(con,"ocr-hindi")
    rows=[dict(row) for row in con.execute("""SELECT p.page_id,p.page_number,p.normalized_text,
      v.version_id,v.local_path,d.corpus_id FROM pages p
      JOIN document_versions v ON v.version_id=p.version_id
      JOIN documents d ON d.document_id=v.document_id
      WHERE d.language='hi' AND NOT EXISTS(
        SELECT 1 FROM page_text_variants x WHERE x.page_id=p.page_id
        AND x.language='hi' AND x.method='tesseract_hin_eng')
      ORDER BY p.page_id""")]
    if not all_pages:
        rows=[row for row in rows if len(re.findall(r"[\u0900-\u097f]",row["normalized_text"] or ""))<20]
    completed=created=failed=review=0

    def save(result):
        nonlocal completed,created,failed,review
        row=result["row"]; completed+=1
        if result.get("error"):
            add_issue(con,row["corpus_id"],row["version_id"],"hindi_ocr_failed","high",
                      f"page {row['page_number']}: {result['error']}"); failed+=1
        else:
            variant_id="T-"+hashlib.sha256((row["page_id"]+"|hi|tesseract_hin_eng").encode()).hexdigest()[:24]
            con.execute("INSERT OR REPLACE INTO page_text_variants VALUES(?,?,?,?,?,?,?,?,?)",
              (variant_id,row["page_id"],"hi",result["raw"],result["normalized"],
               "tesseract_hin_eng",result["confidence"],result["needs_review"],now()))
            created+=1; review+=result["needs_review"]
        con.commit()
        if completed==1 or completed%25==0 or completed==len(rows):
            print(f"Hindi OCR progress: {completed}/{len(rows)} pages",flush=True)

    if workers==1:
        for row in rows: save(_hindi_ocr_task(row))
    else:
        executor=ThreadPoolExecutor(max_workers=workers,thread_name_prefix="hindi-ocr")
        futures=[executor.submit(_hindi_ocr_task,row) for row in rows]
        try:
            for future in as_completed(futures): save(future.result())
        except KeyboardInterrupt:
            for future in futures: future.cancel()
            executor.shutdown(wait=False,cancel_futures=True); raise
        else: executor.shutdown()
    detail={"candidate_pages":len(rows),"ocr_variants_created":created,"failed":failed,
            "needs_review":review,"workers":workers,"all_pages":all_pages}
    stage_finish(con,"ocr-hindi",detail); return detail


def extract(root: Path, con, use_ocr: bool = True, workers: int = 1) -> dict:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    stage_start(con,"extract"); versions=pages_count=review=processed=0
    rows=con.execute("""SELECT v.*,d.corpus_id,d.language FROM document_versions v
                        JOIN documents d ON d.document_id=v.document_id
                        WHERE v.mime_type='application/pdf' ORDER BY v.version_id""").fetchall()
    pending=[dict(row) for row in rows if not con.execute("SELECT 1 FROM pages WHERE version_id=?",(row["version_id"],)).fetchone()]

    def save_result(result):
        nonlocal versions,pages_count,review,processed
        row=result["row"]
        for kind,severity,detail in result["issues"]:
            add_issue(con,row["corpus_id"],row["version_id"],kind,severity,detail)
        for number,raw,normalized,method,char_count,confidence,needs in result["pages"]:
            page_id=f"{row['version_id']}:p{number}"
            con.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?)",
                        (page_id,row["version_id"],number,raw,normalized,method,char_count,confidence,needs))
            pages_count+=1; review+=needs
        if result["pages"]: versions+=1
        processed+=1
        con.commit()
        if processed == 1 or processed % 25 == 0 or processed == len(pending):
            print(f"Extraction progress: {processed}/{len(pending)} PDF versions",flush=True)

    if workers == 1:
        for row in pending: save_result(_extract_pdf_task(row,use_ocr))
    else:
        executor=ThreadPoolExecutor(max_workers=workers,thread_name_prefix="pdf-extract")
        futures=[executor.submit(_extract_pdf_task,row,use_ocr) for row in pending]
        try:
            for future in as_completed(futures): save_result(future.result())
        except KeyboardInterrupt:
            for future in futures: future.cancel()
            executor.shutdown(wait=False,cancel_futures=True)
            raise
        else:
            executor.shutdown()
    refresh_coverage(con)
    detail={"versions":versions,"pages":pages_count,"pages_requiring_review":review,"workers":workers}; stage_finish(con,"extract",detail); return detail


def _boilerplate_clean(pages: list[str]) -> list[str]:
    if len(pages)<2: return pages
    candidates=[]
    for text in pages:
        lines=[line.strip() for line in text.splitlines() if line.strip()]
        candidates.extend(lines[:2]+lines[-2:])
    repeated={line for line,count in Counter(candidates).items() if len(line)<180 and count >= max(2,(len(pages)+1)//2)}
    return ["\n".join(line for line in text.splitlines() if line.strip() not in repeated).strip() for text in pages]


def _page_for_offset(texts: list[str], offset: int) -> int:
    cursor=0
    for number,text in enumerate(texts,1):
        cursor += len(text)+2
        if offset < cursor: return number
    return max(1,len(texts))


def _markers(text: str):
    answer=list(re.finditer(r"(?im)^\s*(?:ANSWER|THE MINISTER\b|MINISTER OF\b)",text))
    annex=list(re.finditer(r"(?im)^\s*(?:ANNEXURE|ANNEX|STATEMENT REFERRED TO)\b",text))
    return (answer[0].start() if answer else None, annex[0].start() if annex else None)


def segment(root: Path, con) -> dict:
    stage_start(con,"segment"); done=auto=flagged=0
    # Some Lok Sabha manifests label bundle membership as "Original" rather
    # than document language. Hindi versions are explicit in their filenames;
    # all remaining Q&A originals in this English-primary corpus are English.
    con.execute("UPDATE documents SET language='hi' WHERE lower(COALESCE(name,'')) LIKE '%hindi%'")
    con.execute("""UPDATE documents SET language='en'
                   WHERE lower(COALESCE(language,'')) IN ('','en','eng','english','original','unknown')
                   AND lower(COALESCE(name,'')) NOT LIKE '%hindi%'""")
    con.commit()
    rows=con.execute("""SELECT q.*,v.version_id,d.document_id,d.name document_name FROM questions q JOIN documents d ON d.corpus_id=q.corpus_id
      JOIN document_versions v ON v.document_id=d.document_id AND v.is_current=1
      WHERE d.language IN ('en','eng','english','unknown') ORDER BY q.corpus_id,v.version_id""").fetchall()
    seen=set()
    for row in rows:
        key=(row["corpus_id"],row["version_id"])
        if key in seen or con.execute("SELECT 1 FROM sections WHERE corpus_id=? AND version_id=?",key).fetchone(): continue
        seen.add(key)
        page_rows=con.execute("SELECT * FROM pages WHERE version_id=? ORDER BY page_number",(row["version_id"],)).fetchall()
        if not page_rows: continue
        texts=_boilerplate_clean([p["normalized_text"] for p in page_rows]); full="\n\f\n".join(texts)
        a,n=_markers(full); boundaries=[]
        correction = bool(re.search(r"\b(?:corrigendum|correction|corrected|revised)[ _-]*(?:answer)?\b",row["document_name"] or "",re.I))
        if correction:
            boundaries.append(("correction",full,1,len(texts),.85,"correction_document"))
            boundaries.append(("full_document",full,1,len(texts),.99,"page_join"))
            con.execute("UPDATE corrections SET text=? WHERE document_id=?",(full,row["document_id"]))
        elif row["source_question_text"] or row["source_answer_text"]:
            # API question/answer text belongs to the parliamentary question,
            # not to each English PDF version. Insert it once below.
            boundaries.append(("full_document",full,1,len(texts),.99,"page_join"))
        else:
            answer_end=n if n is not None and (a is None or n>a) else len(full)
            if a is not None:
                boundaries.append(("question",full[:a].strip(),1,_page_for_offset(texts,a),.9,"deterministic_markers"))
                boundaries.append(("answer",full[a:answer_end].strip(),_page_for_offset(texts,a),_page_for_offset(texts,answer_end),.9,"deterministic_markers"))
            if n is not None:
                boundaries.append(("annexure",full[n:].strip(),_page_for_offset(texts,n),len(texts),.85,"deterministic_markers"))
            boundaries.append(("full_document",full,1,len(texts),.99,"page_join"))
        nums=header_question_numbers(full[:2500]); identity_ok=(not nums or row["question_number"] in nums)
        born=all(p["extraction_method"]=="embedded" and not p["needs_review"] for p in page_rows)
        types={b[0] for b in boundaries}
        if row["source_question_text"]: types.add("question")
        if row["source_answer_text"]: types.add("answer")
        approved=born and identity_ok and {"question","answer"}.issubset(types)
        status="auto_approved" if approved else "needs_review"
        for kind,text in (("question",row["source_question_text"]),("answer",row["source_answer_text"])):
            if text:
                existing=con.execute("""SELECT section_id FROM sections WHERE corpus_id=?
                                           AND version_id IS NULL AND section_type=?
                                           AND method='official_api'""",(row["corpus_id"],kind)).fetchone()
                if existing:
                    con.execute("UPDATE sections SET review_status=? WHERE section_id=?",(status,existing[0]))
                else:
                    sid="S-"+hashlib.sha256((row["corpus_id"]+"official_api"+kind).encode()).hexdigest()[:24]
                    con.execute("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (sid,row["corpus_id"],None,kind,1,None,None,text,.98,"official_api",status))
        for sequence,(kind,text,start_page,end_page,confidence,method) in enumerate(boundaries,1):
            sid="S-"+hashlib.sha256((row["corpus_id"]+row["version_id"]+kind+str(sequence)).encode()).hexdigest()[:24]
            con.execute("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid,row["corpus_id"],row["version_id"],kind,sequence,start_page,end_page,text,confidence,method,status))
            if kind=="annexure":
                aid="A-"+hashlib.sha256(sid.encode()).hexdigest()[:24]
                con.execute("INSERT OR IGNORE INTO annexures VALUES(?,?,?,?,?,?,?)",(aid,row["corpus_id"],sid,"ANNEXURE",start_page,end_page,text))
        if approved: auto+=1
        else:
            flagged+=1; add_issue(con,row["corpus_id"],row["version_id"],"manual_review_required","medium","OCR, identity, or section-boundary confidence did not meet automatic approval gate")
        con.execute("UPDATE questions SET publication_status=? WHERE corpus_id=?",(status,row["corpus_id"])); done+=1; con.commit()
    # Official API question/answer text remains useful evidence even when the
    # original document is unavailable, HTML-only, or corrupt.  Keep those
    # sections searchable while leaving the question pending for document QA.
    for row in con.execute("SELECT corpus_id,source_question_text,source_answer_text,publication_status FROM questions").fetchall():
        for kind,text in (("question",row["source_question_text"]),("answer",row["source_answer_text"])):
            if not text or con.execute("""SELECT 1 FROM sections WHERE corpus_id=? AND version_id IS NULL
                                           AND section_type=? AND method='official_api'""",
                                       (row["corpus_id"],kind)).fetchone():
                continue
            sid="S-"+hashlib.sha256((row["corpus_id"]+"official_api"+kind).encode()).hexdigest()[:24]
            con.execute("INSERT INTO sections VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid,row["corpus_id"],None,kind,1,None,None,text,.98,"official_api",row["publication_status"]))
    # Reconcile at question level after every version is considered. One clean
    # version must not hide an unresolved issue on another official version.
    con.execute("""UPDATE questions SET publication_status='needs_review'
      WHERE publication_status<>'review_approved' AND EXISTS(
        SELECT 1 FROM issues i WHERE i.corpus_id=questions.corpus_id
        AND i.kind='manual_review_required' AND i.state='open')""")
    con.execute("""UPDATE questions SET publication_status='auto_approved'
      WHERE publication_status NOT IN ('review_approved','needs_review')
      AND EXISTS(SELECT 1 FROM sections s WHERE s.corpus_id=questions.corpus_id AND s.section_type='question')
      AND EXISTS(SELECT 1 FROM sections s WHERE s.corpus_id=questions.corpus_id AND s.section_type='answer')
      AND EXISTS(SELECT 1 FROM sections s WHERE s.corpus_id=questions.corpus_id AND s.section_type='full_document')""")
    con.commit()
    question_status=dict(con.execute("SELECT publication_status,count(*) FROM questions GROUP BY publication_status"))
    detail={"segmented":done,"auto_approved_versions":auto,"versions_needing_review":flagged,
            "question_status":question_status}; stage_finish(con,"segment",detail); return detail


def build_index(con) -> dict:
    stage_start(con,"index"); con.execute("DELETE FROM question_fts"); con.execute("DELETE FROM page_fts")
    query="""SELECT q.*,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='question'),'') question_text,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='answer'),'') answer_text,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='annexure'),'') annexure_text,
      COALESCE((SELECT group_concat(text,'\n') FROM corrections WHERE corpus_id=q.corpus_id),'') correction_text,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='full_document'),'') full_document_text,
      COALESCE((SELECT language FROM documents WHERE corpus_id=q.corpus_id ORDER BY CASE WHEN language='en' THEN 0 ELSE 1 END LIMIT 1),'unknown') language
      FROM questions q ORDER BY q.corpus_id"""
    count=0
    for row in con.execute(query):
        con.execute("INSERT INTO question_fts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (row["corpus_id"],row["house"],row["answer_date"],row["session"],row["question_type"],row["language"],
           row["title"],row["members"],row["ministry"],row["question_text"],row["answer_text"],row["annexure_text"],row["correction_text"],row["full_document_text"]))
        count+=1
    page_count=0
    for row in con.execute("""SELECT d.corpus_id,p.version_id,p.page_number,d.language,
      p.normalized_text,COALESCE(group_concat(x.normalized_text,'\n'),'') variant_text
      FROM pages p JOIN document_versions v ON v.version_id=p.version_id
      JOIN documents d ON d.document_id=v.document_id
      LEFT JOIN page_text_variants x ON x.page_id=p.page_id
      GROUP BY p.page_id ORDER BY d.corpus_id,p.version_id,p.page_number"""):
        text=(row["normalized_text"]+"\n"+row["variant_text"]).strip()
        con.execute("INSERT INTO page_fts VALUES(?,?,?,?,?)",
                    (row["corpus_id"],row["version_id"],row["page_number"],row["language"],text))
        page_count+=1
    con.commit(); detail={"indexed":count,"pages_indexed":page_count}; stage_finish(con,"index",detail); return detail


def refresh_coverage(con):
    keys=con.execute("SELECT house,term,session,answer_date,count(*) n FROM questions GROUP BY house,term,session,answer_date").fetchall()
    for row in keys:
        cov="C-"+hashlib.sha256("|".join(str(row[x] or "") for x in ("house","term","session","answer_date")).encode()).hexdigest()[:24]
        downloaded=con.execute("""SELECT count(DISTINCT q.corpus_id) FROM questions q JOIN documents d ON d.corpus_id=q.corpus_id
          WHERE q.house=? AND q.term IS ? AND q.session IS ? AND q.answer_date=? AND d.state='downloaded'""",(row["house"],row["term"],row["session"],row["answer_date"])).fetchone()[0]
        extracted=con.execute("""SELECT count(DISTINCT q.corpus_id) FROM questions q JOIN documents d ON d.corpus_id=q.corpus_id
          JOIN document_versions v ON v.document_id=d.document_id JOIN pages p ON p.version_id=v.version_id
          WHERE q.house=? AND q.term IS ? AND q.session IS ? AND q.answer_date=?""",(row["house"],row["term"],row["session"],row["answer_date"])).fetchone()[0]
        verified=con.execute("""SELECT count(*) FROM questions WHERE house=? AND term IS ? AND session IS ? AND answer_date=? AND publication_status='auto_approved'""",(row["house"],row["term"],row["session"],row["answer_date"])).fetchone()[0]
        con.execute("""INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(coverage_id) DO UPDATE SET discovered_count=excluded.discovered_count,
          downloaded_count=excluded.downloaded_count,extracted_count=excluded.extracted_count,
          verified_count=excluded.verified_count,status=excluded.status""",
          (cov,row["house"],row["term"] or "",row["session"] or "",row["answer_date"],None,row["n"],downloaded,extracted,verified,
           "enumerated_not_reconciled","","Expected official count not yet supplied; absence is not zero."))
    con.commit()


def audit(con) -> dict:
    stage_start(con,"audit"); refresh_coverage(con)
    checks={
      "questions": "SELECT count(*) FROM questions",
      "documents": "SELECT count(*) FROM documents",
      "download_failures": "SELECT count(*) FROM documents WHERE state='failed'",
      "versions_without_pages": "SELECT count(*) FROM document_versions v WHERE mime_type='application/pdf' AND NOT EXISTS(SELECT 1 FROM pages p WHERE p.version_id=v.version_id)",
      "questions_without_documents": "SELECT count(*) FROM questions q WHERE NOT EXISTS(SELECT 1 FROM documents d WHERE d.corpus_id=q.corpus_id)",
      "open_issues": "SELECT count(*) FROM issues WHERE state='open'",
      "publication_ready": "SELECT count(*) FROM questions WHERE publication_status IN ('auto_approved','review_approved')",
    }
    detail={key:con.execute(sql).fetchone()[0] for key,sql in checks.items()}
    detail["acceptance_passed"]=(detail["download_failures"]==0 and detail["versions_without_pages"]==0
                                 and detail["questions_without_documents"]==0 and detail["open_issues"]==0)
    stage_finish(con,"audit",detail); return detail


def _write_csv(path: Path, fields: list[str], rows):
    temp=path.with_suffix(path.suffix+".part")
    with temp.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore"); writer.writeheader()
        for row in rows: writer.writerow(dict(row))
    os.replace(temp,path)


def _write_jsonl(path: Path, rows):
    temp=path.with_suffix(path.suffix+".part")
    with temp.open("w",encoding="utf-8") as stream:
        for row in rows: stream.write(json.dumps(dict(row),ensure_ascii=False,sort_keys=True)+"\n")
    os.replace(temp,path)


def export_corpus(root: Path, con) -> dict:
    stage_start(con,"export"); out=root/"exports"; out.mkdir(exist_ok=True)
    records=[dict(row) for row in con.execute("""SELECT q.*,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='question'),'') cleaned_question,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='answer'),'') cleaned_answer,
      COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='annexure'),'') annexure_text
      FROM questions q ORDER BY house,answer_date,session,question_number,corpus_id""")]
    # Stable, unsurprising names for downstream research tools.  Retain the
    # cleaned_* names for backwards compatibility and audit transparency.
    for record in records:
        record["question_text"] = record["cleaned_question"] or record["source_question_text"]
        record["answer_text"] = record["cleaned_answer"] or record["source_answer_text"]
    _write_jsonl(out/"questions.jsonl",records)
    fields=[d[0] for d in con.execute("SELECT * FROM questions LIMIT 0").description]+["cleaned_question","cleaned_answer","question_text","answer_text","annexure_text"]
    _write_csv(out/"questions.csv",fields,records)
    page_rows=list(con.execute("""SELECT d.corpus_id,p.version_id,p.page_number,p.raw_text,p.normalized_text,p.extraction_method,p.confidence,p.needs_review
      FROM pages p JOIN document_versions v ON v.version_id=p.version_id JOIN documents d ON d.document_id=v.document_id
      ORDER BY d.corpus_id,p.version_id,p.page_number"""))
    _write_jsonl(out/"pages.jsonl",page_rows)
    variant_rows=list(con.execute("""SELECT d.corpus_id,p.version_id,p.page_number,x.language,
      x.raw_text,x.normalized_text,x.method,x.confidence,x.needs_review,x.created_at
      FROM page_text_variants x JOIN pages p ON p.page_id=x.page_id
      JOIN document_versions v ON v.version_id=p.version_id
      JOIN documents d ON d.document_id=v.document_id
      ORDER BY d.corpus_id,p.version_id,p.page_number,x.language,x.method"""))
    _write_jsonl(out/"page_text_variants.jsonl",variant_rows)
    _write_csv(out/"document_versions.csv",[d[0] for d in con.execute("SELECT * FROM document_versions LIMIT 0").description],con.execute("SELECT * FROM document_versions ORDER BY document_id,version_id"))
    _write_csv(out/"documents.csv",[d[0] for d in con.execute("SELECT * FROM documents LIMIT 0").description],con.execute("SELECT * FROM documents ORDER BY corpus_id,document_id"))
    _write_csv(out/"corrections.csv",[d[0] for d in con.execute("SELECT * FROM corrections LIMIT 0").description],con.execute("SELECT * FROM corrections ORDER BY corpus_id,correction_id"))
    _write_csv(out/"supplementary_links.csv",[d[0] for d in con.execute("SELECT * FROM supplementary_links LIMIT 0").description],con.execute("SELECT * FROM supplementary_links ORDER BY corpus_id,link_id"))
    _write_csv(out/"coverage.csv",[d[0] for d in con.execute("SELECT * FROM coverage LIMIT 0").description],con.execute("SELECT * FROM coverage ORDER BY house,sitting_date,session"))
    _write_csv(out/"issues.csv",[d[0] for d in con.execute("SELECT * FROM issues LIMIT 0").description],con.execute("SELECT * FROM issues ORDER BY state,severity,issue_id"))
    parquet="unavailable"
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist([dict(r) for r in records]),out/"questions.parquet",compression="zstd")
        parquet="questions.parquet"
    except (ImportError,ValueError) as exc:
        add_issue(con,"","","parquet_export_unavailable","medium",str(exc)); con.commit()
    manifest={"schema_version":SCHEMA_VERSION,"snapshot":{"from":DEFAULT_FROM,"to":DEFAULT_TO},
      "exported_at":now(),"counts":{"questions":len(records),"pages":len(page_rows),
      "page_text_variants":len(variant_rows)},"parquet":parquet,
      "rights":"Internal research corpus. Redistribution of source documents or extracted text requires a separate rights review."}
    temp=out/"manifest.json.part"; temp.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n",encoding="utf-8"); os.replace(temp,out/"manifest.json")
    stage_finish(con,"export",manifest); return manifest


def _voyant_filename(row: dict) -> str:
    house = row["house"]
    day = row["answer_date"]
    qtype = re.sub(r"[^A-Z0-9]+", "_", row["question_type"].upper()).strip("_") or "UNKNOWN"
    number = re.sub(r"[^A-Za-z0-9]+", "_", str(row["question_number"])).strip("_") or "UNKNOWN"
    suffix = row["corpus_id"].split("-", 1)[-1][:8]
    return f"{house}_{day}_{qtype}_Q{number}_{suffix}.txt"


def export_voyant(output_root: Path, corpus_roots: list[Path]) -> dict:
    """Create combined UTF-8 Voyant collections from one or more corpus DBs.

    Only structured question/answer sections are used for the corresponding
    collections.  A complete file falls back to immutable full-document text
    when deterministic separation is unavailable; metadata makes that fallback
    explicit so it cannot be mistaken for a validated Q&A split.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    collections = ("questions_only", "answers_only", "complete_qa")
    for collection in collections:
        for house_dir in ("lok_sabha", "rajya_sabha"):
            (output_root/collection/house_dir).mkdir(parents=True, exist_ok=True)

    metadata = []
    counts = Counter()
    seen_ids = set()
    for corpus_root in corpus_roots:
        db_path = corpus_root/"corpus.sqlite3"
        if not db_path.is_file():
            raise ValueError(f"corpus database does not exist: {db_path}")
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("""SELECT q.*,
          COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='question'),'') question_text,
          COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='answer'),'') answer_text,
          COALESCE((SELECT group_concat(text,'\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='annexure'),'') annexure_text,
          COALESCE((SELECT group_concat(text,'\n\f\n') FROM sections WHERE corpus_id=q.corpus_id AND section_type='full_document'),'') full_document_text
          FROM questions q ORDER BY house,answer_date,session,question_number,corpus_id""").fetchall()
        for source_row in rows:
            row = dict(source_row)
            if row["corpus_id"] in seen_ids:
                continue
            seen_ids.add(row["corpus_id"])
            question = row["question_text"].strip() or row["source_question_text"].strip()
            answer = row["answer_text"].strip() or row["source_answer_text"].strip()
            annexure = row["annexure_text"].strip()
            full_document = row["full_document_text"].strip()
            filename = _voyant_filename(row)
            house_dir = "lok_sabha" if row["house"] == "LS" else "rajya_sabha"
            paths = {key: "" for key in collections}

            if question:
                rel = Path("questions_only")/house_dir/filename
                (output_root/rel).write_text(question.rstrip()+"\n", encoding="utf-8")
                paths["questions_only"] = rel.as_posix(); counts["question_files"] += 1
            if answer:
                rel = Path("answers_only")/house_dir/filename
                (output_root/rel).write_text(answer.rstrip()+"\n", encoding="utf-8")
                paths["answers_only"] = rel.as_posix(); counts["answer_files"] += 1

            complete_parts = []
            if question:
                complete_parts.append("QUESTION\n\n"+question)
            if answer:
                complete_parts.append("ANSWER\n\n"+answer)
            if annexure:
                complete_parts.append("ANNEXURE\n\n"+annexure)
            complete_basis = "structured_qa"
            if not complete_parts and full_document:
                complete_parts = [full_document]
                complete_basis = "full_document_fallback_unseparated"
            if complete_parts:
                rel = Path("complete_qa")/house_dir/filename
                (output_root/rel).write_text("\n\n".join(complete_parts).rstrip()+"\n", encoding="utf-8")
                paths["complete_qa"] = rel.as_posix(); counts["complete_files"] += 1
            else:
                complete_basis = "no_extracted_text"

            try:
                source_metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                source_metadata = {}
            metadata.append({
              "corpus_id":row["corpus_id"], "house":row["house"],
              "answer_date":row["answer_date"], "year":row["answer_date"][:4],
              "term":row["term"] or "", "session":row["session"] or "",
              "question_number":row["question_number"], "question_type":row["question_type"],
              "title":row["title"] or "", "members":row["members"] or "",
              "ministry":row["ministry"] or "", "publication_status":row["publication_status"],
              "has_separated_question":int(bool(question)), "has_separated_answer":int(bool(answer)),
              "complete_text_basis":complete_basis,
              "question_file":paths["questions_only"], "answer_file":paths["answers_only"],
              "complete_qa_file":paths["complete_qa"], "official_url":row["official_url"] or "",
              "collection_scope":"targeted_search_question",
              "discovery_terms":source_metadata.get("matched_terms", source_metadata.get("search_terms", "")),
            })
        con.close()

    fields = ["corpus_id","house","answer_date","year","term","session","question_number",
      "question_type","title","members","ministry","publication_status",
      "has_separated_question","has_separated_answer","complete_text_basis","question_file",
      "answer_file","complete_qa_file","official_url","collection_scope","discovery_terms"]
    _write_csv(output_root/"metadata.csv", fields, metadata)
    manifest = {
      "generated_at":now(), "scope":"targeted search collection; not a complete enumeration of all parliamentary questions",
      "questions":len(metadata), **dict(counts),
      "corpora":[str(path) for path in corpus_roots], "encoding":"UTF-8"
    }
    temp=output_root/"manifest.json.part"
    temp.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    os.replace(temp,output_root/"manifest.json")
    return manifest


def add_review(con, corpus_id: str, reviewer: str, decision: str, reason: str,
               version_id: str = "", superseded: dict | None = None):
    if decision not in {"approved","rejected","needs_revision"}: raise ValueError("invalid review decision")
    rid="R-"+hashlib.sha256((corpus_id+reviewer+decision+now()).encode()).hexdigest()[:24]
    con.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)",(rid,corpus_id,version_id or None,reviewer,decision,reason,json_text(superseded or {}),now()))
    if decision=="approved":
        con.execute("UPDATE questions SET publication_status='review_approved' WHERE corpus_id=?",(corpus_id,))
        con.execute("UPDATE issues SET state='resolved',resolved_at=? WHERE corpus_id=? AND kind='manual_review_required' AND state='open'",(now(),corpus_id))
    con.commit()
    return rid
