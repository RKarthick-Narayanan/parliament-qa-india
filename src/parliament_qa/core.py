"""Resumable collection, evidence storage and CSV export."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from .adapters import ls, rs
from .matching import matches, normalize

MAX_RESPONSE = 16 * 1024 * 1024
HEADERS = {"User-Agent": "ParliamentQA/0.1 (research; contact repository maintainer)",
           "Referer": "https://sansad.in/"}
ALLOWED_HOSTS = {"sansad.in", "rsdoc.nic.in", "rajyasabha.nic.in", "rsdebate.nic.in",
                 "eparlib.sansad.in", "elibrary.sansad.in", "integration.rajyasabha.digital",
                 "bucketapi.rajyasabha.digital"}


def now():
    return datetime.now(timezone.utc).isoformat()


def jsonstr(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def db_open(root):
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "checkpoint.sqlite3", timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS run(config TEXT NOT NULL, config_hash TEXT NOT NULL, started TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY, house TEXT, term TEXT, start TEXT, end TEXT, page INTEGER, state TEXT, error TEXT, url TEXT, raw_path TEXT, sha256 TEXT, retrieved TEXT, total_pages INTEGER);
    CREATE TABLE IF NOT EXISTS questions(key TEXT PRIMARY KEY, house TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS matches(question_key TEXT, term TEXT, field TEXT, method TEXT, occurrences INTEGER, context TEXT, PRIMARY KEY(question_key,term,field,method), FOREIGN KEY(question_key) REFERENCES questions(key));
    CREATE TABLE IF NOT EXISTS documents(key TEXT PRIMARY KEY, question_key TEXT NOT NULL, url TEXT NOT NULL, language TEXT, name TEXT, repository_checksum TEXT, state TEXT, path TEXT, sha256 TEXT, size INTEGER, retrieved TEXT, verification TEXT, evidence TEXT, error TEXT, FOREIGN KEY(question_key) REFERENCES questions(key));
    CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, url TEXT, retrieved TEXT, raw_path TEXT, sha256 TEXT, status INTEGER);
    CREATE TABLE IF NOT EXISTS issues(key TEXT PRIMARY KEY, house TEXT, kind TEXT, detail TEXT);
    """)
    return con


def official_url(url):
    p = urlparse(url)
    return p.scheme == "https" and p.hostname in ALLOWED_HOSTS and not p.username and not p.password


def respect_retry_after(headers):
    value = headers.get("Retry-After", "").strip()
    if not value:
        return
    try:
        seconds = int(value) if value.isdigit() else max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return
    if seconds > 30:
        raise ValueError(f"Retry-After {seconds}s; retry later")
    time.sleep(seconds)


