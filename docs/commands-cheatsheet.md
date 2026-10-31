# Parliament Q&A Command Cheat Sheet

Run project commands from:

```bash
cd "/home/karthick/Documents/EL in Parliment/parliament-qa-india-github"
```

Paths below use the current LS, RS, Voyant, and external concept-discovery locations. Commands that contact official sources require internet access, while corpus processing commands work from acquired local files.

## Environment and help

### Activate the existing environment

```bash
source .venv/bin/activate
```

This activates the Python environment containing `parliament-qa` and its dependencies. Run it once in every new terminal before using project commands.

### Install or refresh the project

```bash
python3 -m pip install -e '.[corpus]'
```

This installs the project in editable mode with Parquet export support. Code changes become available without rebuilding the package.

### Display command help

```bash
parliament-qa --help
parliament-qa extract --help
```

The first command lists every available stage. The second pattern displays options for any selected subcommand.

## Legacy targeted collector

### Start a configured targeted collection

```bash
parliament-qa run \
  --config topics/endangered_languages.yaml \
  --output results/languages
```

This searches the sources and terms defined in the supplied YAML configuration. It creates a checkpointed targeted collection in the output folder.

### Resume a targeted collection

```bash
parliament-qa run \
  --config topics/endangered_languages.yaml \
  --output results/languages \
  --resume
```

This continues an interrupted targeted collection using its saved checkpoint. The configuration must match the configuration used to create that checkpoint.

### Download targeted-collection documents

```bash
parliament-qa download --output results/languages
```

This downloads pending official documents registered by the targeted collector. Completed downloads are checkpointed, so the command can be rerun safely.

### Inspect targeted-collection status

```bash
parliament-qa status --output results/languages
```

This summarizes the targeted checkpoint, records, documents, and unresolved jobs. It does not acquire or modify source material.

### Export a targeted collection

```bash
parliament-qa export --output results/languages
```

This regenerates the targeted collection's manifests and tabular outputs. When the output contains a canonical corpus database, the same command acts as a corpus export alias.

## Canonical full-text corpus pipeline

Use `results/lok-sabha-matching-corpus` or `results/rajya-sabha-matching-corpus` as `CORPUS` below.

### Inventory official records

```bash
parliament-qa inventory \
  --output CORPUS \
  --house LS
```

This enumerates official questions for the selected House and inserts stable canonical identities. Repeat `--house` for both Houses or omit it to use both by default.

### Import a local manifest without internet

```bash
parliament-qa inventory \
  --output CORPUS \
  --no-network \
  --house RS \
  --seed-storage reference \
  --seed-manifest /path/to/manifest.csv
```

This imports metadata and document references from a local CSV without contacting official websites. `reference` avoids copying large source files, while `hardlink` and `copy` provide alternative storage modes.

### Import only matching-question manifest rows

```bash
parliament-qa inventory \
  --output CORPUS \
  --no-network \
  --seed-manifest /path/to/document_manifest.csv \
  --seed-corpus-role matching_question \
  --seed-storage reference
```

This accepts only rows whose `corpus_role` equals `matching_question`. Auxiliary or excluded documents remain outside the canonical question inventory.

### Acquire official documents

```bash
parliament-qa acquire --output CORPUS
```

This discovers and downloads pending official originals while recording provenance and checksums. Existing successful downloads are reused.

### Refresh official documents

```bash
parliament-qa acquire --output CORPUS --refresh
```

This re-fetches registered URLs and preserves changed bytes as new document versions. Use it only when checking for corrected or revised official files.

### Extract English text and OCR

```bash
parliament-qa extract --output CORPUS --workers 4
```

This extracts embedded page text and applies English OCR only to pages that require it. The stage is resumable and skips versions that already have page records.

### Extract without OCR

```bash
parliament-qa extract --output CORPUS --no-ocr --workers 4
```

This extracts embedded text but does not run English OCR on sparse or image-only pages. Those pages are retained and flagged for review instead.

### Add Hindi OCR variants

```bash
parliament-qa ocr-hindi --output CORPUS --workers 4
```

This adds non-destructive Hindi OCR variants for candidate Devanagari pages. It never overwrites embedded or English page text.

### Force Hindi OCR for all pages

```bash
parliament-qa ocr-hindi --output CORPUS --workers 4 --all-pages
```

This applies Hindi OCR even when a page already contains usable embedded Devanagari. Use it for OCR comparison or quality auditing rather than routine processing.

### Segment questions and answers

```bash
parliament-qa segment --output CORPUS
```

This creates cleaned question, answer, annexure, correction, and full-document sections. It automatically approves only records that satisfy the born-digital, identity, and boundary checks.

### Build the search index

```bash
parliament-qa index --output CORPUS
```

This rebuilds SQLite FTS5 indexes over metadata, structured sections, and full-document text. Search results remain traceable to their canonical question and page evidence.

### Audit corpus quality

```bash
parliament-qa audit --output CORPUS
```

This reports question, document, download, extraction, review, and publication-ready counts. A non-zero exit status means acceptance conditions still have unresolved issues.

### Export canonical corpus files

```bash
parliament-qa export-corpus --output CORPUS
```

This generates CSV, JSONL, Parquet, page-level evidence, document manifests, and issue reports. It does not download or re-extract documents.

