"""Step 3b: cross-check each faculty member's papers against their ORCID record.

ORCID lists are curated by the researchers themselves, so they are a check on OpenAlex
in both directions:

  confirmed  a harvested work whose DOI (or exact title) is in the person's ORCID record.
             04_check_attribution.py keeps these without further review.
  added      an ORCID DOI that OpenAlex has but did not credit to the person's author
             profile (often a split OpenAlex profile). The work is fetched from OpenAlex
             and appended to data/raw/works/<slug>.jsonl, so later steps treat it like
             any other harvested work. Added when the person is on the byline (matched
             by ORCID iD, else by surname), or when OpenAlex's byline is incomplete
             (one author, or marked truncated); then the byline slot is -1.
  missing    ORCID works OpenAlex cannot supply (no DOI, or a DOI OpenAlex lacks).
             Listed in data/orcid.json and the coverage report for a human to look at.

Run after 03_harvest_works.py (which rewrites the raw files) and before 04.
Faculty without an ORCID iD are skipped.

  uv run python pipeline/03b_orcid.py            # cached ORCID responses when present
  uv run python pipeline/03b_orcid.py --refresh  # re-download ORCID records

Writes data/orcid.json and updates data/works_index.json (adds `orcid_added`).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import unicodedata

import yaml

from common import CONFIG, DATA, OPENALEX, RAW, cached_get

ORCID_API = "https://pub.orcid.org/v3.0"
OUT = DATA / "orcid.json"
WORKS_DIR = RAW / "works"
SELECT = ",".join([  # same fields as 03_harvest_works.py
    "id", "doi", "title", "publication_year", "publication_date", "type", "language",
    "primary_location", "authorships", "is_authors_truncated", "cited_by_count",
    "open_access", "primary_topic", "topics", "keywords", "abstract_inverted_index",
    "is_retracted", "is_paratext", "updated_date",
])


def sid(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


def norm_doi(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip().lower()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d if d.startswith("10.") else None


def norm_title(t: str | None) -> str | None:
    if not t:
        return None
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"<[^>]+>", " ", t)          # inline markup such as <i>
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return t if len(t) >= 20 else None       # short titles ("Introduction") are not unique


def norm_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]+", " ", s)


def orcid_works(orcid: str, refresh: bool) -> list[dict]:
    """One entry per ORCID work group: DOIs, title, year, type."""
    data = cached_get(f"{ORCID_API}/{orcid}/works", refresh=refresh, min_interval=0.1,
                      headers={"Accept": "application/json"})
    out = []
    for g in data.get("group") or []:
        ids = (g.get("external-ids") or {}).get("external-id") or []
        dois = {norm_doi(e.get("external-id-value")) for e in ids
                if e.get("external-id-type") == "doi" and e.get("external-id-relationship") == "self"}
        s = (g.get("work-summary") or [{}])[0]
        year = (((s.get("publication-date") or {}).get("year") or {}).get("value"))
        out.append({
            "dois": sorted(d for d in dois if d),
            "title": (((s.get("title") or {}).get("title") or {}).get("value")),
            "year": int(year) if year and year.isdigit() else None,
            "type": s.get("type"),
        })
    return out


def openalex_by_doi(dois: list[str], refresh: bool) -> dict[str, dict]:
    found = {}
    usable = [d for d in dois if "|" not in d and "," not in d]
    for i in range(0, len(usable), 50):
        chunk = usable[i:i + 50]
        data = cached_get(f"{OPENALEX}/works", {
            "filter": "doi:" + "|".join(chunk), "per-page": 100, "select": SELECT,
        }, refresh=refresh)
        for w in data.get("results") or []:
            d = norm_doi(w.get("doi"))
            if d:
                found[d] = w
    # OpenAlex merges a preprint into its published version, so a preprint DOI can come back
    # as a record carrying the journal DOI. Ask for each unmatched DOI alone to map it back.
    for d in usable:
        if d in found:
            continue
        data = cached_get(f"{OPENALEX}/works", {"filter": f"doi:{d}", "select": SELECT}, refresh=refresh)
        res = data.get("results") or []
        if len(res) == 1:
            found[d] = res[0]
    return found


def byline_slot(work: dict, orcid: str, surname: str) -> int | None:
    """Index of the faculty member in the byline: ORCID iD first, then surname."""
    auths = work.get("authorships") or []
    for i, a in enumerate(auths):
        if (a["author"].get("orcid") or "").endswith(orcid):
            return i
    for i, a in enumerate(auths):
        names = norm_name(a["author"].get("display_name")) + " " + norm_name(a.get("raw_author_name"))
        if surname in names.split():
            return i
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    roster = {f["slug"]: f for f in json.loads((DATA / "roster.json").read_text())}
    overrides = yaml.safe_load((CONFIG / "faculty_overrides.yaml").read_text())
    index_path = DATA / "works_index.json"
    index = json.loads(index_path.read_text())
    previous = json.loads(OUT.read_text()) if OUT.exists() else {}
    result: dict[str, dict] = {}

    for slug, meta in index.items():
        if args.only and slug not in args.only:
            if slug in previous:
                result[slug] = previous[slug]
            continue
        f = roster.get(slug, {})
        orcid = (overrides.get(slug, {}).get("orcid") or f.get("orcid") or "").rsplit("/", 1)[-1]
        path = WORKS_DIR / f"{slug}.jsonl"
        if not orcid or not path.exists():
            continue
        surname = norm_name(f.get("name", "")).split()[-1] if f.get("name") else ""

        works = [json.loads(line) for line in path.open()]
        # Rerun without a fresh harvest: drop this step's earlier additions, then redo them.
        prev_added = set(meta.get("orcid_added") or [])
        if prev_added:
            works = [w for w in works if sid(w["id"]) not in prev_added]
            path.write_text("".join(json.dumps(w, ensure_ascii=False) + "\n" for w in works))
            meta["work_ids"] = sorted(set(meta["work_ids"]) - prev_added)
        by_doi = {norm_doi(w.get("doi")): sid(w["id"]) for w in works if norm_doi(w.get("doi"))}
        by_title = collections.defaultdict(list)
        for w in works:
            t = norm_title(w.get("title"))
            if t:
                by_title[t].append((w.get("publication_year"), sid(w["id"])))

        recs = orcid_works(orcid, args.refresh)
        confirmed: set[str] = set()
        want: dict[str, dict] = {}   # DOI -> ORCID record, for DOIs not in the harvest
        no_doi: list[dict] = []
        for r in recs:
            hit = next((by_doi[d] for d in r["dois"] if d in by_doi), None)
            if not hit:
                t = norm_title(r["title"])
                hit = next((wid for y, wid in by_title.get(t, [])
                            if not (r["year"] and y) or abs(r["year"] - y) <= 1), None)
            if hit:
                confirmed.add(hit)
            elif r["dois"]:
                want[r["dois"][0]] = r
            else:
                no_doi.append(r)

        fetched = openalex_by_doi(sorted(want), args.refresh)
        have = {sid(w["id"]) for w in works}
        added: dict[str, int] = {}
        split_ids: collections.Counter = collections.Counter()
        byline_mismatch, not_in_openalex = [], []
        new_records = []
        for d, r in want.items():
            w = fetched.get(d)
            if not w:
                not_in_openalex.append({**r, "dois": r["dois"][:1]})
                continue
            wid = sid(w["id"])
            if wid in have:        # same work reached through another DOI (e.g. preprint)
                confirmed.add(wid)
                continue
            slot = byline_slot(w, orcid, surname)
            if slot is None:
                # OpenAlex sometimes keeps only the first author; then trust ORCID (slot -1)
                incomplete = len(w.get("authorships") or []) <= 1 or w.get("is_authors_truncated")
                if not incomplete:
                    byline_mismatch.append({"work_id": wid, "doi": d, "title": w.get("title")})
                    continue
                slot = -1
            added[wid] = slot
            have.add(wid)
            new_records.append(w)
            aid = sid(w["authorships"][slot]["author"].get("id")) if slot >= 0 else None
            if aid:
                split_ids[aid] += 1

        with path.open("a") as fh:
            for w in new_records:
                fh.write(json.dumps(w, ensure_ascii=False) + "\n")
        meta["work_ids"] = sorted(set(meta["work_ids"]) | set(added))
        meta["orcid_added"] = sorted(added)

        def by_year(xs):
            return sorted(xs, key=lambda x: -(x.get("year") or 0))

        result[slug] = {
            "orcid": orcid,
            "orcid_works": len(recs),
            "confirmed": sorted(confirmed),
            "added": dict(sorted(added.items())),
            # OpenAlex author IDs that hold the added works: likely split profiles of this person
            "other_author_ids": dict(split_ids.most_common()),
            "byline_mismatch": byline_mismatch,
            "not_in_openalex": by_year(not_in_openalex),
            "no_doi": by_year(no_doi),
        }
        print(f"  {slug:28s} orcid={len(recs):4d} confirmed={len(confirmed):4d} added={len(added):3d} "
              f"not-in-openalex={len(not_in_openalex) + len(no_doi):3d} byline-mismatch={len(byline_mismatch)}")

    index_path.write_text(json.dumps(index, indent=1) + "\n")
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    tot = collections.Counter()
    for r in result.values():
        tot.update(confirmed=len(r["confirmed"]), added=len(r["added"]),
                   missing=len(r["not_in_openalex"]) + len(r["no_doi"]))
    print(f"Wrote {OUT.relative_to(DATA.parent)}: {len(result)} faculty with ORCID, {dict(tot)}")


if __name__ == "__main__":
    main()
