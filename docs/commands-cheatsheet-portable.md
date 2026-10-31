# Portable Command Guide for New Users

This guide is for people cloning the repository on another computer. It uses repository-relative paths and does not assume the author's username, storage partition, existing databases, or local datasets.

## 1. Clone and enter the repository

```bash
git clone https://github.com/RKarthick-Narayanan/parliament-qa-india.git
cd parliament-qa-india
```

This downloads the source code and documentation from GitHub. Corpus databases, official PDFs, extracted text, and local analysis outputs are intentionally not included.

## 2. Install system dependencies

### Ubuntu, Debian, or WSL

```bash
sudo apt update
sudo apt install -y python3 python3-venv poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin
```

This installs Python, PDF text/rendering tools, and English/Hindi OCR support. Windows users should preferably run these commands inside WSL rather than native Command Prompt.

### macOS with Homebrew

```bash
brew install python poppler tesseract tesseract-lang
```

This installs the corresponding command-line dependencies on macOS. Confirm that `pdftotext`, `pdftoppm`, and `tesseract` are available afterward.

## 3. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[corpus]'
```

This creates an isolated environment and installs the project in editable mode with Parquet support. Activate `.venv` again whenever opening a new terminal.

## 4. Verify the installation

```bash
parliament-qa --help
command -v pdftotext
command -v pdftoppm
command -v tesseract
python3 -m unittest discover -s tests -v
```

These commands verify the CLI, PDF/OCR programs, and project tests. Resolve installation errors before starting a large collection.

## 5. Run a small official-source test

```bash
parliament-qa run \
  --config topics/smoke_ls.yaml \
  --output results/smoke-ls

parliament-qa run \
  --config topics/smoke_rs.yaml \
  --output results/smoke-rs
```

These configurations perform small targeted searches before committing time or storage to a larger run. Internet access and availability of the official parliamentary services are required.

## 6. Run a configured targeted search

```bash
parliament-qa run \
  --config topics/endangered_languages.yaml \
  --output results/language-search
```

This searches the configured Houses, date ranges, and English terms while archiving source evidence. Stop with `Ctrl+C` and add `--resume` to the same command to continue its checkpoint.

## 7. Build a canonical corpus from official inventories

The LS and RS corpora are kept separate so that coverage, review status, and exports remain auditable by House.

### Inventory Lok Sabha

```bash
parliament-qa inventory \
  --output results/lok-sabha-corpus \
  --house LS
```

This enumerates available official Lok Sabha records and creates a canonical SQLite database. Historical gaps remain explicit rather than being interpreted as zero questions.

### Inventory Rajya Sabha

```bash
parliament-qa inventory \
  --output results/rajya-sabha-corpus \
  --house RS
```

This enumerates available official Rajya Sabha records using bounded date requests. Source failures and unavailable historical periods are recorded in the issue and coverage tables.

### Seed a corpus from a local manifest

```bash
parliament-qa inventory \
  --output results/rajya-sabha-corpus \
  --no-network \
  --house RS \
  --seed-storage reference \
  --seed-manifest /absolute/path/to/manifest.csv
```

This imports an existing CSV inventory without copying large local documents. Replace the manifest path and House with values appropriate to the supplied dataset.

## 8. Process each canonical corpus

Run the following sequence separately with `CORPUS` set to either `results/lok-sabha-corpus` or `results/rajya-sabha-corpus`.

```bash
CORPUS="results/lok-sabha-corpus"

parliament-qa acquire --output "$CORPUS"
parliament-qa extract --output "$CORPUS" --workers 4
parliament-qa ocr-hindi --output "$CORPUS" --workers 4
parliament-qa segment --output "$CORPUS"
parliament-qa index --output "$CORPUS"
parliament-qa audit --output "$CORPUS"
parliament-qa export-corpus --output "$CORPUS"
```

This sequence downloads originals, extracts page text, adds Hindi OCR variants, segments Q&A, builds FTS5 indexes, audits quality, and exports reusable files. Each stage is checkpointed or idempotent, but unresolved downloads and review flags must still be examined.

## 9. Record a manual review

```bash
parliament-qa review \
  --output results/rajya-sabha-corpus \
  --corpus-id RS-REPLACE-WITH-REAL-ID \
  --reviewer "Reviewer Name" \
  --decision approved \
  --reason "Verified against official evidence"
