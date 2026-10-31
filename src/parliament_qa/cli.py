"""Command line interface for independent collection folders."""
import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .concepts import discover_concepts, promote_concepts, verify_concepts
from .core import collect, db_open, download, export, jsonstr, now
from .corpus import (acquire as corpus_acquire, add_review, audit as corpus_audit,
                     build_index as corpus_index, corpus_open,
                     export_corpus, export_voyant, extract as corpus_extract,
                     inventory as corpus_inventory, ocr_hindi,
                     segment as corpus_segment)


CORPUS_COMMANDS = ("inventory", "acquire", "extract", "ocr-hindi", "segment", "index", "audit", "export-corpus", "review")


def inferred_house(path: Path) -> str:
    name = str(path).casefold()
    if "rajya" in name or "/rs/" in name or path.name.casefold().startswith("rs_"):
        return "RS"
    if "lok" in name or "/ls/" in name or path.name.casefold().startswith("ls_"):
        return "LS"
    return ""


def main():
    parser = argparse.ArgumentParser(prog="parliament-qa")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--resume", action="store_true")
    for command in ("download", "status", "export"):
        p = sub.add_parser(command); p.add_argument("--output", required=True, type=Path)
    inv = sub.add_parser("inventory", help="enumerate official Q&A and/or seed local manifests")
    inv.add_argument("--output", required=True, type=Path)
    inv.add_argument("--seed-manifest", action="append", type=Path, default=[])
    inv.add_argument("--no-network", action="store_true", help="only import supplied local manifests")
    inv.add_argument("--house", action="append", choices=("LS","RS"), help="limit live enumeration; repeat for both Houses")
    inv.add_argument("--seed-corpus-role", help="import only manifest rows with this corpus_role")
    inv.add_argument("--seed-storage",choices=("copy","hardlink","reference"),default="copy",
                     help="how local seed files are stored (default: copy)")
    for command in ("segment", "index", "audit", "export-corpus"):
        p = sub.add_parser(command); p.add_argument("--output", required=True, type=Path)
    voyant = sub.add_parser("export-voyant", help="combine corpus databases into Voyant TXT collections")
    voyant.add_argument("--output", required=True, type=Path)
    voyant.add_argument("--corpus", required=True, action="append", type=Path,
                        help="corpus output folder; repeat for each House")
    acq = sub.add_parser("acquire")
    acq.add_argument("--output", required=True, type=Path)
    acq.add_argument("--refresh", action="store_true", help="re-fetch downloaded URLs and preserve changed versions")
    ext = sub.add_parser("extract")
    ext.add_argument("--output", required=True, type=Path)
    ext.add_argument("--no-ocr", action="store_true", help="flag sparse pages instead of invoking English OCR")
    ext.add_argument("--workers", type=int, default=1, help="parallel PDF/OCR workers (default: 1)")
    hin = sub.add_parser("ocr-hindi", help="add non-destructive Hindi OCR page variants")
    hin.add_argument("--output", required=True, type=Path)
    hin.add_argument("--workers",type=int,default=3)
    hin.add_argument("--all-pages",action="store_true",help="OCR even pages with usable embedded Devanagari")
    review = sub.add_parser("review", help="record an immutable human review decision")
    review.add_argument("--output", required=True, type=Path)
    review.add_argument("--corpus-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", required=True, choices=("approved","rejected","needs_revision"))
    review.add_argument("--reason", required=True)
    review.add_argument("--version-id", default="")
    discover = sub.add_parser("discover-concepts", help="stage exact survey/census candidates from official sources")
    discover.add_argument("--output", required=True, type=Path)
    discover.add_argument("--config", required=True, type=Path)
    discover.add_argument("--house", action="append", choices=("LS", "RS"),
                          help="limit discovery to a House; repeat for both")
    verify = sub.add_parser("verify-concepts", help="download and identity-check staged concept candidates")
    verify.add_argument("--output", required=True, type=Path)
    verify.add_argument("--workers", type=int, default=3)
    promote = sub.add_parser("promote-concepts", help="merge verified candidates and rebuild corpus outputs")
    promote.add_argument("--output", required=True, type=Path)
    promote.add_argument("--ls-corpus", required=True, type=Path)
    promote.add_argument("--rs-corpus", required=True, type=Path)
    promote.add_argument("--voyant-output", required=True, type=Path)
    promote.add_argument("--profile-script", type=Path)
    promote.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    root = args.output.resolve()
    try:
        if args.command == "discover-concepts":
            summary = discover_concepts(root, args.config.resolve(), tuple(args.house or ("LS", "RS")))
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        if args.command == "verify-concepts":
            summary = verify_concepts(root, workers=args.workers)
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        if args.command == "promote-concepts":
            summary = promote_concepts(
                root, args.ls_corpus.resolve(), args.rs_corpus.resolve(),
                args.voyant_output.resolve(),
                args.profile_script.resolve() if args.profile_script else None,
                workers=args.workers,
            )
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        if args.command == "export-voyant":
            summary = export_voyant(root, [path.resolve() for path in args.corpus])
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        if args.command in CORPUS_COMMANDS or (args.command == "export" and (root/"corpus.sqlite3").exists()):
            con = corpus_open(root)
            if args.command == "inventory":
                manifests = [(path.resolve(), inferred_house(path)) for path in args.seed_manifest]
                missing = [str(path) for path, _ in manifests if not path.is_file()]
                if missing:
                    raise ValueError("seed manifest does not exist: " + ", ".join(missing))
                summary = corpus_inventory(root, con, manifests, network=not args.no_network,
                                           houses=tuple(args.house or ("LS","RS")),
                                           seed_role=args.seed_corpus_role or "",
                                           seed_storage=args.seed_storage)
            elif args.command == "acquire":
                summary = corpus_acquire(root, con, refresh=args.refresh)
            elif args.command == "extract":
                summary = corpus_extract(root, con, use_ocr=not args.no_ocr, workers=args.workers)
            elif args.command == "ocr-hindi":
                summary = ocr_hindi(root,con,workers=args.workers,all_pages=args.all_pages)
            elif args.command == "segment":
                summary = corpus_segment(root, con)
            elif args.command == "index":
                summary = corpus_index(con)
            elif args.command == "audit":
                summary = corpus_audit(con)
            elif args.command == "review":
                if not con.execute("SELECT 1 FROM questions WHERE corpus_id=?",(args.corpus_id,)).fetchone():
                    raise ValueError("unknown corpus ID: " + args.corpus_id)
                summary = {"review_id":add_review(con,args.corpus_id,args.reviewer,args.decision,args.reason,args.version_id)}
            else:
                summary = export_corpus(root, con)
            con.close()
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 1 if args.command == "audit" and not summary["acceptance_passed"] else 0
        cfg, config_hash = load_config(args.config) if args.command == "run" else (None, None)
        if args.command != "run" and not (root/"checkpoint.sqlite3").exists():
            raise ValueError("output folder does not contain a checkpoint")
        if args.command == "run" and args.resume and not (root/"checkpoint.sqlite3").exists():
            raise ValueError("no checkpoint to resume")
        con = db_open(root)
        previous = con.execute("SELECT * FROM run LIMIT 1").fetchone()
        if args.command == "run":
            if previous and not args.resume:
                raise ValueError("output already contains a run; use --resume or another folder")
            if previous and previous["config_hash"] != config_hash:
                raise ValueError("configuration differs from the existing checkpoint")
            if not previous:
                con.execute("INSERT INTO run VALUES(?,?,?)", (jsonstr(cfg),config_hash,now()));con.commit()
            collect(root, con, cfg, args.resume)
            if cfg["documents"]["download"]:
                download(root, con)
        elif not previous:
            raise ValueError("checkpoint has no run configuration")
        elif args.command == "download":
            download(root, con)
        summary = export(root, con)
        print(json.dumps(summary, indent=2))
        failures = con.execute("SELECT count(*) FROM jobs WHERE state IN ('pending','failed')").fetchone()[0]
        doc_failed = con.execute("SELECT count(*) FROM documents WHERE state='failed'").fetchone()[0]
        issue_failed = con.execute("SELECT count(*) FROM issues WHERE kind='enrichment_failed'").fetchone()[0]
        con.close()
        return 1 if (failures or doc_failed or issue_failed) and args.command in ("run", "download") else 0
    except KeyboardInterrupt:
        print("Interrupted; completed work is checkpointed. Resume using the same configuration.", file=sys.stderr)
        return 130
    except (ValueError, OSError) as exc:
        print("Error:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