def archive(root, con, key, url, body, status):
    digest = hashlib.sha256(body).hexdigest()
    rel = Path("raw") / (digest + ".json.gz")
    dest = root / rel
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():
        tmp = dest.with_suffix(".part")
        with tmp.open("wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as zipped:
                zipped.write(body)
        os.replace(tmp, dest)
    con.execute("INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?,?)", (key, url, now(), str(rel), digest, status))
    con.commit()
    return str(rel), digest


def request_json(root, con, key, url, params=None):
    if not official_url(url):
        raise ValueError("unapproved source host: " + url)
    error = ""
    for attempt in range(3):
        try:
            with requests.get(url, params=params, headers=HEADERS, timeout=(15, 90), stream=True, allow_redirects=False) as response:
                chunks = []
                total = 0
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > MAX_RESPONSE:
                        raise ValueError("response exceeds 16 MiB; partition query")
                    chunks.append(chunk)
                body = b"".join(chunks)
                raw_path, sha = archive(root, con, key+f":attempt:{attempt+1}", response.url, body, response.status_code)
                if 300 <= response.status_code < 400:
                    raise ValueError("API redirect requires inspection")
                if response.status_code == 429 or response.status_code >= 500:
                    respect_retry_after(response.headers)
                response.raise_for_status()
                archive(root, con, key, response.url, body, response.status_code)
                try:
                    parsed = json.loads(body)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError("malformed or truncated JSON response") from exc
                return parsed, response.url, raw_path, sha
        except (requests.RequestException, ValueError) as exc:
            error = str(exc)
            if attempt < 2 and "exceeds 16 MiB" not in error and "retry later" not in error:
                time.sleep(0.4 * (attempt + 1))
            else:
                break
    raise RuntimeError(error)


def create_jobs(con, cfg):
    start = date.fromisoformat(cfg["dates"]["from"])
    end = date.fromisoformat(cfg["dates"]["to"])
    for house in cfg["houses"]:
        for term in cfg["search"]["terms"]:
            for year in range(start.year, end.year + 1):
                begin, finish = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
                key = hashlib.sha256(jsonstr([house, term, begin.isoformat(), finish.isoformat(), 0]).encode()).hexdigest()
                con.execute("INSERT OR IGNORE INTO jobs(key,house,term,start,end,page,state) VALUES(?,?,?,?,?,0,'pending')",
                            (key, house, term, begin.isoformat(), finish.isoformat()))
    con.commit()


def save_question(con, q, term, cfg):
    try:
        date.fromisoformat(q["date"])
    except ValueError:
        con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)", (q["key"]+":date", q["house"], "invalid_date", q["date"]))
        return
    if not cfg["dates"]["from"] <= q["date"] <= cfg["dates"]["to"]:
        return
    con.execute("INSERT INTO questions VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (q["key"], q["house"], q["source_id"], jsonstr(q)))
    # Discovery is preserved separately even when the index does not disclose the field.
    con.execute("INSERT OR IGNORE INTO matches VALUES(?,?,?,?,?,?)", (q["key"], term, "source_index", "source_returned", 0, ""))
    for field in ("title", "question_text", "answer_text"):
        count, context = matches(q[field], term)
        if count:
            con.execute("INSERT OR REPLACE INTO matches VALUES(?,?,?,?,?,?)", (q["key"], term, field, "literal_verified", count, context))
    if q["house"] == "RS" and not q["answer_text"]:
        con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)", (q["key"]+":answer", "RS", "answer_unavailable", "API did not supply answer text; answer-only matches may be missed"))


def add_document(con, qkey, url, language="", name="", checksum=""):
    if not url or not official_url(url):
        if url:
            con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)", (qkey+":url:"+hashlib.sha256(url.encode()).hexdigest()[:8], qkey[:2], "unsupported_document_url", url))
        return
    key = hashlib.sha256((qkey + "|" + url).encode()).hexdigest()
    con.execute("INSERT OR IGNORE INTO documents(key,question_key,url,language,name,repository_checksum,state,verification) VALUES(?,?,?,?,?,?,'pending','not_checked')", (key,qkey,url,language,name,checksum))


def ls_enrich(root, con, q):
    base = q["item_api_url"]
    if not base:
        raise ValueError("missing DSpace item API URL")
    item, *_ = request_json(root, con, "item:"+q["key"], base)
    bundles_url = item.get("_links", {}).get("bundles", {}).get("href", "")
    if not bundles_url:
        raise ValueError("missing bundles URL")
    for bundle in paginated(root, con, "bundles:"+q["key"], bundles_url, "bundles"):
        if str(bundle.get("name", "")).upper() != "ORIGINAL":
            continue
        bits_url = bundle.get("_links", {}).get("bitstreams", {}).get("href", "")
        if not bits_url:
            raise ValueError("missing bitstreams URL")
        for bit in paginated(root, con, "bits:"+q["key"]+":"+str(bundle.get("uuid")), bits_url, "bitstreams"):
            name = str(bit.get("name") or "")
            url = bit.get("_links", {}).get("content", {}).get("href", "")
            if not url:
                raise ValueError("missing bitstream content URL")
            check = bit.get("checkSum") or {}
            checksum = str(check.get("checkSumAlgorithm", "")) + ":" + str(check.get("value", "")) if check.get("value") else ""
            lang = "hi" if re.search(r"(?:^|[_\-.])(?:hin|hindi)(?:[_\-.]|$)", name, re.I) else "unknown"
            add_document(con, q["key"], url, lang, name, checksum)
    con.commit()


