import tempfile
import unittest
from pathlib import Path

from parliament_qa.concepts import (
    _upsert_candidate,
    discovery_open,
    exact_hits,
    load_concept_config,
    normalize_concept_text,
)


CONFIG = Path(__file__).parents[1] / "config" / "survey_census_concepts.yaml"


class ConceptRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_concept_config(CONFIG)

    def test_normalization_repairs_html_unicode_and_line_hyphenation(self):
        value = "<p>Mother-\n tongue&nbsp;surveys</p>"
        self.assertEqual(normalize_concept_text(value), "mother tongue surveys")

    def test_exact_phrase_families_and_variants(self):
        cases = {
            "A linguistic survey was undertaken.": "linguistic_survey",
            "Surveys on languages were undertaken.": "linguistic_survey",
            "The mother-tongue enumeration is complete.": "mother_tongue_survey",
            "An enumeration of mother tongues was conducted.": "mother_tongue_survey",
            "The linguistic censuses use new tables.": "language_census",
            "Language classification based on the census was published.": "language_census",
        }
        for text, concept in cases.items():
            with self.subTest(text=text):
                found = {hit["concept"] for hit in exact_hits(self.config, {"answer": text})}
                self.assertIn(concept, found)

    def test_false_boundaries_and_semantic_expansion_are_rejected(self):
        for text in (
            "A language surveyor was appointed.",
            "The census discusses multilingual classification.",
            "Language mapping and dialect documentation were conducted.",
        ):
            with self.subTest(text=text):
                self.assertEqual(exact_hits(self.config, {"answer": text}), [])

    def test_hits_preserve_field_observed_form_count_and_context(self):
        hits = exact_hits(self.config, {"answer": "Language survey; language surveys."})
        selected = next(hit for hit in hits if hit["concept"] == "linguistic_survey")
        self.assertEqual(selected["field"], "answer")
        self.assertEqual(selected["occurrences"], 2)
        self.assertEqual(selected["observed"], "Language survey")
        self.assertIn("language surveys", selected["context"].casefold())

    def test_candidate_and_hit_checkpoint_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            con = discovery_open(Path(temp))
            row = {
                "house": "RS", "source_id": "official-1", "session": "200",
                "answer_date": "2003-01-01", "question_number": "1",
                "question_type": "UNSTARRED", "title": "Language Survey",
                "members": "Member", "ministry": "HOME AFFAIRS",
                "question_text": "", "answer_text": "",
            }
            hits = exact_hits(self.config, {"title": row["title"]})
            first = _upsert_candidate(con, row, hits, "request-1", "test")
            second = _upsert_candidate(con, row, hits, "request-1", "test")
            self.assertEqual(first, second)
            self.assertEqual(con.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM hits").fetchone()[0], 1)
            con.close()


if __name__ == "__main__":
    unittest.main()
