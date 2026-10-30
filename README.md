# Parliament Q&A collector

A small Python tool for collecting targeted Lok Sabha and Rajya Sabha questions and answers from official sources. Configure terms and dates in YAML, then save metadata, source evidence and, optionally, original documents.

It is designed for research discovery. Search results are candidates, sources differ, and no result count proves complete coverage. The first release searches English terms; it keeps available English and Hindi document links without claiming systematic Hindi search coverage. General debates and bills are outside its scope.

## Install

Python 3.11 or newer is required. Create and activate a virtual environment, then install the project:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows use `.venv\Scripts\activate` instead. `requests` and `PyYAML` are installed as dependencies. PDF text verification uses the optional `pdftotext` program from Poppler. Without it, PDFs are downloaded and labelled `not_checked`. Scans are labelled `needs_ocr`; OCR and browser link capture are not part of the core installation.

## Run

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

See [data model and sources](docs/data-model.md), [limitations](docs/limitations.md) and [verification](VERIFICATION.md). The package contains no corpus. The code licence does not grant rights to parliamentary source documents.

## Software and data rights

The [MIT licence](LICENSE) applies to this project's software. Lok Sabha and Rajya Sabha records and documents originate from their respective official publishing institutions; public accessibility does not automatically grant unrestricted redistribution rights. Consult the [Lok Sabha e-Library copyright policy](https://elibrary.sansad.in/copyright-policy) and the [Rajya Sabha Secretariat copyright policy](https://sansad.in/getFile/RSCMSNEW/careers_documents/1609857778468.37_copyright.pdf?source=rssecretariat), together with any applicable terms, before using or redistributing data. See [NOTICE.md](NOTICE.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for software provenance.

This project is independent of the Parliament of India and is not officially endorsed.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests are self-contained and do not require the existing research workspace or network access.