def paginated(root, con, key, url, field):
    page = 0
    while True:
        data, *_ = request_json(root, con, key+":"+str(page), url, {"page": page, "size": 100})
        vals = data.get("_embedded", {}).get(field, [])
        if not isinstance(vals, list):
            raise ValueError("invalid DSpace " + field)
        if not vals and int(data.get("page", {}).get("totalElements", 0)) > 0:
            raise ValueError("DSpace response missing expected " + field)
        yield from vals
        page += 1
        info = data.get("page", {})
        if page >= int(info.get("totalPages", 1)):
            break


def rs_docs(con, q, row):
    for field, language in (("files", "en"), ("hindifiles", "hi")):
        url = str(row.get(field) or "").strip().replace("\\", "/")
        add_document(con, q["key"], url, language, Path(urlparse(url).path).name)


def recover_rs(root, con, q):
    if not all(q.get(x) for x in ("session", "question_number", "question_type", "date")):
        raise ValueError("insufficient RS metadata for recovery")
    alternate_ids = set()
    for page in range(1, 101):
        data, *_ = request_json(root, con, "recover:"+q["key"]+":"+str(page), rs.INTEGRATION,
                               {"sessionFrom": q["session"], "sessionTo": q["session"], "questionNo": q["question_number"],
                                "questionType": q["question_type"], "page": page, "size": 100})
        rows = data.get("records")
        if not isinstance(rows, list):
            raise ValueError("recovery response lacks records")
        for row in rows:
            candidate = rs.question(row)
            from difflib import SequenceMatcher
            if all(candidate[k].casefold() == q[k].casefold() for k in ("session", "question_number", "question_type", "date")) and SequenceMatcher(None, normalize(candidate["title"]), normalize(q["title"])).ratio() >= .65:
                alternate_ids.add(candidate["source_id"])
                rs_docs(con, q, row)
        if page >= int(data.get("_metadata", {}).get("totalPages", 1)):
            q["alternate_source_ids"] = " | ".join(sorted(alternate_ids))
            con.execute("UPDATE questions SET payload=? WHERE key=?", (jsonstr(q),q["key"]))
            con.commit()
            return
    raise ValueError("recovery pagination exceeded limit")


def enrich(root, con, q):
    key = q["key"] + ":enrich"
    try:
        if q["house"] == "LS":
            ls_enrich(root, con, q)
        else:
            recover_rs(root, con, q)
        con.execute("DELETE FROM issues WHERE key=?", (key,))
    except (ValueError, RuntimeError, KeyError) as exc:
        con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)", (key, q["house"], "enrichment_failed", str(exc)))
    if not con.execute("SELECT 1 FROM documents WHERE question_key=?", (q["key"],)).fetchone():
        con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?)", (q["key"]+":documents", q["house"], "no_document_link", "No official document link was found"))
    else:
        con.execute("DELETE FROM issues WHERE key=?", (q["key"]+":documents",))
    con.commit()


def add_page(con, job, page):
    key = hashlib.sha256(jsonstr([job["house"],job["term"],job["start"],job["end"],page]).encode()).hexdigest()
    con.execute("INSERT OR IGNORE INTO jobs(key,house,term,start,end,page,state) VALUES(?,?,?,?,?,?,'pending')",
                (key,job["house"],job["term"],job["start"],job["end"],page))


