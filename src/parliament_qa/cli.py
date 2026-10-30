"""Command line interface for independent collection folders."""
import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .core import collect, db_open, download, export, jsonstr, now


def main():
    parser = argparse.ArgumentParser(prog="parliament-qa")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--resume", action="store_true")
    for command in ("download", "status", "export"):
        p = sub.add_parser(command); p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    try:
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
