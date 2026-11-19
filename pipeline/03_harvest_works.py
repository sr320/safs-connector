"""Step 3: harvest every OpenAlex work for each faculty member's included author IDs.

Reads  config/faculty_overrides.yaml  (candidates with decision: include)
Writes data/raw/works/<slug>.jsonl    (full OpenAlex records, gitignored cache)
       data/works_index.json          (slug -> [work ids], plus harvest metadata)

Usage:
  uv run python pipeline/03_harvest_works.py            # use cached pages when present
  uv run python pipeline/03_harvest_works.py --refresh  # re-download everything
"""
from __future__ import annotations

import argparse
import json
from datetime import date

import yaml

from common import CONFIG, DATA, OPENALEX, RAW, cached_get

SELECT = ",".join([
    "id", "doi", "title", "publication_year", "publication_date", "type", "language",
    "primary_location", "authorships", "is_authors_truncated", "cited_by_count",
    "open_access", "primary_topic", "topics", "keywords", "abstract_inverted_index",
    "is_retracted", "is_paratext", "updated_date",
])
WORKS_DIR = RAW / "works"


def included_ids(entry: dict) -> list[str]:
    return [e["id"] for e in entry.get("openalex", []) if e.get("decision") == "include"]


def harvest(author_ids: list[str], refresh: bool) -> list[dict]:
    """All works for any of the author IDs (OpenAlex OR filter).

    Uses page-number paging: OpenAlex requires an API key for cursor paging but
    allows basic paging up to 10,000 results, far more than any one person has.
    """
    works: dict[str, dict] = {}
    flt = "author.id:" + "|".join(author_ids)  # OR filter, up to 100 values
    page = 1
    while True:
        data = cached_get(f"{OPENALEX}/works", {
            "filter": flt, "per-page": 200, "page": page, "select": SELECT,
            "sort": "publication_year:desc",  # stable order across pages
        }, refresh=refresh)
        results = data.get("results", [])
        for w in results:
            works[w["id"]] = w
        count = data.get("meta", {}).get("count", 0)
        if not results or page * 200 >= count:
            break
        if page * 200 >= 10_000:
            raise RuntimeError(f"{flt}: >10k works; needs an OpenAlex API key for cursor paging")
        page += 1
    return list(works.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    overrides = yaml.safe_load((CONFIG / "faculty_overrides.yaml").read_text())
    index_path = DATA / "works_index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    WORKS_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for slug, entry in overrides.items():
        if args.only and slug not in args.only:
            total += len(index.get(slug, {}).get("work_ids", []))
            continue
        ids = included_ids(entry)
        if not ids:
            print(f"  {slug:28s} (no included author IDs; skipped)")
            index[slug] = {"author_ids": [], "work_ids": [], "harvested_on": None}
            continue
        works = harvest(ids, args.refresh)
        with open(WORKS_DIR / f"{slug}.jsonl", "w") as fh:
            for w in works:
                fh.write(json.dumps(w, ensure_ascii=False) + "\n")
        index[slug] = {
            "author_ids": ids,
            "work_ids": sorted(w["id"].rsplit("/", 1)[-1] for w in works),
            "harvested_on": date.today().isoformat(),
        }
        total += len(works)
        print(f"  {slug:28s} {len(works):5d} works  (authors: {', '.join(ids)})")

    index_path.write_text(json.dumps(index, indent=1) + "\n")
    uniq = len({w for v in index.values() for w in v["work_ids"]})
    print(f"Harvested {total} faculty-work links, {uniq} unique works -> {index_path.relative_to(DATA.parent)}")


if __name__ == "__main__":
    main()