def process_job(root, con, cfg, job):
    try:
        if job["house"] == "LS":
            data, url, raw, sha = request_json(root, con, "search:"+job["key"], ls.API,
                {"scope":ls.SCOPE,"query":job["term"],"f.dateIssued":job["start"][:4]+",equals","size":100,"page":job["page"]})
            objects, total = ls.page(data)
            if not objects and job["page"] < total:
                raise ValueError("empty page before expected total")
            rows = [(ls.question(obj), None) for obj in objects]
        else:
            end_exclusive = (date.fromisoformat(job["end"]) + timedelta(days=1)).isoformat()
            where = f"adate >= '{job['start']}' AND adate < '{end_exclusive}' AND {rs.clause(job['term'])}"
            data, url, raw, sha = request_json(root, con, "search:"+job["key"], rs.API, {"whereclause":where})
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise ValueError("RS search response is not a list of records")
            rows = [(rs.question(row), row) for row in data]
            if any(not job["start"] <= q["date"] <= job["end"] for q, _ in rows):
                raise ValueError("RS API returned a record outside the requested date range")
            total = 1
        for q, source in rows:
            save_question(con, q, job["term"], cfg)
            if source is not None and cfg["dates"]["from"] <= q["date"] <= cfg["dates"]["to"]:
                rs_docs(con, q, source)
        if job["house"] == "LS" and job["page"] + 1 < total:
            add_page(con, job, job["page"]+1)
        con.execute("UPDATE jobs SET state='done',error='',url=?,raw_path=?,sha256=?,retrieved=?,total_pages=? WHERE key=?", (url,raw,sha,now(),total,job["key"]))
        con.commit()
        print(f"{job['house']} {job['term']!r} {job['start']} page {job['page']}: {len(rows)} records", flush=True)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        con.rollback()
        # Split an unparseable or oversized RS period, down to a single day.
        begin, end = date.fromisoformat(job["start"]), date.fromisoformat(job["end"])
        if job["house"] == "RS" and begin < end and any(x in str(exc) for x in ("truncated", "exceeds 16 MiB")):
            mid = begin + (end-begin)//2
            for lo, hi in ((begin,mid),(mid+timedelta(days=1),end)):
                sub = dict(job); sub["start"],sub["end"] = lo.isoformat(),hi.isoformat()
                add_page(con, sub, 0)
            con.execute("UPDATE jobs SET state='split',error=? WHERE key=?", (str(exc),job["key"]))
        else:
            con.execute("UPDATE jobs SET state='failed',error=? WHERE key=?", (str(exc),job["key"]))
        con.commit()
        print(f"{job['house']} search failed: {exc}", flush=True)


def collect(root, con, cfg, resume):
    create_jobs(con, cfg)
    if resume:
        con.execute("UPDATE jobs SET state='pending' WHERE state='failed'")
        con.commit()
    while True:
        job = con.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY house,start,term,page LIMIT 1").fetchone()
        if not job:
            break
        process_job(root, con, cfg, job)
    # Iterate in key order without buffering all records; enrichment is idempotent.
    last = ""
    while True:
        row = con.execute("SELECT key,payload FROM questions WHERE key>? ORDER BY key LIMIT 1", (last,)).fetchone()
        if not row:
            break
        last = row["key"]
        marker = row["key"]+":complete"
        if con.execute("SELECT 1 FROM requests WHERE key=?", (marker,)).fetchone():
            continue
        q = json.loads(row["payload"])
        enrich(root, con, q)
        if not con.execute("SELECT 1 FROM issues WHERE key=?", (q["key"]+":enrich",)).fetchone():
            con.execute("INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?,?)", (marker,"",now(),"","",0)); con.commit()


