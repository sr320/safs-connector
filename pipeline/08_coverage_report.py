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
          SUM(fw.status='keep' AND w.abstract IS NOT NULL) AS with_abstract
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
        "",
        "| Faculty | Rank | Profiles | Kept | Suspect | Dropped | Years | 1st-author | Abstracts | Top external partners | Scholar |",
        "|---|---|---:|---:|---:|---:|---|---:|---:|---|---|",
    ]
    for r in rows:
        flag = " ⚠︎" if r["resolution_status"] != "auto" else ""
        scholar = (f"[link](https://scholar.google.com/citations?user={r['google_scholar_id']})"
                   if r["google_scholar_id"] else "–")
        years = f"{r['y0']}–{r['y1']}" if r["y0"] else "–"
        lines.append(
            f"| {r['name']}{flag} | {r['rank']} | {r['n_profiles']} | {r['keep'] or 0} | "
            f"{r['suspect'] or 0} | {r['dropped'] or 0} | {years} | {r['first_author'] or 0} | "
            f"{r['with_abstract'] or 0} | {'; '.join(partners.get(r['slug'], [])) or '–'} | {scholar} |")
    lines += ["", "⚠︎ = author resolution still `needs_review` in `config/faculty_overrides.yaml`.", ""]
    (DATA / "coverage.md").write_text("\n".join(lines))
    print(f"Wrote data/coverage.md ({len(rows)} faculty)")


if __name__ == "__main__":
    main()
