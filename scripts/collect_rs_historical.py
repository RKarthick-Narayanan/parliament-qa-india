#!/usr/bin/env python3
"""Collect targeted Rajya Sabha Q&A metadata before 2003.

The official legacy endpoint can truncate large all-year responses while still
returning HTTP 200.  This collector therefore uses bounded year/term requests,
archives each valid response, and re-applies exact local phrase matching before
exporting candidates.  It is idempotent: a valid archived response is reused.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

API = "https://rsdoc.nic.in/Question/Search_Questions"
TERMS = [
    "endangered languages", "dialect", "dialects", "mother tongue",
    "tribal languages", "tribal language", "tribal dialect", "tribal dialects",
    "regional language", "regional languages", "eighth schedule",
]
FIELDS = {"title": "qtitle", "question_text": "qn_text", "answer_text": "ans_text"}
HEADERS = {
    "User-Agent": "ParliamentLanguageResearch/2.0 (academic; rate limited)",
    "Referer": "https://sansad.in/", "Accept": "application/json",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plain(value) -> str:
    text = str(value or "")
    for _ in range(2):
        text = html.unescape(text)
    text = re.sub(r"<(?:br\s*/?|/p)>", "\n", text, flags=re.I)
    return re.sub(r"<[^>]*>", " ", text)


def normalized(value) -> str:
    return re.sub(r"\s+", " ", plain(value)).strip().casefold()


PATTERNS = {
    term: re.compile(r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)", re.I)
    for term in TERMS
}


def iso_date(row: dict) -> str:
    candidate = str(row.get("adate") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return candidate
    try:
        return datetime.strptime(str(row.get("ans_date") or "")[:10], "%d.%m.%Y").date().isoformat()
    except ValueError:
        return ""


def qnum(value) -> str:
    try:
        return str(int(float(str(value))))
    except (TypeError, ValueError):
        return str(value or "").strip()


def matches(row: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for label, field in FIELDS.items():
        text = normalized(row.get(field))
        for term, pattern in PATTERNS.items():
            if pattern.search(text):
                result.setdefault(term, set()).add(label)
    return result


def term_clause(term: str) -> str:
    value = term.replace(" ", "%")
    return "(" + " OR ".join(f"{field} LIKE '%{value}%'" for field in FIELDS.values()) + ")"


def valid_archive(path: Path, year: int) -> list[dict] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or any(iso_date(row)[:4] != str(year) for row in data):
        return None
    return data


def fetch_job(out: Path, year: int, index: int, term: str) -> tuple[dict, list[dict]]:
    key = f"year_{year}_{index:02d}"
    path = out / "raw" / "term" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = valid_archive(path, year)
    where = f"adate >= '{year}-01-01' AND adate < '{year + 1}-01-01' AND {term_clause(term)}"
    url = API + "?" + urlencode({"whereclause": where})
    if data is None:
        error = ""
        for attempt in range(1, 4):
            try:
                response = requests.get(API, params={"whereclause": where}, headers=HEADERS,
                                        timeout=(15, 120))
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    raise ValueError("official response was not a list")
                if any(iso_date(row)[:4] != str(year) for row in data):
                    raise ValueError("official endpoint ignored year boundaries")
                temp = path.with_suffix(".json.part")
                temp.write_bytes(response.content)
                os.replace(temp, path)
                url = response.url
                break
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                data = None
                if attempt < 3:
                    time.sleep(attempt)
        if data is None:
            return ({"key": key, "year": year, "term": term, "url": url,
                     "state": "failed", "records": 0, "sha256": "", "bytes": 0,
                     "retrieved_at": now(), "error": error}, [])
    body = path.read_bytes()
    return ({"key": key, "year": year, "term": term, "url": url,
             "state": "ok", "records": len(data), "sha256": hashlib.sha256(body).hexdigest(),
             "bytes": len(body), "retrieved_at": now(), "error": ""}, data)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    temp = path.with_suffix(path.suffix + ".part")
    with temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--from-year", type=int, default=1999)
    parser.add_argument("--to-year", type=int, default=2002)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    jobs = [(year, index, term) for year in range(args.from_year, args.to_year + 1)
            for index, term in enumerate(TERMS)]
    evidence = []
    records: dict[str, dict] = {}
    discoveries: dict[str, dict[str, set[str]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_job, out, *job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            meta, rows = future.result(); evidence.append(meta)
            for row in rows:
                hit = matches(row)
                if not hit:
                    continue
                ident = str(row.get("qslno") or "")
                if not ident:
                    continue
                records[ident] = row
                target = discoveries.setdefault(ident, {})
                for term, fields in hit.items():
                    target.setdefault(term, set()).update(fields)
            print(f"Historical RS queries: {done}/{len(jobs)}", flush=True)

    candidates = []
    for ident, row in records.items():
        day = iso_date(row)
        terms = sorted(discoveries[ident])
        matching_fields = sorted({field for fields in discoveries[ident].values() for field in fields})
        candidates.append({
          "house":"RS", "qslno":ident, "source_id":ident, "session":row.get("ses_no") or "",
          "question_number":qnum(row.get("qno")), "question_type":str(row.get("qtype") or "").strip(),
          "answer_date":day, "date":day, "title":plain(row.get("qtitle")).strip(),
          "members":plain(row.get("name")).strip(), "ministry":plain(row.get("min_name")).strip(),
          "question_text":plain(row.get("qn_text")).strip(), "answer_text":plain(row.get("ans_text")).strip(),
          "matched_terms":" | ".join(terms), "matching_fields":" | ".join(matching_fields),
          "official_record_api_url":API+"?"+urlencode({"whereclause":f"qslno={ident}"}),
          "official_url":API+"?"+urlencode({"whereclause":f"qslno={ident}"}),
          "source_url":str(row.get("files") or "").strip(), "hindi_source_url":str(row.get("hindifiles") or "").strip(),
          "language":"en", "collection_scope":"targeted_search_question",
        })
    candidates.sort(key=lambda row: (row["answer_date"], int(row["question_number"] or 0), row["qslno"]))
    candidate_fields = ["house","qslno","source_id","session","question_number","question_type",
      "answer_date","date","title","members","ministry","question_text","answer_text","matched_terms",
      "matching_fields","official_record_api_url","official_url","source_url","hindi_source_url","language",
      "collection_scope"]
    evidence.sort(key=lambda row: (row["year"], row["term"]))
    write_csv(out/"RS_QA_1999_2002_candidates.csv", candidates, candidate_fields)
    seed_rows = []
    for row in candidates:
        for language, source_url in (("en", row["source_url"]), ("hi", row["hindi_source_url"])):
            if not source_url:
                continue
            seed = dict(row)
            seed["language"] = language
            seed["source_url"] = source_url
            seed["name"] = source_url.split("?", 1)[0].rsplit("/", 1)[-1]
            seed["corpus_role"] = "matching_question"
            seed_rows.append(seed)
    seed_fields = candidate_fields + ["name", "corpus_role"]
    write_csv(out/"RS_QA_1999_2002_seed.csv", seed_rows, seed_fields)
    write_csv(out/"source_requests.csv", evidence,
              ["key","year","term","url","state","records","sha256","bytes","retrieved_at","error"])
    counts = {str(year): sum(row["answer_date"].startswith(str(year)) for row in candidates)
              for year in range(args.from_year, args.to_year + 1)}
    report = {"generated_at":now(), "terms":TERMS, "queries":len(evidence),
              "successful_queries":sum(row["state"] == "ok" for row in evidence),
              "unique_matching_questions":len(candidates), "counts_by_year":counts,
              "seed_document_rows":len(seed_rows),
              "scope":"targeted search across official title, question text, and answer text"}
    (out/"collection_report.json").write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["successful_queries"] == report["queries"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
