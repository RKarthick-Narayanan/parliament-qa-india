"""Validate and canonicalize a topic before any network access."""
import hashlib
import json
from datetime import date
from pathlib import Path

import yaml


def load_config(path: Path) -> tuple[dict, str]:
    try:
        with path.open(encoding="utf-8") as f:
            obj = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError("invalid YAML configuration: " + str(exc)) from exc
    if not isinstance(obj, dict) or obj.get("version") != 1:
        raise ValueError("config must be a mapping with version: 1")
    houses = obj.get("houses")
    if not isinstance(houses, list) or not houses or any(h not in ("LS", "RS") for h in houses):
        raise ValueError("houses must be a non-empty list containing LS and/or RS")
    dates = obj.get("dates")
    if not isinstance(dates, dict):
        raise ValueError("dates must contain from and to")
    try:
        start = date.fromisoformat(str(dates["from"]))
        end = date.fromisoformat(str(dates["to"]))
    except (ValueError, KeyError) as exc:
        raise ValueError("dates must be ISO dates: YYYY-MM-DD") from exc
    if start > end:
        raise ValueError("dates.from must not be after dates.to")
    search = obj.get("search")
    terms = search.get("terms") if isinstance(search, dict) else None
    if not isinstance(terms, list) or not terms or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError("search.terms must be a non-empty list of strings")
    documents = obj.get("documents", {})
    if not isinstance(documents, dict) or not isinstance(documents.get("download", False), bool):
        raise ValueError("documents.download must be a boolean")
    cfg = {"version": 1, "name": str(obj.get("name", "collection")),
           "houses": list(dict.fromkeys(houses)),
           "dates": {"from": start.isoformat(), "to": end.isoformat()},
           "search": {"terms": list(dict.fromkeys(t.strip() for t in terms))},
           "documents": {"download": documents.get("download", False)}}
    encoded = json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return cfg, hashlib.sha256(encoded).hexdigest()
