"""Step 9: human spot-check of Claude's topic tags (M3 acceptance: >=90% agreement).

  uv run python pipeline/09_spotcheck.py sample [--n 100]   # -> data/spotcheck.csv
  (open the CSV in a spreadsheet; fill agree_theme and agree_subtopics with y/n)
  uv run python pipeline/09_spotcheck.py score              # agreement summary
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3

from common import DATA

DB = DATA / "safs.sqlite"
CSV = DATA / "spotcheck.csv"
FIELDS = ["work_id", "faculty", "year", "title", "primary_theme", "subtopics", "taxon_groups",
          "ecosystems", "methods", "regions", "summary", "agree_theme", "agree_subtopics", "notes"]


def sample(n: int) -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM themes")}
    rows = con.execute("""
        SELECT w.id, w.year, w.title, s.summary,
               (SELECT group_concat(f.name, '; ') FROM faculty_works fw JOIN faculty f ON f.slug = fw.faculty_slug
                WHERE fw.work_id = w.id AND fw.status = 'keep') AS faculty
        FROM works w JOIN work_summaries s ON s.work_id = w.id
        WHERE EXISTS (SELECT 1 FROM faculty_works fw WHERE fw.work_id = w.id AND fw.status = 'keep')
        ORDER BY w.id""").fetchall()
    if not rows:
        raise SystemExit("No tagged papers in safs.sqlite yet: run 05_enrich_topics.py, then 07_build_db.py.")
    picked = random.Random(7).sample(rows, min(n, len(rows)))

    def themes(wid, role):
        return [names[r[0]] for r in con.execute(
            "SELECT theme_id FROM work_themes WHERE work_id=? AND role=? ORDER BY rank", (wid, role))]

    def tags(wid, kind):
        return "; ".join(r[0] for r in con.execute(
            "SELECT value FROM work_tags WHERE work_id=? AND kind=?", (wid, kind)))

    with CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in picked:
            w.writerow({
                "work_id": r["id"], "faculty": r["faculty"], "year": r["year"], "title": r["title"],
                "primary_theme": "; ".join(themes(r["id"], "primary_theme")),
                "subtopics": "; ".join(themes(r["id"], "subtopic")),
                "taxon_groups": tags(r["id"], "taxon_group"), "ecosystems": tags(r["id"], "ecosystem"),
                "methods": tags(r["id"], "method"), "regions": tags(r["id"], "region"),
                "summary": r["summary"], "agree_theme": "", "agree_subtopics": "", "notes": ""})
    print(f"Wrote {CSV.relative_to(DATA.parent)} ({len(picked)} papers). Fill agree_* with y/n, then run `score`.")


def score() -> None:
    rows = list(csv.DictReader(CSV.open()))
    for col in ("agree_theme", "agree_subtopics"):
        vals = [r[col].strip().lower()[:1] for r in rows if r[col].strip()]
        if not vals:
            print(f"  {col}: not filled in yet")
            continue
        yes = sum(v == "y" for v in vals)
        print(f"  {col:16s} {yes}/{len(vals)} = {yes / len(vals):.0%}  (target >= 90%)")
    misses = [r for r in rows if r["agree_theme"].strip().lower().startswith("n")]
    if misses:
        print("\nTheme disagreements (consider refining topic_taxonomy.yaml descriptions):")
        for r in misses:
            print(f"  {r['work_id']}  [{r['primary_theme']}]  {r['title'][:70]}  {r['notes']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sample", "score"])
    ap.add_argument("--n", type=int, default=100)
    args = ap.parse_args()
    sample(args.n) if args.cmd == "sample" else score()


if __name__ == "__main__":
    main()