def file_hash(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def header_question_numbers(text):
    """Read labelled question numbers while rejecting dates in the number position."""
    numbers = []
    for heading in re.finditer(r"\bquestion\b", text, flags=re.IGNORECASE):
        remainder = text[heading.end():]
        label = re.match(r"\s*(?:number|n[o0])\.?\s*[:#*.-]*\s*", remainder, flags=re.IGNORECASE)
        if label is None:
            continue
        value = remainder[label.end():]
        number = re.match(r"\d{1,5}(?!\d)", value)
        if number is None:
            continue
        # Check the entire candidate as a calendar date before accepting its
        # first component as a question number.
        calendar = re.match(r"(\d{1,2})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{2,4})(?!\d)", value)
        if calendar:
            day, _, month, year = calendar.groups()
            year = int(year)
            if year < 100:
                year += 2000
            try:
                date(year, int(month), int(day))
                continue
            except ValueError:
                pass
        numbers.append(number.group())
    return numbers


def verify_text(text, question):
    """Require a question number plus independent date or title evidence per page."""
    number = str(question.get("question_number") or "")
    title = normalize(question.get("title", ""))
    target_date = question.get("date", "")
    title_words = set(re.findall(r"[a-z]{4,}", title)) - {"language", "languages", "government", "regarding"}
    conflicting = []
    for i, page in enumerate(text.split("\f"), 1):
        head = normalize(page[:2000])
        found = header_question_numbers(head)
        if not found:
            found = re.findall(r"(?:^|\n)[ \t]*[*†#+]?[ \t]*(\d{1,5})[.．, \t#]+(?:SHRI|SHRIMATI|SMT\.?|DR\.|PROF\.)", page, re.I)
        expected_type = str(question.get("question_type") or "").upper().replace("-", "").replace(" ", "")
        types = re.search(r"\b(UN[- ]?STARRED|STARRED)\s+QUESTION", head[:700], re.I)
        if types and expected_type and types.group(1).upper().replace("-", "").replace(" ", "") != expected_type:
            continue
        dates = set()
        for d, m, y in re.findall(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](20\d{2})(?!\d)", head[:700]):
            try:
                dates.add(date(int(y),int(m),int(d)).isoformat())
            except ValueError:
                pass
        overlap = len(title_words & set(re.findall(r"[a-z]{4,}", head))) / max(1, len(title_words))
        title_ok = (len(title) >= 8 and title in head) or (len(title_words) >= 2 and overlap >= .8)
        if number and number in found and (target_date in dates or (not dates and title_ok)):
            return "verified_content", jsonstr({"page": i, "number": number, "dates": sorted(dates), "title_overlap": round(overlap, 3)})
        if found and number not in found:
            conflicting.extend(found)
    if conflicting:
        return "question_number_mismatch", jsonstr({"expected":number,"observed":conflicting[:10]})
    return "identity_unconfirmed", "insufficient independent question number and date/title evidence"


def verify_pdf(path, q):
    try:
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "not_checked", "pdftotext unavailable or timed out"
    if proc.returncode:
        return "not_checked", "pdftotext could not extract text"
    text = proc.stdout.decode("utf-8", errors="replace")
    if len(re.sub(r"\s", "", text)) < 80:
        return "needs_ocr", "insufficient text; OCR or manual review required"
    return verify_text(text, q)


def download_one(root, con, row):
    path = root / row["path"] if row["path"] else None
    if row["state"] == "downloaded" and path and path.is_file() and file_hash(path) == row["sha256"]:
        # Older checkpoints may have stored extensionless PDF URLs as .bin.
        with path.open("rb") as existing:
            is_pdf = existing.read(16).lstrip().startswith(b"%PDF-")
        if is_pdf and path.suffix != ".pdf":
            new_path = path.with_suffix(".pdf")
            os.replace(path, new_path)
            q = json.loads(con.execute("SELECT payload FROM questions WHERE key=?", (row["question_key"],)).fetchone()[0])
            verification, evidence = verify_pdf(new_path, q)
            con.execute("UPDATE documents SET path=?,verification=?,evidence=? WHERE key=?", (str(new_path.relative_to(root)),verification,evidence,row["key"]))
            con.commit()
        return
    url = row["url"]
    suffix = Path(urlparse(row["name"] or urlparse(url).path).path).suffix.lower()
    if suffix not in {".pdf", ".doc", ".docx", ".html", ".htm", ".txt"}:
        suffix = ".bin"
    rel = Path("documents") / (row["key"][:24] + suffix)
    target = root / rel
    target.parent.mkdir(exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    error = ""
    for attempt in range(3):
        try:
            current = url
            for _ in range(6):
                if not official_url(current):
                    raise ValueError("unapproved document host")
                response = requests.get(current, headers=HEADERS, timeout=(15,90), stream=True, allow_redirects=False)
                if response.is_redirect:
                    from urllib.parse import urljoin
                    current = urljoin(current, response.headers.get("Location", ""))
                    response.close()
                    continue
                break
            else:
                raise ValueError("too many document redirects")
            with response:
                if response.status_code == 429 or response.status_code >= 500:
                    respect_retry_after(response.headers)
                response.raise_for_status()
                digest = hashlib.sha256(); size = 0; prefix = b""
                with temp.open("wb") as f:
                    for block in response.iter_content(1024*1024):
                        if not block:
                            continue
                        if len(prefix) < 16:
                            prefix += block[:16-len(prefix)]
                        f.write(block); digest.update(block); size += len(block)
                if size == 0:
                    raise ValueError("empty document")
                if suffix == ".pdf" and not prefix.lstrip().startswith(b"%PDF-"):
                    raise ValueError("document is not a valid PDF signature")
                if suffix == ".bin" and prefix.lstrip().startswith(b"%PDF-"):
                    rel = rel.with_suffix(".pdf")
                    target = root / rel
                    suffix = ".pdf"
                if row["repository_checksum"]:
                    algorithm, expected = row["repository_checksum"].split(":", 1)
                    algo = algorithm.lower().replace("-", "")
                    if algo in {"md5", "sha1", "sha256"} and file_hash(temp, algo).lower() != expected.lower():
                        raise ValueError("repository checksum mismatch")
                os.replace(temp, target)
                q = json.loads(con.execute("SELECT payload FROM questions WHERE key=?", (row["question_key"],)).fetchone()[0])
                verification, evidence = verify_pdf(target, q) if suffix == ".pdf" else ("not_checked", "non-PDF original")
                con.execute("UPDATE documents SET state='downloaded',path=?,sha256=?,size=?,retrieved=?,verification=?,evidence=?,error='' WHERE key=?", (str(rel),digest.hexdigest(),size,now(),verification,evidence,row["key"]))
                con.commit()
                print(f"Downloaded {row['question_key']}: {rel} ({verification})", flush=True)
                return
        except (requests.RequestException, ValueError, OSError) as exc:
            error = str(exc)
            if temp.exists():
                temp.unlink()
            if attempt < 2 and "retry later" not in error:
                time.sleep(.4 * (attempt+1))
            elif "retry later" in error:
                break
    con.execute("UPDATE documents SET state='failed',error=? WHERE key=?", (error,row["key"]))
    con.commit()
    print(f"Download failed {row['question_key']}: {error}", flush=True)


def download(root, con):
    last = ""
    while True:
        row = con.execute("SELECT * FROM documents WHERE key>? ORDER BY key LIMIT 1", (last,)).fetchone()
        if not row:
            return
        last = row["key"]
        download_one(root, con, row)


def csv_export(path, fields, rows):
    temp = path.with_suffix(".part")
    with temp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    os.replace(temp, path)


def export(root, con):
    questions = (json.loads(row[0]) for row in con.execute("SELECT payload FROM questions ORDER BY house,key"))
    csv_export(root/"questions.csv", ["key","house","source_id","alternate_source_ids","term","session","date","question_number","question_type","title","members","ministry","question_text","answer_text","text_source","official_url","item_api_url","source"], questions)
    csv_export(root/"matches.csv", ["question_key","term","field","method","occurrences","context"], con.execute("SELECT * FROM matches ORDER BY question_key,term,field,method"))
    csv_export(root/"documents.csv", ["key","question_key","url","language","name","repository_checksum","state","path","sha256","size","retrieved","verification","evidence","error"], con.execute("SELECT * FROM documents ORDER BY question_key,key"))
    csv_export(root/"coverage.csv", ["key","house","term","start","end","page","state","error","url","raw_path","sha256","retrieved","total_pages"], con.execute("SELECT * FROM jobs ORDER BY house,start,term,page"))
    csv_export(root/"unresolved.csv", ["key","house","kind","detail"], con.execute("SELECT * FROM issues UNION ALL SELECT key,house,'search_failed',error FROM jobs WHERE state='failed' UNION ALL SELECT key,substr(question_key,1,2),'document_failed',error FROM documents WHERE state='failed' ORDER BY 1"))
    run = con.execute("SELECT * FROM run LIMIT 1").fetchone()
    counts = {table: con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("questions","matches","documents","jobs","issues")}
    failed = con.execute("SELECT count(*) FROM jobs WHERE state IN ('pending','failed')").fetchone()[0]
    summary = {"config": json.loads(run["config"]), "config_sha256":run["config_hash"], "started_utc":run["started"],
               "exported_utc":now(), "counts":counts, "incomplete_search_jobs":failed,
               "coverage_note":"Search results are candidates; completeness is not established. RS answer fields may be missing; LS DSpace indexed fields are not disclosed."}
    temp = root/"run.json.part"
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    os.replace(temp,root/"run.json")
    return summary
