# Limitations and source behaviour

- Full-corpus enumeration means all records exposed by official interfaces plus a
  transparent gap register; it cannot establish that unavailable records do not exist.
- Lok Sabha material before the electronic Q&A service and historical Rajya Sabha
  sessions require reconciliation against official sitting/session records.
- English is the primary searchable layer. Hindi originals and metadata are retained,
  but complete Hindi OCR is not claimed.
- OCR, missing pages, identity conflicts, corrections and uncertain boundaries remain
  unpublished until reviewed.
- An unavailable expected per-sitting count remains `enumerated_not_reconciled` and is
  never interpreted as zero.

- A successful query does not establish completeness. Source indexing, missing text, migrations, unavailable documents, variations in wording and languages affect discovery.
- LS searches the DSpace general index; a returned item is not proof that a literal phrase occurs in a particular field. This release does not split LS document text into separate question and answer fields.
- Some RS answers are missing in the source API, so answer-only references may be missed even when all searches return successfully. The collector does not run a broad language-specific backfill for arbitrary topics.
- Official document links may fail or return non-PDF originals. Download failures appear in `unresolved.csv`; the tool does not manufacture replacement documents.
- Scans, ambiguous or multilingual documents and missing optional text extraction require review. The identity checker is conservative and reports uncertainty rather than forcing a match.
- Search response bodies are bounded to 16 MiB in memory. Oversized or malformed RS responses are partitioned by date down to a day; a still-failing partition remains failed. LS pagination uses 100 items per request. Network failures remain retryable.
- Raw evidence contains decoded response bodies as received by the HTTP client, gzip-compressed without changing their content. Checkpoint request records provide original URL, retrieval time and SHA-256. Private output folders may contain substantial material and are excluded from the source repository.
- Published historical results are not automatically reproduced: source records can change and previous research included manual, OCR and additional discovery work outside this general collector.
