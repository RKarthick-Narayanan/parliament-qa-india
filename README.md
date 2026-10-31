# Parliament Q&A collector and full-text corpus

A small Python tool for collecting targeted Lok Sabha and Rajya Sabha questions and answers from official sources. Configure terms and dates in YAML, then save metadata, source evidence and, optionally, original documents.

It is designed for research discovery. Search results are candidates, sources differ, and no result count proves complete coverage. The first release searches English terms; it keeps available English and Hindi document links without claiming systematic Hindi search coverage. General debates and bills are outside its scope.

The package also provides a separate, resumable full-corpus workflow for official
questions dated 1999-01-01 through the frozen 2026-10-30 cutoff. It stores immutable
source versions, page text and cleaned sections in `corpus.sqlite3`; existing targeted
search checkpoints remain compatible and separate.

## Install

Python 3.11 or newer is required. Create and activate a virtual environment, then install the project:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows use `.venv\Scripts\activate` instead. `requests` and `PyYAML` are installed as dependencies. PDF text verification uses the optional `pdftotext` program from Poppler. Without it, PDFs are downloaded and labelled `not_checked`. Scans are labelled `needs_ocr`; OCR and browser link capture are not part of the core installation.

The full corpus additionally needs Poppler and Tesseract command-line tools. Install
the Parquet dependency with `python -m pip install '.[corpus]'`. English is the primary
extraction language; Hindi originals and metadata are retained without claiming
complete Hindi OCR.

## Run

See the [portable command guide](docs/commands-cheatsheet-portable.md) for
installation, collection, corpus processing, review, export and contribution
commands.

Copy and edit `topics/endangered_languages.yaml`. Dates are inclusive. To collect metadata before downloading documents, set `documents.download: false`:

```bash
parliament-qa run --config topics/endangered_languages.yaml --output results/languages
parliament-qa status --output results/languages
parliament-qa download --output results/languages
parliament-qa export --output results/languages
```

Run a small example first: `topics/smoke_ls.yaml` and `topics/smoke_rs.yaml` each select a single day and term. Results depend on the availability and current contents of official systems.

Press Ctrl+C to interrupt a run. Resume with the same configuration and output folder:

```bash
parliament-qa run --config topics/endangered_languages.yaml --output results/languages --resume
```

Changing the configuration requires a new output folder. Resume retries failed search requests and incomplete enrichment. `download` retries failed documents and validates hashes of existing downloads. A non-zero exit code indicates incomplete search, enrichment or downloads; see `coverage.csv` and `unresolved.csv`. A source can return no results or omit text even when a request succeeds.

## Output

The output folder contains `questions.csv`, `matches.csv`, `documents.csv`, `coverage.csv`, `unresolved.csv`, `run.json`, `checkpoint.sqlite3`, compressed source responses in `raw/`, and downloaded originals in `documents/`. The raw gzip files retain the exact decoded HTTP response body; `requests` in the checkpoint records URL, time and SHA-256. Document paths are relative to the output folder.

`source_returned` in `matches.csv` means the search service returned a candidate. `literal_verified` means the requested phrase was found in a returned field after normalization; `occurrences` counts that field's matches. A downloaded PDF is not automatically a verified question association. `documents.csv` records download and identity status separately.

See the [data model diagram and sources](docs/data-model.md#data-model), [limitations](docs/limitations.md) and [verification](VERIFICATION.md). The package contains no corpus. The code licence does not grant rights to parliamentary source documents.

## Full corpus workflow

Run each idempotent stage against the same output directory:

```bash
parliament-qa inventory --output results/full-corpus
parliament-qa acquire --output results/full-corpus
parliament-qa extract --output results/full-corpus
parliament-qa segment --output results/full-corpus
parliament-qa index --output results/full-corpus
parliament-qa audit --output results/full-corpus
parliament-qa export --output results/full-corpus
```

`inventory` enumerates Lok Sabha's official Part 1 collection by year and Rajya Sabha
by date ranges, archiving exact source responses. Existing CSV inventories can be
imported without network access; referenced files are copied into checksum-addressed
corpus storage:

Use `--house RS` or `--house LS` to limit a live enumeration to one House.
For manifests containing discovery or auxiliary documents, use
`--seed-corpus-role matching_question` to import only the finalized matching set.
Use `--seed-storage hardlink` for large local collections on the same filesystem;
this avoids duplicating PDF data while keeping checksum-addressed corpus paths.

```bash
parliament-qa inventory --output results/full-corpus --no-network \
  --seed-manifest '/path/to/ls/Final_1999_2026_pdf_inventory.csv' \
  --seed-manifest '/path/to/rajya_sabha_questions.csv'
```

The House is inferred from `lok`, `ls`, `rajya`, or an `rs` path component. A generic
manifest must contain a `house` or `House` column. `acquire` resolves Lok Sabha original
bitstreams and downloads pending files; `--refresh` preserves changed content as a new
version. `extract` uses embedded page text first and English OCR for sparse pages;
`--no-ocr` flags them instead. Use `extract --workers N` to run Poppler/OCR work in
parallel while database writes remain serialized. `segment` creates full-document, question, answer,
annexure and correction sections. Only unambiguous born-digital records are approved
automatically.

`ocr-hindi` adds `hin+eng` Tesseract output as a separate page-text variant without
overwriting embedded or English text. By default it processes Hindi-labelled pages
with fewer than 20 embedded Devanagari characters; `--all-pages` forces every Hindi
page. Rerun `index` and `export` afterward to update page search and
`page_text_variants.jsonl`.

Record a manual decision without changing immutable source or page text:

```bash
parliament-qa review --output results/full-corpus --corpus-id LS-... \
  --reviewer initials --decision approved --reason 'PDF and section boundaries verified'
```

`index` rebuilds the SQLite FTS5 table. `audit` exits non-zero while downloads,
unextracted PDFs, documentless questions or open review issues remain. `export` writes
question JSONL/CSV/Parquet, page JSONL, document/version manifests, corrections,
supplementary links, coverage and issue reports under `exports/`.

Create combined UTF-8 TXT collections for Voyant Tools from completed House corpora:

```bash
parliament-qa export-voyant --output results/voyant \
  --corpus results/lok-sabha-matching-corpus \
  --corpus results/rajya-sabha-matching-corpus
```

This writes `questions_only`, `answers_only`, and `complete_qa` collections, each
split into `lok_sabha` and `rajya_sabha`, plus `metadata.csv`. The metadata labels
the records as targeted search questions; the command does not claim complete
enumeration of every parliamentary question in the date range.

The full text remains internal by default. Releasing official files or extracted text
requires a separate rights review.

## Software and data rights

The [MIT licence](LICENSE) applies to this project's software. Lok Sabha and Rajya Sabha records and documents originate from their respective official publishing institutions; public accessibility does not automatically grant unrestricted redistribution rights. Consult the [Lok Sabha e-Library copyright policy](https://elibrary.sansad.in/copyright-policy) and the [Rajya Sabha Secretariat copyright policy](https://sansad.in/getFile/RSCMSNEW/careers_documents/1609857778468.37_copyright.pdf?source=rssecretariat), together with any applicable terms, before using or redistributing data. See [NOTICE.md](NOTICE.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for software provenance.

This project is independent of the Parliament of India and is not officially endorsed.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests are self-contained and do not require the existing research workspace or network access.
