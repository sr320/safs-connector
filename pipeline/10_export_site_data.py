"""Step 10: export the data the explorer (site/) needs, from data/safs.sqlite (M5).

  uv run python pipeline/10_export_site_data.py   # -> site/data/meta.json, site/data/works.json

The site computes every view (graph edges, faculty cards, search) in the browser from
works.json, so year and theme filters stay consistent across views. Only papers with
status 'keep' are exported.

works.json rows are compact arrays (see `cols` in the file) to keep the download small.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from common import DATA, ROOT

DB = DATA / "safs.sqlite"
OUT = ROOT / "site" / "data"


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: con.execute(sql, a).fetchall()

    faculty = [dict(slug=r["slug"], name=r["name"], rank=r["rank"], url=r["profile_url"],
                    areas=json.loads(r["research_areas"] or "[]"), scholar=r["google_scholar_id"])
               for r in q("SELECT * FROM faculty ORDER BY name")]
    themes = [dict(id=r["id"], name=r["name"]) for r in
              q("SELECT id, name FROM themes WHERE parent_id IS NULL ORDER BY sort")]
    subtopics = [dict(id=r["id"], name=r["name"], theme=r["parent_id"]) for r in
                 q("SELECT id, name, parent_id FROM themes WHERE parent_id IS NOT NULL ORDER BY sort")]

    kept = {r["work_id"] for r in q("SELECT DISTINCT work_id FROM faculty_works WHERE status='keep'")}
    fac_of = defaultdict(list)
    for r in q("SELECT work_id, faculty_slug FROM faculty_works WHERE status='keep' ORDER BY faculty_slug"):
        fac_of[r["work_id"]].append(r["faculty_slug"])
    org_of = defaultdict(set)
    for r in q("""SELECT ai.work_id, COALESCE(i.org_id, i.id) AS org
                  FROM authorship_institutions ai JOIN institutions i ON i.id = ai.institution_id
                  JOIN institutions o ON o.id = COALESCE(i.org_id, i.id)
                  WHERE i.is_uw = 0 AND o.is_uw = 0"""):
        org_of[r["work_id"]].add(r["org"])
    primary, subs = {}, defaultdict(list)
    for r in q("SELECT work_id, theme_id, role FROM work_themes ORDER BY rank"):
        if r["role"] == "primary_theme":
            primary[r["work_id"]] = r["theme_id"]
        else:
            subs[r["work_id"]].append(r["theme_id"])
    tags = defaultdict(lambda: defaultdict(list))
    for r in q("SELECT work_id, kind, value FROM work_tags"):
        tags[r["work_id"]][r["kind"]].append(r["value"])
    summary = {r["work_id"]: r["summary"] for r in q("SELECT work_id, summary FROM work_summaries")}

    cols = ["id", "title", "year", "venue", "doi", "type", "cited_by", "faculty", "theme",
            "subtopics", "orgs", "taxa", "species", "ecosystems", "regions", "methods", "summary"]
    rows = []
    used_orgs = set()
    for w in q("SELECT id, title, year, venue, doi, type, cited_by FROM works ORDER BY year DESC, id"):
        wid = w["id"]
        if wid not in kept:
            continue
        orgs = sorted(org_of.get(wid, ()))
        used_orgs |= set(orgs)
        t = tags.get(wid, {})
        rows.append([wid, w["title"], w["year"], w["venue"], w["doi"], w["type"], w["cited_by"],
                     fac_of[wid], primary.get(wid), subs.get(wid, []), orgs,
                     t.get("taxon_group", []), t.get("species", []), t.get("ecosystem", []),
                     t.get("region", []), t.get("method", []), summary.get(wid)])

    orgs = {}
    for r in q("SELECT id, name, country, type, city, lat, lon FROM institutions"):
        if r["id"] in used_orgs:
            orgs[r["id"]] = dict(name=r["name"], country=r["country"], type=r["type"],
                                 city=r["city"], lat=r["lat"], lon=r["lon"])

    OUT.mkdir(parents=True, exist_ok=True)
    meta = dict(faculty=faculty, themes=themes, subtopics=subtopics, orgs=orgs,
                n_works=len(rows), built=con.execute("SELECT date('now')").fetchone()[0])
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    (OUT / "works.json").write_text(json.dumps(dict(cols=cols, rows=rows), ensure_ascii=False,
                                               separators=(",", ":")))
    for f in ("meta.json", "works.json"):
        print(f"Wrote site/data/{f}  ({(OUT / f).stat().st_size / 1e6:.1f} MB)")
    print(f"  {len(rows)} papers, {len(faculty)} faculty, {len(themes)} themes, {len(orgs)} partner orgs")


if __name__ == "__main__":
    main()
