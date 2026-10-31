import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.sample_qa import create_sample, largest_remainder, period_for


class RepresentativeSampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_database(self, house: str) -> Path:
        path = self.base / f"{house}.sqlite3"
        con = sqlite3.connect(path)
        con.executescript("""
          CREATE TABLE questions(
            corpus_id TEXT PRIMARY KEY, house TEXT, answer_date TEXT, session TEXT,
            question_number TEXT, question_type TEXT, title TEXT, members TEXT,
            ministry TEXT, official_url TEXT, publication_status TEXT
          );
          CREATE TABLE sections(
            section_id TEXT PRIMARY KEY, corpus_id TEXT, section_type TEXT,
            sequence INTEGER, text TEXT
          );
        """)
        years = (2001, 2010, 2018, 2024)
        number = 0
        for year in years:
            for question_type in ("STARRED", "UNSTARRED"):
                for duplicate in range(3):
                    number += 1
                    corpus_id = f"{house}-{number:03}"
                    con.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        corpus_id, house, f"{year}-01-{duplicate + 1:02}", "S1", str(number),
                        question_type, f"Title {number}", f"Member {number}", "MINISTRY",
                        f"https://example.invalid/{corpus_id}", "auto_approved",
                    ))
                    con.execute("INSERT INTO sections VALUES(?,?,?,?,?)",
                                (f"{corpus_id}-q", corpus_id, "question", 0, f"Question {number}?"))
                    con.execute("INSERT INTO sections VALUES(?,?,?,?,?)",
                                (f"{corpus_id}-a", corpus_id, "answer", 0, f"Answer {number}."))
        # These records must never enter the eligible population.
        con.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            f"{house}-review", house, "2024-02-01", "S1", "90", "UNSTARRED",
            "Review", "Member", "MINISTRY", "", "needs_review"))
        con.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            f"{house}-missing", house, "2024-02-02", "S1", "91", "UNSTARRED",
            "Missing answer", "Member", "MINISTRY", "", "auto_approved"))
        con.execute("INSERT INTO sections VALUES(?,?,?,?,?)",
                    (f"{house}-missing-q", f"{house}-missing", "question", 0, "Question?"))
        con.commit()
        con.close()
        return path

    def test_period_boundaries(self):
        self.assertEqual(period_for("1999-01-01"), "1999-2006")
        self.assertEqual(period_for("2014-12-31"), "2007-2014")
        self.assertEqual(period_for("2026-10-30"), "2021-2026")
        self.assertEqual(period_for("1998-12-31"), "")

    def test_largest_remainder_is_exact_and_proportional(self):
        result = largest_remainder({("a", "x"): 7, ("b", "x"): 2, ("c", "x"): 1}, 4)
        self.assertEqual(sum(result.values()), 4)
        self.assertEqual(result[("a", "x")], 3)

    def test_sample_is_complete_balanced_and_byte_reproducible(self):
        ls = self.make_database("LS")
        rs = self.make_database("RS")
        first = self.base / "first"
        second = self.base / "second"
        manifest = create_sample(ls, rs, 8, 12345, first)
        create_sample(ls, rs, 8, 12345, second)

        self.assertEqual(manifest["house_sample_sizes"], {"LS": 4, "RS": 4})
        self.assertEqual(manifest["eligibility"]["LS"]["eligible_complete_qa"], 24)
        self.assertEqual(len(set(manifest["selected_corpus_ids"])), 8)
        for name in ("sample_40.csv", "sample_40.jsonl", "sample_40.txt", "sampling_manifest.json"):
            one = (first / name).read_bytes()
            two = (second / name).read_bytes()
            self.assertEqual(hashlib.sha256(one).digest(), hashlib.sha256(two).digest())
        rows = [json.loads(line) for line in (first / "sample_40.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["question_text"] and row["answer_text"] for row in rows))
        self.assertTrue(all(float(row["sampling_weight"]) > 0 for row in rows))

    def test_odd_sample_size_is_rejected(self):
        ls = self.make_database("LS")
        rs = self.make_database("RS")
        with self.assertRaisesRegex(ValueError, "even"):
            create_sample(ls, rs, 7, 1, self.base / "output")


if __name__ == "__main__":
    unittest.main()
