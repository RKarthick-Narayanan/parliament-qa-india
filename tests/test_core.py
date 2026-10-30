import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parliament_qa.adapters import ls, rs
from parliament_qa.config import load_config
from parliament_qa.core import archive, create_jobs, db_open, download_one, enrich, export, file_hash, header_question_numbers, process_job, request_json, respect_retry_after, save_question, verify_text
from parliament_qa.matching import matches


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); self.con = db_open(self.root)
        self.cfg = {"version":1,"name":"test","houses":["RS"],"dates":{"from":"2013-08-12","to":"2013-08-12"},"search":{"terms":["endangered languages"]},"documents":{"download":False}}
        self.con.execute("INSERT INTO run VALUES(?,?,?)", (json.dumps(self.cfg),"abc","2026-01-01")); self.con.commit()

    def tearDown(self):
        self.con.close(); self.tmp.cleanup()

    def test_config_and_date(self):
        cfg = self.root/"topic.yaml"
        cfg.write_text('version: 1\nhouses: [RS]\ndates: {from: "2013-01-01", to: "2013-12-31"}\nsearch: {terms: [dialect]}\n', encoding="utf-8")
        self.assertEqual(load_config(cfg)[0]["search"]["terms"], ["dialect"])
        cfg.write_text('version: 1\nhouses: [RS]\ndates: {from: "2014-01-01", to: "2013-01-01"}\nsearch: {terms: [x]}', encoding="utf-8")
        with self.assertRaises(ValueError): load_config(cfg)

    def test_phrase_boundaries_and_formatting(self):
        self.assertEqual(matches('Endangered&lt;br/&gt;languages', 'endangered languages')[0], 1)
        self.assertEqual(matches('tribal languages and dialects', 'tribal language')[0], 0)
        self.assertEqual(matches('tribal languages and dialects', 'dialect')[0], 0)
        self.assertEqual(matches('endanger-\ned languages', 'endangered languages')[0], 1)

    def test_safe_like_literal(self):
        clause = rs.clause("people's 100%_dialect")
        self.assertIn("people''s", clause); self.assertIn("[%]", clause); self.assertIn("[_]", clause)

    def test_raw_evidence_lossless(self):
        raw = b'{ "x": [1, 2] }\n'
        path, digest = archive(self.root,self.con,"request","https://sansad.in/",raw,200)
        self.assertEqual(gzip.decompress((self.root/path).read_bytes()),raw)
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_question_identity_separate_houses(self):
        r = {"qslno":193630,"qno":688.0,"ses_no":229,"adate":"2013-08-12","qtitle":"endangered languages"}
        q = rs.question(r); save_question(self.con,q,"endangered languages",self.cfg)
        save_question(self.con,q,"languages",self.cfg)
        other = dict(q,key="LS:193630",house="LS"); save_question(self.con,other,"endangered languages",self.cfg)
        self.assertEqual(self.con.execute('select count(*) from questions').fetchone()[0],2)
        self.assertEqual(q["source_id"], "193630"); self.assertEqual(q["question_number"], "688")
        self.assertEqual(self.con.execute("select count(*) from issues where kind='answer_unavailable'").fetchone()[0],1)

    def test_pdf_identity_and_multi_page(self):
        q = {"question_number":"3332","question_type":"UNSTARRED","date":"2011-09-02","title":"LANGUAGES ON THE VERGE OF EXTINCTION"}
        text = "UNSTARRED QUESTION NO. 22\n02.09.2011 Other question\fUNSTARRED QUESTION NO. 3332\n02.09.2011 LANGUAGES ON THE VERGE OF EXTINCTION"
        status, evidence = verify_text(text,q); self.assertEqual(status,"verified_content"); self.assertEqual(json.loads(evidence)["page"],2)
        status, _ = verify_text("UNSTARRED QUESTION NO02.09.2011\nUnrelated",q)
        self.assertNotEqual(status,"verified_content")
        status, _ = verify_text("UNSTARRED QUESTION NO. 247\n02.09.2011",q)
        self.assertEqual(status,"question_number_mismatch")

    def test_question_header_dates_and_number_followed_by_date(self):
        self.assertEqual(header_question_numbers("QUESTION NO02.09.2011"), [])
        self.assertEqual(header_question_numbers("Question No. 02 / 09 / 2011"), [])
        self.assertEqual(header_question_numbers("QUESTION NUMBER 2549/04/08/2026"), ["2549"])
        self.assertEqual(header_question_numbers("QUESTION N0. 3332"), ["3332"])

    def test_ls_page_shape_and_pagination(self):
        data = {"_embedded":{"searchResult":{"_embedded":{"objects":[{}]},"page":{"totalPages":3}}}}
        self.assertEqual(ls.page(data)[1],3)
        with self.assertRaises(ValueError): ls.page({})

    def test_incomplete_enrichment_is_recorded(self):
        q = rs.question({"qslno":193630,"qno":688,"ses_no":229,"adate":"2013-08-12","qtitle":"endangered languages"})
        save_question(self.con,q,"endangered languages",self.cfg)
        with patch('parliament_qa.core.recover_rs', side_effect=ValueError('missing document metadata')):
            enrich(self.root,self.con,q)
        self.assertEqual(self.con.execute("select kind from issues where key=?", (q['key']+':enrich',)).fetchone()[0], 'enrichment_failed')

    def test_http_retry_and_archive(self):
        class Response:
            status_code = 200; headers = {}; url = 'https://sansad.in/test'
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def raise_for_status(self): pass
            def iter_content(self, size): yield b'{"ok":true}'
        import requests
        with patch('parliament_qa.core.requests.get', side_effect=[requests.Timeout('timeout'), Response()]) as get:
            with patch('parliament_qa.core.time.sleep'):
                data, _, path, _ = request_json(self.root,self.con,'test','https://sansad.in/test')
        self.assertTrue(data['ok']); self.assertEqual(get.call_count,2)
        self.assertTrue((self.root/path).is_file())

    def test_retry_after_does_not_retry_early(self):
        with self.assertRaisesRegex(ValueError, 'retry later'):
            respect_retry_after({'Retry-After': '120'})

    def test_ls_schedules_next_page(self):
        cfg = dict(self.cfg, houses=['LS'])
        create_jobs(self.con,cfg)
        job = self.con.execute("select * from jobs where house='LS'").fetchone()
        payload = {'_embedded':{'searchResult':{'_embedded':{'objects':[{'_embedded':{'indexableObject':{'uuid':'one','metadata':{'dc.date.issued':[{'value':'2013-08-12'}]}}}}]},'page':{'totalPages':2}}}}
        with patch('parliament_qa.core.request_json', return_value=(payload,'url','raw','hash')):
            process_job(self.root,self.con,cfg,job)
        self.assertEqual(self.con.execute("select page from jobs where state='pending' and house='LS'").fetchone()[0],1)

    def test_failed_json_and_resume_checkpoint(self):
        create_jobs(self.con,self.cfg)
        job = self.con.execute("select * from jobs").fetchone()
        with patch('parliament_qa.core.request_json', side_effect=RuntimeError('malformed or truncated JSON response')):
            process_job(self.root,self.con,self.cfg,job)
        self.assertEqual(self.con.execute('select state from jobs').fetchone()[0], 'failed')
        self.con.execute("update jobs set state='pending'"); self.con.commit()
        row = {"qslno":193630,"qno":688,"ses_no":229,"adate":"2013-08-12","qtitle":"endangered languages","ans_text":"answer"}
        with patch('parliament_qa.core.request_json', return_value=([row],"url","raw","hash")):
            process_job(self.root,self.con,self.cfg,self.con.execute('select * from jobs').fetchone())
        self.assertEqual(self.con.execute('select state from jobs').fetchone()[0], 'done')
        self.assertEqual(self.con.execute('select count(*) from questions').fetchone()[0],1)
        export(self.root,self.con); first=(self.root/'questions.csv').read_bytes()
        export(self.root,self.con); self.assertEqual(first,(self.root/'questions.csv').read_bytes())

    def test_existing_corrupt_download_retried(self):
        q = rs.question({"qslno":1,"qno":2,"ses_no":3,"adate":"2013-08-12","qtitle":"endangered languages"})
        save_question(self.con,q,"endangered languages",self.cfg)
        (self.root/'bad.pdf').write_bytes(b'corrupted')
        self.con.execute("insert into documents(key,question_key,url,language,name,state,path,sha256) values('x',?,'https://sansad.in/x.pdf','en','x.pdf','downloaded','bad.pdf','wrong')", (q["key"],)); self.con.commit()
        with patch('parliament_qa.core.requests.get', side_effect=ValueError('offline')):
            with patch('parliament_qa.core.time.sleep'):
                download_one(self.root,self.con,self.con.execute('select * from documents').fetchone())
        self.assertEqual(self.con.execute('select state from documents').fetchone()[0], 'failed')


if __name__ == '__main__': unittest.main()
