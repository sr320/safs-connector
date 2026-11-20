"""Step 8: per-faculty coverage report -> data/coverage.md

Lets each faculty member eyeball their record: how many papers we hold, how many are
flagged, the year span, and top external partners. Compare against Google Scholar
(link provided) to spot missing papers.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from common import DATA

DB = DATA / "safs.sqlite"


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT f.slug, f.name, f.rank, f.resolution_status, f.google_scholar_id, f.orcid,
          (SELECT COUNT(*) FROM faculty_author_ids a WHERE a.faculty_slug=f.slug AND a.decision='include') AS n_profiles,
          SUM(fw.status='keep') AS keep, SUM(fw.status='suspect') AS suspect, SUM(fw.status='drop') AS dropped,
          MIN(CASE WHEN fw.status='keep' THEN w.year END) AS y0,
          MAX(CASE WHEN fw.status='keep' THEN w.year END) AS y1,
          SUM(fw.status='keep' AND fw.author_position='first') AS first_author,
          SUM(fw.status='keep' AND w.abstract IS NOT NULL) AS with_abstract,
          SUM(fw.status='keep' AND fw.in_orcid=1) AS in_orcid,
          SUM(fw.source='orcid' AND fw.status='keep') AS orcid_only,
          (SELECT COUNT(*) FROM orcid_missing m WHERE m.faculty_slug=f.slug) AS orcid_missing
        FROM faculty f LEFT JOIN faculty_works fw ON fw.faculty_slug=f.slug
        LEFT JOIN works w ON w.id=fw.work_id
        GROUP BY f.slug ORDER BY f.name""").fetchall()

    partners = {}
    for r in con.execute("""SELECT faculty_slug, name, n_works FROM v_faculty_org
                            ORDER BY faculty_slug, n_works DESC"""):
        partners.setdefault(r["faculty_slug"], [])
        if len(partners[r["faculty_slug"]]) < 3:
            partners[r["faculty_slug"]].append(f"{r['name']} ({r['n_works']})")

    tot = con.execute("SELECT status, COUNT(*) FROM faculty_works GROUP BY status").fetchall()
    lines = [
        "# Coverage report",
        "",
        f"Generated {date.today().isoformat()} by `pipeline/08_coverage_report.py`. "
        f"Links: {', '.join(f'{s} {n}' for s, n in tot)}.",
        "",
        "- **Kept**: counted in the explorer. **Suspect**: likely another person with the same name;",
        "  run `04_check_attribution.py` with an Anthropic key, or settle in `config/work_overrides.yaml`.",
        "  **Dropped**: errata, retractions, or confirmed misattributions.",
        "- Compare **Kept** with the Google Scholar count to spot missing papers.",
        "- **In ORCID**: kept papers the person also lists in their own ORCID record (+ papers found",
        "  only through ORCID). **ORCID gaps**: ORCID works OpenAlex can't supply (listed below).",
        "  Blank when there is no ORCID iD or the ORCID record is empty.",
        "",
        "| Faculty | Rank | Profiles | Kept | Suspect | Dropped | In ORCID | ORCID gaps | Years | 1st-author | Abstracts | Top external partners | Scholar |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---|",
    ]
    for r in rows:
        flag = " ⚠︎" if r["resolution_status"] != "auto" else ""
        scholar = (f"[link](https://scholar.google.com/citations?user={r['google_scholar_id']})"
                   if r["google_scholar_id"] else "–")
        years = f"{r['y0']}–{r['y1']}" if r["y0"] else "–"
        listed = r["in_orcid"] or 0
        orc = (f"{listed}" + (f" (+{r['orcid_only']})" if r["orcid_only"] else "")) if listed else "–"
        gaps = str(r["orcid_missing"]) if listed else "–"
        lines.append(
            f"| {r['name']}{flag} | {r['rank']} | {r['n_profiles']} | {r['keep'] or 0} | "
            f"{r['suspect'] or 0} | {r['dropped'] or 0} | {orc} | {gaps} | {years} | {r['first_author'] or 0} | "
            f"{r['with_abstract'] or 0} | {'; '.join(partners.get(r['slug'], [])) or '–'} | {scholar} |")
    lines += ["", "⚠︎ = author resolution still `needs_review` in `config/faculty_overrides.yaml`.", ""]

    # ORCID works that OpenAlex could not supply, newest first
    names = {r["slug"]: r["name"] for r in rows}
    missing: dict[str, list] = {}
    for m in con.execute("SELECT * FROM orcid_missing ORDER BY faculty_slug, year DESC"):
        missing.setdefault(m["faculty_slug"], []).append(m)
    if missing:
        lines += ["## ORCID works missing from OpenAlex", "",
                  "Listed in the person's ORCID record but not in the explorer: no DOI, or a DOI OpenAlex",
                  "does not index (reports, theses, book chapters, talks). Shows up to 10 per person.", ""]
        for slug in sorted(missing, key=lambda s: names.get(s, s)):
            ms = missing[slug]
            lines.append(f"<details><summary>{names.get(slug, slug)} ({len(ms)})</summary>\n")
            for m in ms[:10]:
                doi = f" [doi](https://doi.org/{m['doi']})" if m["doi"] else ""
                kind = (m["type"] or "").replace("-", " ")
                lines.append(f"- {m['year'] or 'n.d.'}: {m['title'] or '(untitled)'} ({kind}){doi}")
            lines += ["", "</details>", ""]
    (DATA / "coverage.md").write_text("\n".join(lines))
    print(f"Wrote data/coverage.md ({len(rows)} faculty)")


if __name__ == "__main__":
    main()