```

This records an attributable, immutable review decision without deleting extraction evidence. Valid decisions are `approved`, `rejected`, and `needs_revision`.

## 10. Export text collections for Voyant

```bash
parliament-qa export-voyant \
  --output results/voyant \
  --corpus results/lok-sabha-corpus \
  --corpus results/rajya-sabha-corpus
```

This creates questions-only, answers-only, and complete-Q&A UTF-8 text collections. The accompanying metadata connects each file to its House, date, question identity, and corpus record.

## 11. Run exact survey and language-census retrieval

Choose a data location with at least 10 GiB free and keep it outside the Git repository.

```bash
CONCEPT_WORKSPACE="../parliament-data/survey-census-1999-2026"

parliament-qa discover-concepts \
  --output "$CONCEPT_WORKSPACE" \
  --config config/survey_census_concepts.yaml \
  --house LS \
  --house RS
```

This performs the resumable year/session discovery using the versioned exact-English concept rules. Raw responses, coverage, candidates, exact hits, and issues are stored in the external workspace.

### Verify staged candidates

```bash
parliament-qa verify-concepts \
  --output "$CONCEPT_WORKSPACE" \
  --workers 4
```

This downloads offered originals and classifies candidates as verified, excluded, or needing review. It does not modify either canonical corpus.

### Promote verified candidates

```bash
parliament-qa promote-concepts \
  --output "$CONCEPT_WORKSPACE" \
  --ls-corpus results/lok-sabha-corpus \
  --rs-corpus results/rajya-sabha-corpus \
  --voyant-output results/voyant \
  --workers 4
```

This merges only identity-verified candidates and preserves staged evidence for all other decisions. It also refreshes downstream extraction, segmentation, indexes, audits, corpus exports, and Voyant output.

## 12. Create a reproducible representative sample

```bash
python3 scripts/sample_qa.py \
  --size 40 \
  --seed 20261031 \
  --ls-db results/lok-sabha-corpus/corpus.sqlite3 \
  --rs-db results/rajya-sabha-corpus/corpus.sqlite3 \
  --output analysis/representative-sample
```

This selects 20 publication-ready records per House using period and question-type strata. The fixed seed, probabilities, weights, selected IDs, and input hashes make the sample reproducible.

## 13. Inspect outputs

Canonical corpus exports appear under each corpus folder's `exports/` directory.

```text
exports/
├── questions.csv
├── questions.jsonl
├── questions.parquet
├── pages.jsonl
├── page_text_variants.jsonl
├── documents.csv
├── document_versions.csv
├── coverage.csv
├── issues.csv
└── manifest.json
```

`questions.*` contains one record per canonical parliamentary question, while `pages.jsonl` preserves page-level traceability. Do not interpret a successfully extracted targeted collection as complete coverage of every parliamentary question.

## 14. Storage and interruption guidance

- Keep `results/`, downloaded documents, raw responses, SQLite databases, and analysis outputs outside version control.
- Allow substantial free space for official PDFs, raw evidence, OCR, and exports.
- Stop long stages with `Ctrl+C`; rerun the same command and output path to resume.
- Do not run two writers against the same SQLite corpus simultaneously.
- Back up canonical databases and manifests before relocating or deleting source files.

These practices protect provenance and reduce the risk of incomplete or corrupted checkpoints. Large collections should preferably use a dedicated data partition or external volume.

## 15. Git contribution workflow

```bash
git status
python3 -m unittest discover -s tests -v
git diff --check
git switch -c feature-name
git add README.md docs config scripts src tests pyproject.toml .gitignore
git diff --cached --name-only
git commit -m "Describe the change"
git push -u origin feature-name
```

This workflow tests and inspects source changes before publishing them. Never stage corpus results, raw responses, official documents, extracted text, local analysis, credentials, or SQLite databases.

## Data and rights note

The repository publishes tooling and schemas rather than redistributing the complete internal corpus. Users remain responsible for official-source terms, document rights, citation, privacy, and any redistribution review.
