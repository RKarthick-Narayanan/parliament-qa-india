#!/usr/bin/env python3
"""Create a reproducible, stratified sample of complete parliamentary Q&A."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


PERIODS = (
    (1999, 2006, "1999-2006"),
    (2007, 2014, "2007-2014"),
    (2015, 2020, "2015-2020"),
    (2021, 2026, "2021-2026"),
)
QUESTION_TYPES = ("STARRED", "UNSTARRED")
OUTPUT_FIELDS = (
    "sample_index", "corpus_id", "house", "answer_date", "period", "session",
    "question_number", "question_type", "title", "members", "ministry",
    "question_text", "answer_text", "official_url", "stratum_population",
    "stratum_sample_size", "sampling_probability", "sampling_weight",
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def period_for(answer_date: str) -> str:
    try:
        year = int(answer_date[:4])
    except (TypeError, ValueError):
        return ""
    return next((label for start, end, label in PERIODS if start <= year <= end), "")


def load_eligible(database: Path, expected_house: str) -> tuple[list[dict], dict]:
    if not database.is_file():
        raise ValueError(f"corpus database does not exist: {database}")
    con = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(row) for row in con.execute("""
      SELECT q.corpus_id,q.house,q.answer_date,q.session,q.question_number,
             q.question_type,q.title,q.members,q.ministry,q.official_url,
             COALESCE((SELECT group_concat(text,'\n') FROM
               (SELECT text FROM sections s WHERE s.corpus_id=q.corpus_id
                AND s.section_type='question' AND trim(s.text)<>''
                ORDER BY s.sequence,s.section_id)),'') AS question_text,
             COALESCE((SELECT group_concat(text,'\n') FROM
               (SELECT text FROM sections s WHERE s.corpus_id=q.corpus_id
                AND s.section_type='answer' AND trim(s.text)<>''
                ORDER BY s.sequence,s.section_id)),'') AS answer_text
      FROM questions q
      WHERE q.publication_status IN ('auto_approved','review_approved')
        AND q.question_type IN ('STARRED','UNSTARRED')
      ORDER BY q.corpus_id
    """)]
    total_ready = con.execute("""SELECT count(*) FROM questions
      WHERE publication_status IN ('auto_approved','review_approved')""").fetchone()[0]
    con.close()
    if any(row["house"] != expected_house for row in rows):
        raise ValueError(f"{database} contains an unexpected House")
    eligible = []
    for row in rows:
        row["period"] = period_for(row["answer_date"])
        row["question_text"] = row["question_text"].strip()
        row["answer_text"] = row["answer_text"].strip()
        if row["period"] and row["question_text"] and row["answer_text"]:
            eligible.append(row)
    return eligible, {
        "publication_ready": total_ready,
        "eligible_complete_qa": len(eligible),
        "excluded_missing_or_out_of_scope": total_ready - len(eligible),
    }


def largest_remainder(populations: dict[tuple[str, str], int], quota: int) -> dict[tuple[str, str], int]:
    total = sum(populations.values())
    if quota < 1:
        raise ValueError("House sample quota must be positive")
    if quota > total:
        raise ValueError(f"House sample quota {quota} exceeds {total} eligible records")
    exact = {key: count * quota / total for key, count in populations.items()}
    allocated = {key: int(value) for key, value in exact.items()}
    remaining = quota - sum(allocated.values())
    order = sorted(populations, key=lambda key: (-(exact[key] - allocated[key]), -populations[key], key))
    for key in order[:remaining]:
        allocated[key] += 1
    if sum(allocated.values()) != quota or any(allocated[key] > populations[key] for key in populations):
        raise RuntimeError("invalid largest-remainder allocation")
    return allocated


def seeded_priority(seed: int, corpus_id: str) -> str:
    return hashlib.sha256(f"{seed}|{corpus_id}".encode("utf-8")).hexdigest()


def select_house(records: list[dict], quota: int, seed: int) -> tuple[list[dict], list[dict]]:
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in records:
        strata[(row["period"], row["question_type"])].append(row)
    populations = {key: len(value) for key, value in strata.items()}
    allocation = largest_remainder(populations, quota)
    selected = []
    report = []
    for key in sorted(strata):
        population = populations[key]
        sample_size = allocation[key]
        ranked = sorted(strata[key], key=lambda row: (seeded_priority(seed, row["corpus_id"]), row["corpus_id"]))
        probability = sample_size / population if sample_size else 0.0
        for row in ranked[:sample_size]:
            output = dict(row)
            output.update({
                "stratum_population": population,
                "stratum_sample_size": sample_size,
                "sampling_probability": f"{probability:.12f}",
                "sampling_weight": f"{1 / probability:.12f}",
            })
            selected.append(output)
        report.append({
            "period": key[0], "question_type": key[1], "population": population,
            "sample_size": sample_size,
            "sampling_probability": round(probability, 12),
            "sampling_weight": round(1 / probability, 12) if probability else None,
        })
    return selected, report


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_outputs(output: Path, selected: list[dict], manifest: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "sample_40.csv"
    temporary = csv_path.with_suffix(".csv.part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(selected)
    os.replace(temporary, csv_path)
    atomic_text(output / "sample_40.jsonl", "".join(
        json.dumps({key: row.get(key, "") for key in OUTPUT_FIELDS}, ensure_ascii=False,
                   sort_keys=True) + "\n" for row in selected
    ))
    text_blocks = []
    for row in selected:
        text_blocks.append(
            f"{row['sample_index']}. {row['house']} | {row['answer_date']} | "
            f"{row['question_type']} QUESTION {row['question_number']}\n"
            f"Title: {row.get('title') or ''}\nMember(s): {row.get('members') or ''}\n"
            f"Ministry: {row.get('ministry') or ''}\nCorpus ID: {row['corpus_id']}\n"
            f"Official URL: {row.get('official_url') or ''}\n\nQUESTION\n{row['question_text']}\n\n"
            f"ANSWER\n{row['answer_text']}\n"
        )
    atomic_text(output / "sample_40.txt", ("\n" + "=" * 80 + "\n\n").join(text_blocks) + "\n")
    atomic_text(output / "sampling_manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def create_sample(ls_database: Path, rs_database: Path, size: int, seed: int, output: Path) -> dict:
    if size < 2 or size % 2:
        raise ValueError("sample size must be a positive even number")
    per_house = size // 2
    databases = {"LS": ls_database, "RS": rs_database}
    selected = []
    eligibility = {}
    stratum_reports = {}
    for house, database in databases.items():
        records, eligibility[house] = load_eligible(database, house)
        house_sample, stratum_reports[house] = select_house(records, per_house, seed)
        selected.extend(house_sample)
    selected.sort(key=lambda row: (row["house"], row["answer_date"], row["question_type"],
                                   row["question_number"], row["corpus_id"]))
    for index, row in enumerate(selected, 1):
        row["sample_index"] = index
    counts = Counter(row["house"] for row in selected)
    if len(selected) != size or len({row["corpus_id"] for row in selected}) != size:
        raise RuntimeError("sample size or uniqueness validation failed")
    if counts != {"LS": per_house, "RS": per_house}:
        raise RuntimeError("equal House allocation validation failed")
    if any(not row["question_text"] or not row["answer_text"] for row in selected):
        raise RuntimeError("selected record lacks question or answer text")
    manifest = {
        "schema_version": 1,
        "method": "equal House allocation; proportional largest-remainder allocation by period and question type; SHA-256 seeded selection",
        "seed": seed,
        "requested_size": size,
        "house_sample_sizes": dict(sorted(counts.items())),
        "periods": [label for _, _, label in PERIODS],
        "question_types": list(QUESTION_TYPES),
        "eligibility": eligibility,
        "strata": stratum_reports,
        "inputs": {house: {"path": str(path.resolve()), "sha256": file_hash(path)}
                   for house, path in databases.items()},
        "selected_corpus_ids": [row["corpus_id"] for row in selected],
    }
    write_outputs(output, selected, manifest)
    return manifest


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261031)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ls-db", type=Path,
                        default=repository / "results/lok-sabha-matching-corpus/corpus.sqlite3")
    parser.add_argument("--rs-db", type=Path,
                        default=repository / "results/rajya-sabha-matching-corpus/corpus.sqlite3")
    args = parser.parse_args()
    try:
        manifest = create_sample(args.ls_db, args.rs_db, args.size, args.seed, args.output)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "sample_size": manifest["requested_size"],
        "house_sample_sizes": manifest["house_sample_sizes"],
        "output": str(args.output.resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
