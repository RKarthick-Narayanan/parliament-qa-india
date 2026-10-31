import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from parliament_qa.corpus import (add_review, audit, build_index, corpus_open,
                                  export_corpus, export_voyant, extract, inventory, segment,
                                  stable_identity, upsert_question, inventory_ls, ocr_hindi,
                                  _rs_range)


def simple_pdf(path: Path, lines: list[str]):
    """Write a small, valid one-page PDF without a test-only dependency."""
    escaped = []
    for line in lines:
        line = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        escaped.append(f"({line}) Tj 0 -18 Td")
    stream = "BT /F1 11 Tf 72 740 Td " + " ".join(escaped) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream.encode())} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n{obj}\nendobj\n".encode())
    xref = len(content)
    content.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(content)


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "corpus"
        self.con = corpus_open(self.root)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def make_seed(self):
        pdf = self.base / "documents" / "question.pdf"
        pdf.parent.mkdir()
        simple_pdf(pdf, [
            "LOK SABHA", "UNSTARRED QUESTION NO. 42", "15.03.2020",
            "PRESERVATION OF ENDANGERED LANGUAGES",
            "Will the Minister state the preservation measures?",
            "ANSWER", "THE MINISTER OF CULTURE", "A preservation scheme is in operation.",
        ])
        manifest = self.base / "lok_sabha_seed.csv"
        fields = ["item_uuid", "date", "lok_sabha", "session", "question_number",
                  "question_type", "subject", "member", "ministry", "local_path",
                  "source_url", "language"]
        with manifest.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
            writer.writerow({"item_uuid":"official-42", "date":"2020-03-15", "lok_sabha":"17",
                "session":"III", "question_number":"42", "question_type":"UNSTARRED",
                "subject":"Preservation of endangered languages", "member":"Example Member",
                "ministry":"CULTURE", "local_path":"documents/question.pdf",
                "source_url":"https://elibrary.sansad.in/example/42.pdf", "language":"en"})
        return manifest

    def test_stable_identity(self):
        one = stable_identity("LS", "abc", "17", "III", "2020-03-15", "UNSTARRED", "42")
        two = stable_identity("LS", "abc", "18", "I", "2026-01-01", "STARRED", "1")
        self.assertEqual(one, two)
        fallback = stable_identity("RS", "", "", "252", "2020-03-15", "UNSTARRED", "42")
        self.assertTrue(fallback[0].startswith("RS-"))
        self.assertNotEqual(one, fallback)

    def test_official_id_enriches_fallback_without_duplicate(self):
        base={"house":"RS","date":"2020-03-15","question_number":"42",
              "question_type":"UNSTARRED","session":"250","title":"Example"}
        fallback=upsert_question(self.con,base)
        official=upsert_question(self.con,{**base,"source_id":"official-rs-42"})
        self.assertEqual(fallback,official)
        self.assertEqual(self.con.execute("select count(*) from questions").fetchone()[0],1)
        self.assertEqual(self.con.execute("select source_id from questions").fetchone()[0],"official-rs-42")

    def test_rs_official_record_url_alias(self):
        url="https://rsdoc.nic.in/Question/Search_Questions?whereclause=qslno%3D42"
        corpus_id=upsert_question(self.con,{"house":"RS","qslno":"42","date":"2020-03-15",
          "question_number":"42","question_type":"UNSTARRED","official_record_api_url":url})
        stored=self.con.execute("select official_url from questions where corpus_id=?",(corpus_id,)).fetchone()[0]
        self.assertEqual(stored,url)

    def test_seed_manifest_can_filter_corpus_role(self):
        manifest=self.base/"rs_roles.csv"
        fields=["qslno","date","question_number","question_type","corpus_role"]
        with manifest.open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
            writer.writerow({"qslno":"1","date":"2020-01-01","question_number":"1",
                             "question_type":"UNSTARRED","corpus_role":"matching_question"})
            writer.writerow({"qslno":"2","date":"2020-01-02","question_number":"2",
                             "question_type":"UNSTARRED","corpus_role":"auxiliary_seed_not_promoted"})
        result=inventory(self.root,self.con,[(manifest,"RS")],network=False,seed_role="matching_question")
        self.assertEqual(result["questions"],1)
        self.assertEqual(result["seeded"][0]["skipped_by_role"],1)

    def test_plural_pdf_fields_and_hardlink_storage(self):
        pdf=self.base/"documents"/"plural.pdf"; pdf.parent.mkdir(exist_ok=True)
        simple_pdf(pdf,["UNSTARRED QUESTION NO. 9","01.01.2020","ANSWER","Example answer text"])
        manifest=self.base/"lok_master.csv"
        fields=["item_uuid","date","question_number","question_type","pdf_local_paths",
                "pdf_source_urls","pdf_filenames","pdf_sha256"]
        with manifest.open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
            writer.writerow({"item_uuid":"ls-nine","date":"2020-01-01","question_number":"9",
                "question_type":"UNSTARRED","pdf_local_paths":"documents/plural.pdf",
                "pdf_source_urls":"https://elibrary.sansad.in/nine.pdf","pdf_filenames":"nine.pdf"})
        result=inventory(self.root,self.con,[(manifest,"LS")],network=False,seed_storage="hardlink")
        self.assertEqual(result["questions"],1)
        version=self.con.execute("select local_path from document_versions").fetchone()[0]
        self.assertEqual(Path(version).stat().st_ino,pdf.stat().st_ino)

    def test_official_enumeration_paginates_and_splits(self):
        def ls_page(number):
            item={"_embedded":{"indexableObject":{"uuid":f"ls-{number}","metadata":{
                "dc.date.issued":[{"value":"2020-03-15"}],
                "dc.identifier.questionnumber":[{"value":str(number)}],
                "dc.identifier.questiontype":[{"value":"Unstarred"}],
                "dc.title":[{"value":f"Question {number}"}]}}},
                "_links":{"indexableObject":{"href":f"https://elibrary.sansad.in/items/ls-{number}"}}}
            return {"_embedded":{"searchResult":{"_embedded":{"objects":[item]},"page":{"totalPages":2}}}}
        with patch("parliament_qa.corpus.official_get",side_effect=[ls_page(1),ls_page(2)]) as get:
            inventory_ls(self.root,self.con,date(2020,1,1),date(2020,12,31))
        self.assertEqual(get.call_count,2)
        self.assertEqual(self.con.execute("select count(*) from questions where house='LS'").fetchone()[0],2)

        rows=[
          [{"qslno":101,"qno":1,"ses_no":250,"adate":"2020-01-01","qtype":"UNSTARRED","qtitle":"One"}],
          [{"qslno":102,"qno":2,"ses_no":250,"adate":"2020-01-02","qtype":"UNSTARRED","qtitle":"Two"}],
        ]
        with patch("parliament_qa.corpus.official_get",side_effect=[ValueError("oversized"),*rows]) as get:
            _rs_range(self.root,self.con,date(2020,1,1),date(2020,1,2))
        self.assertEqual(get.call_count,3)
        self.assertEqual(self.con.execute("select count(*) from questions where house='RS'").fetchone()[0],2)

    def test_end_to_end_offline_and_idempotent(self):
        manifest = self.make_seed()
        result = inventory(self.root, self.con, [(manifest, "LS")], network=False)
        self.assertEqual(result["questions"], 1)
        version = self.con.execute("select * from document_versions").fetchone()
        self.assertTrue(Path(version["local_path"]).is_relative_to(self.root))
        self.assertEqual(Path(version["local_path"]).read_bytes()[:5], b"%PDF-")

        extracted = extract(self.root, self.con, use_ocr=False, workers=2)
        self.assertEqual(extracted["versions"], 1)
        self.assertEqual(self.con.execute("select count(*) from pages").fetchone()[0], 1)
        segmented = segment(self.root, self.con)
        self.assertEqual(segmented["auto_approved_versions"], 1)
        self.assertEqual({r[0] for r in self.con.execute("select section_type from sections")},
                         {"question", "answer", "full_document"})

        self.con.execute("update documents set language='hi'"); self.con.commit()
        with patch("parliament_qa.corpus._tesseract_languages",return_value={"eng","hin"}), \
             patch("parliament_qa.corpus._ocr_page",return_value="भारत सरकार हिंदी प्रश्न उत्तर"):
            hindi=ocr_hindi(self.root,self.con,workers=2)
        self.assertEqual(hindi["ocr_variants_created"],1)
        self.assertEqual(self.con.execute("select count(*) from page_text_variants").fetchone()[0],1)
        indexed=build_index(self.con)
        self.assertEqual(indexed["indexed"], 1)
        self.assertEqual(indexed["pages_indexed"],1)
        hit = self.con.execute("select corpus_id from question_fts where question_fts match 'preservation'").fetchone()
        self.assertIsNotNone(hit)
        report = audit(self.con)
        self.assertTrue(report["acceptance_passed"])

        exported = export_corpus(self.root, self.con)
        self.assertEqual(exported["counts"], {"questions":1, "pages":1,"page_text_variants":1})
        self.assertTrue((self.root/"exports/questions.jsonl").is_file())
        self.assertTrue((self.root/"exports/pages.jsonl").is_file())
        self.assertTrue((self.root/"exports/page_text_variants.jsonl").is_file())
        self.assertTrue((self.root/"exports/questions.parquet").is_file())
        record = json.loads((self.root/"exports/questions.jsonl").read_text().splitlines()[0])
        self.assertIn("preservation measures", record["cleaned_question"])
        self.assertEqual(record["question_text"],record["cleaned_question"])
        self.assertEqual(record["answer_text"],record["cleaned_answer"])

        voyant_root=self.base/"voyant"
        voyant=export_voyant(voyant_root,[self.root])
        self.assertEqual(voyant["questions"],1)
        self.assertEqual(voyant["question_files"],1)
        self.assertEqual(voyant["answer_files"],1)
        with (voyant_root/"metadata.csv").open(encoding="utf-8-sig") as stream:
            metadata=list(csv.DictReader(stream))
        self.assertEqual(metadata[0]["collection_scope"],"targeted_search_question")
        self.assertTrue((voyant_root/metadata[0]["complete_qa_file"]).is_file())

        inventory(self.root, self.con, [(manifest, "LS")], network=False)
        self.assertEqual(extract(self.root, self.con, use_ocr=False)["versions"], 0)
        self.assertEqual(segment(self.root, self.con)["segmented"], 0)
        self.assertEqual(self.con.execute("select count(*) from questions").fetchone()[0], 1)
        self.assertEqual(self.con.execute("select count(*) from document_versions").fetchone()[0], 1)

    def test_review_gate_for_incomplete_text(self):
        pdf = self.base / "uncertain.pdf"
        simple_pdf(pdf, ["QUESTION NO. 7", "01.01.2020", "No deterministic answer heading is present in this document."])
        manifest = self.base / "rs_seed.csv"
        with manifest.open("w", encoding="utf-8", newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=["date","question_number","session","local_path"]); writer.writeheader()
            writer.writerow({"date":"2020-01-01","question_number":"7","session":"250","local_path":str(pdf)})
        inventory(self.root,self.con,[(manifest,"RS")],network=False)
        extract(self.root,self.con,use_ocr=False); result=segment(self.root,self.con)
        self.assertEqual(result["versions_needing_review"],1)
        self.assertEqual(self.con.execute("select publication_status from questions").fetchone()[0],"needs_review")
        self.assertEqual(self.con.execute("select kind from issues").fetchone()[0],"manual_review_required")
        corpus_id=self.con.execute("select corpus_id from questions").fetchone()[0]
        review_id=add_review(self.con,corpus_id,"tester","approved","checked against PDF")
        self.assertTrue(review_id.startswith("R-"))
        self.assertEqual(self.con.execute("select publication_status from questions").fetchone()[0],"review_approved")
        self.assertEqual(self.con.execute("select state from issues").fetchone()[0],"resolved")

    def test_official_text_is_segmented_without_pdf(self):
        corpus_id=upsert_question(self.con,{"house":"RS","source_id":"api-only",
          "date":"2001-01-01","question_number":"10","question_type":"UNSTARRED",
          "question_text":"Official question text?","answer_text":"Official answer text."})
        result=segment(self.root,self.con)
        self.assertEqual(result["segmented"],0)
        sections=dict(self.con.execute("select section_type,text from sections where corpus_id=?",(corpus_id,)))
        self.assertEqual(sections["question"],"Official question text?")
        self.assertEqual(sections["answer"],"Official answer text.")
        self.assertEqual(self.con.execute("select publication_status from questions where corpus_id=?",(corpus_id,)).fetchone()[0],"pending")


if __name__ == "__main__":
    unittest.main()
