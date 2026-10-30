# Verification report

Verification performed on 2026-10-30 UTC with Python 3.12 on Linux. The public project was installed in a new virtual environment using `python -m pip install .`. No research databases or PDFs were required by the installed code.

## Automated checks

`python -m unittest discover -s tests -v`: 13 tests passed. They cover configuration, literal term boundaries, safe query construction, compressed raw evidence, source identity, partial enrichment, retries, LS pagination, malformed RS data, PDF identity evidence, corrupt downloads and checkpoint/export behaviour.

## Live official-source checks

Both smoke configurations use metadata-only collection first; `download` is run separately. Outputs were kept in temporary directories outside this repository.

```bash
parliament-qa run --config topics/smoke_ls.yaml --output /tmp/parliament-qa-release-ls
parliament-qa run --config topics/smoke_rs.yaml --output /tmp/parliament-qa-release-rs
parliament-qa download --output /tmp/parliament-qa-release-ls
parliament-qa download --output /tmp/parliament-qa-release-rs
```

- LS: `eighth schedule`, 2000-03-07. The source returned 18 indexed results for 2000; local date filtering retained one question: Q1862, “Dogri Language in Eighth Schedule”, item `bc1074e2-0ee0-4b57-9c43-3d1d0ff7291a`. The official PDF was downloaded and `verified_content`; printed number 1862 and date 2000-03-07 were found on page 1. Its SHA-256 `d5cb02cdca7ab7af0cfb2a3d4959188b1ac369b0c14781d635286c9c413387ad` agrees with the existing LS research inventory.
- RS: `endangered languages`, 2013-08-12. The API returned one question: legacy ID 193630, unstarred Q688, “Expert committee on endangered languages”. The migrated official PDF downloaded with SHA-256 `07cae9f36e14045d322de44147bcc39c96f5fb20cb77e67b9db9911e7635b694`. The package reported `needs_ocr`. The hash agrees with the existing research manifest, which separately records OCR evidence of Q688 and the title on page 1; the package itself did not perform that OCR check. An official Hindi DOCX also downloaded.
- The older English HTML URL for the RS question returned HTTP 500. It is recorded as a failed document and the download command returned exit code 1. The PDF and DOCX successes do not erase that failure.

The official LS collection API identified scope `75228d43-3a98-4b7d-b90f-ffec6aa9fa11` as `Part 1(Questions And Answers)` during this check.

## Resume and export

A bounded RS collection was interrupted after 0.5 seconds using `timeout --signal=INT 0.5 ...`, then resumed with the same `--config` and `--resume`. It completed with one question, four matches and no duplicate question. A completed LS run was resumed inside the network-restricted sandbox; it reused its checkpoint without another search. A subsequent LS download used the existing checksum-verified PDF without a network request. Both House exports were reproduced offline; the question and document CSV SHA-256 values were unchanged. `run.json` records a new export timestamp and is intentionally not byte-identical.

## Practical limits

These are small checks, not a complete historical crawl. RS answer-only matches may be missed if the source does not return answer text; DSpace indexed fields are not disclosed. Scanned documents still need OCR or human review for an independent package-level identity verdict. Source availability and results can change.

## Attribution and release-hygiene follow-up

On 2026-10-30, the previously similar question-number/date safeguard was replaced with a separately written calendar-date check after the original zero-hour licence could not be independently established. The Commoner-probe MIT notice was added as a precaution, and its text was checked against the saved upstream licence. Fourteen tests passed against the built wheel; the revised code also re-verified the previously downloaded LS Q1862 PDF. The wheel contains the project licence, provenance notice and third-party notice.

The official LS and RS policy links returned HTTP 200 during the check. A representative `git check-ignore` test confirmed that checkpoints, raw responses, downloaded documents, extracted text folders and common credential files match ignore rules. A file-list review and a pattern-based secret scan found no obvious corpus files or credentials in the intended source files; a pattern scan cannot prove all sensitive data is absent. The project directory was not a Git repository, so there were no locally accessible tracked files, staged changes or Git history to examine. Repeat those checks on any actual repository before publication.