### Record a human review

```bash
parliament-qa review \
  --output CORPUS \
  --corpus-id RS-EXAMPLE \
  --reviewer "Reviewer Name" \
  --decision approved \
  --reason "Checked against official evidence"
```

This records an immutable approval, rejection, or needs-revision decision with reviewer and reason. Use `--version-id` when the decision applies to a specific document version.

### Export Voyant collections

```bash
parliament-qa export-voyant \
  --output results/voyant \
  --corpus results/lok-sabha-matching-corpus \
  --corpus results/rajya-sabha-matching-corpus
```

This produces questions-only, answers-only, and complete-Q&A UTF-8 text collections. It also writes metadata linking every text file to its canonical corpus record.

## Survey and language-census retrieval

### Discover exact concept matches

```bash
parliament-qa discover-concepts \
  --output /media/karthick/Apps1/parliament-corpus/survey-census-1999-2026 \
  --config config/survey_census_concepts.yaml \
  --house LS \
  --house RS
```

This searches every available year and official session using the configured exact English phrase families. Raw responses, request checksums, coverage, candidates, hits, and issues are checkpointed externally.

### Verify concept candidates

```bash
parliament-qa verify-concepts \
  --output /media/karthick/Apps1/parliament-corpus/survey-census-1999-2026 \
  --workers 4
```

This downloads candidate originals, checks exact document matches, and verifies question identity. Candidates are classified as verified, excluded, or requiring review without changing the canonical corpora.

### Promote verified concept candidates

```bash
parliament-qa promote-concepts \
  --output /media/karthick/Apps1/parliament-corpus/survey-census-1999-2026 \
  --ls-corpus results/lok-sabha-matching-corpus \
  --rs-corpus results/rajya-sabha-matching-corpus \
  --voyant-output results/voyant \
  --profile-script analysis/01_corpus_profile/profile.py \
  --workers 4
```

This merges only identity-verified candidates and deduplicates them against existing canonical records. It then refreshes extraction, segmentation, indexes, audits, exports, Voyant files, and the local profile.

## Standalone research utilities

### Create a representative 40-question sample

```bash
python scripts/sample_qa.py \
  --size 40 \
  --seed 20261031 \
  --output analysis/representative_sample
```

This selects 20 publication-ready LS and 20 publication-ready RS questions using reproducible stratified sampling. It writes CSV, JSONL, readable TXT, and a sampling manifest under the ignored local `analysis/` directory.

### Collect historical Rajya Sabha candidates

```bash
python scripts/collect_rs_historical.py \
  --output /media/karthick/Apps1/parliament-corpus/rs-historical \
  --from-year 1999 \
  --to-year 2002 \
  --workers 4
```

This runs bounded official RS term searches for the selected historical years and reapplies local exact matching. It archives responses and exports candidate, seed, request, and collection reports.

### Generate the local corpus profile

```bash
python analysis/01_corpus_profile/profile.py
```

This reads the current LS and RS exports and regenerates local descriptive reports. The entire `analysis/` directory is ignored and must not be pushed to Git.

## Testing and validation

### Run all unit and integration tests

```bash
python -m unittest discover -s tests -v
```

This runs collector, corpus, concept-retrieval, export, resume, and sampling tests. Run it before committing code changes.

### Check Python syntax

```bash
python -m py_compile \
  src/parliament_qa/*.py \
  scripts/sample_qa.py \
  scripts/collect_rs_historical.py
```

This compiles the Python sources without running the collection pipeline. It catches syntax and import-time parsing errors quickly.

### Check the working tree for whitespace errors

```bash
git diff --check
```

This reports trailing whitespace and malformed patch lines in tracked changes. It does not modify files.

## External storage maintenance

The migration utility is machine-specific and is intentionally excluded from Git.

### Preview workspace migration

```bash
bash scripts/migrate_workspace_to_apps.sh --dry-run
```

This lists files that would move to the Apps partition without changing anything. It also verifies the mount type and available free space.

### Execute or resume workspace migration

```bash
bash scripts/migrate_workspace_to_apps.sh --execute
```

This copies and checksum-verifies selected data before replacing original locations with symlinks. Stop all corpus writers first, and keep the Apps partition mounted throughout the operation.

## Git-safe publishing

### Inspect changes

```bash
git status
git diff --stat
```

These commands show tracked modifications and untracked source files before staging. Generated analysis, corpus results, databases, PDFs, spreadsheets, and raw text should remain ignored.

### Stage project code and documentation

```bash
git add \
  .gitignore \
  README.md \
  docs \
  pyproject.toml \
  config \
  scripts \
  src \
  tests
```

This stages reusable code, configuration, documentation, and tests. Ignore rules prevent local analysis, external result links, raw files, and the machine-specific migration script from being staged.

### Verify staged content

```bash
git status
git diff --cached --stat
git diff --cached --name-only
```

These commands show exactly what will enter the commit. Stop if the list contains `analysis`, `results`, databases, PDFs, CSVs, JSONL, spreadsheets, or extracted text.

### Commit and push

```bash
git commit -m "Add full-text corpus and retrieval tooling"
git push origin main
```

The first command records the staged source changes in local Git history. The second publishes that commit to the configured `origin` repository.

