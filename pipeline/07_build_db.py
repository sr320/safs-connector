"""Step 7: build data/safs.sqlite from the roster, overrides, harvested works and
attribution flags. Rebuilt from scratch each run (it is a derived artifact).

Core tables
  faculty, faculty_author_ids, works, faculty_works (with attribution status),
  authorships, authorship_institutions, institutions
Views (count only faculty_works.status = 'keep')
  v_faculty_works, v_faculty_institution, v_faculty_faculty, v_institution_summary
Topic tables (themes, work_themes, work_tags, work_summaries) come from
  config/topic_taxonomy.yaml + data/topic_tags.jsonl (M3).
Institution coordinates, parent links and partner-organization rollup come from
  data/institutions.json + data/affiliation_matches.json (M4, 06_normalize_insts.py):
  views v_faculty_org, v_org_summary, v_theme_org.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from common import CONFIG, DATA, RAW, UW_OPENALEX_ID

DB = DATA / "safs.sqlite"

SCHEMA = """
CREATE TABLE faculty (
  slug TEXT PRIMARY KEY, name TEXT, rank TEXT, titles TEXT, research_areas TEXT,
  profile_url TEXT, orcid TEXT, google_scholar_id TEXT, research_blurb TEXT,
  resolution_status TEXT
);
CREATE TABLE faculty_author_ids (
  faculty_slug TEXT REFERENCES faculty(slug), openalex_author_id TEXT,
  decision TEXT, method TEXT, confidence REAL, reason TEXT,
  PRIMARY KEY (faculty_slug, openalex_author_id)
);
CREATE TABLE works (
  id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER, pub_date TEXT, type TEXT,
  language TEXT, venue TEXT, venue_type TEXT, cited_by INTEGER, is_oa INTEGER,
  oa_status TEXT, primary_topic TEXT, subfield TEXT, field TEXT, domain TEXT,
  topics TEXT, keywords TEXT, abstract TEXT, n_authors INTEGER,
  authors_truncated INTEGER, is_retracted INTEGER, is_paratext INTEGER
);
CREATE TABLE faculty_works (
  faculty_slug TEXT REFERENCES faculty(slug), work_id TEXT REFERENCES works(id),
  status TEXT,            -- keep | suspect | drop
  reasons TEXT, method TEXT, note TEXT,
  author_position TEXT, is_corresponding INTEGER, byline_has_uw INTEGER,
  source TEXT,            -- openalex (author profile) | orcid (found only through ORCID)
  in_orcid INTEGER,       -- 1 listed in their ORCID record, 0 not listed, NULL no ORCID iD
  PRIMARY KEY (faculty_slug, work_id)
);
-- ORCID works OpenAlex could not supply (no DOI, or a DOI OpenAlex lacks)
CREATE TABLE orcid_missing (
  faculty_slug TEXT REFERENCES faculty(slug), title TEXT, year INTEGER, doi TEXT, type TEXT
);
CREATE TABLE authorships (
  work_id TEXT REFERENCES works(id), idx INTEGER, author_id TEXT, display_name TEXT,
  raw_name TEXT, author_position TEXT, is_corresponding INTEGER,
  faculty_slug TEXT,      -- set when this author is a SAFS faculty member
  PRIMARY KEY (work_id, idx)
);
CREATE TABLE authorship_institutions (
  work_id TEXT, idx INTEGER, institution_id TEXT,
  source TEXT,            -- openalex | ror-affiliation (free-text affiliation matched by ROR) | human
  PRIMARY KEY (work_id, idx, institution_id)
);
CREATE TABLE institutions (
  id TEXT PRIMARY KEY, name TEXT, ror TEXT, country TEXT, type TEXT, lineage TEXT,
  is_uw INTEGER,
  city TEXT, region TEXT, lat REAL, lon REAL,
  parent_id TEXT,         -- direct parent institution (M4)
  org_id TEXT             -- partner organization it rolls up to, e.g. NWFSC -> NOAA (M4)
);
CREATE INDEX ix_fw_work ON faculty_works(work_id);
CREATE INDEX ix_auth_author ON authorships(author_id);
CREATE INDEX ix_ai_inst ON authorship_institutions(institution_id);

CREATE VIEW v_faculty_works AS
  SELECT fw.faculty_slug, w.* FROM faculty_works fw JOIN works w ON w.id = fw.work_id
  WHERE fw.status = 'keep';

-- co-author institutions per faculty member (distinct papers), excluding UW itself
CREATE VIEW v_faculty_institution AS
  SELECT fw.faculty_slug, i.id AS institution_id, i.name, i.country, i.type,
         COUNT(DISTINCT fw.work_id) AS n_works,
         MIN(w.year) AS first_year, MAX(w.year) AS last_year
  FROM faculty_works fw
  JOIN works w ON w.id = fw.work_id
  JOIN authorship_institutions ai ON ai.work_id = fw.work_id
  JOIN institutions i ON i.id = ai.institution_id
  WHERE fw.status = 'keep' AND i.is_uw = 0
  GROUP BY fw.faculty_slug, i.id;

-- internal SAFS collaboration
CREATE VIEW v_faculty_faculty AS
  SELECT a.faculty_slug AS a, b.faculty_slug AS b, COUNT(*) AS n_works,
         MIN(w.year) AS first_year, MAX(w.year) AS last_year
  FROM faculty_works a
  JOIN faculty_works b ON a.work_id = b.work_id AND a.faculty_slug < b.faculty_slug
  JOIN works w ON w.id = a.work_id
  WHERE a.status = 'keep' AND b.status = 'keep'
  GROUP BY a.faculty_slug, b.faculty_slug;

-- same, rolled up to partner organizations (NOAA science centers -> NOAA, etc.)
CREATE VIEW v_faculty_org AS
  SELECT fw.faculty_slug, o.id AS org_id, o.name, o.country, o.type, o.city, o.lat, o.lon,
         COUNT(DISTINCT fw.work_id) AS n_works,
         MIN(w.year) AS first_year, MAX(w.year) AS last_year
  FROM faculty_works fw
  JOIN works w ON w.id = fw.work_id
  JOIN authorship_institutions ai ON ai.work_id = fw.work_id
  JOIN institutions i ON i.id = ai.institution_id
  JOIN institutions o ON o.id = COALESCE(i.org_id, i.id)
  WHERE fw.status = 'keep' AND i.is_uw = 0 AND o.is_uw = 0
  GROUP BY fw.faculty_slug, o.id;

CREATE VIEW v_org_summary AS
  SELECT org_id, name, country, type, city, lat, lon,
         COUNT(DISTINCT faculty_slug) AS n_faculty, SUM(n_works) AS faculty_work_links,
         MIN(first_year) AS first_year, MAX(last_year) AS last_year
  FROM v_faculty_org GROUP BY org_id;

CREATE VIEW v_institution_summary AS
  SELECT institution_id, name, country, type,
         COUNT(DISTINCT faculty_slug) AS n_faculty, SUM(n_works) AS faculty_work_links
  FROM v_faculty_institution GROUP BY institution_id;

-- ---- topics (filled from data/topic_tags.jsonl when present)
CREATE TABLE themes (
  id TEXT PRIMARY KEY, name TEXT, description TEXT,
  parent_id TEXT,          -- NULL for a theme, theme id for a subtopic
  sort INTEGER
);
CREATE TABLE work_themes (
  work_id TEXT, theme_id TEXT,
  role TEXT,               -- primary_theme | subtopic
  rank INTEGER,
  PRIMARY KEY (work_id, theme_id, role)
);
CREATE TABLE work_tags (
  work_id TEXT, kind TEXT, value TEXT,   -- kind: taxon_group|species|ecosystem|region|method
  PRIMARY KEY (work_id, kind, value)
);
CREATE TABLE work_summaries (work_id TEXT PRIMARY KEY, summary TEXT, model TEXT);
CREATE INDEX ix_wt_theme ON work_themes(theme_id);
CREATE INDEX ix_tags_value ON work_tags(kind, value);

-- every theme a kept paper touches (primary theme + parent themes of its subtopics)
CREATE VIEW v_work_theme_any AS
  SELECT work_id, theme_id FROM work_themes WHERE role = 'primary_theme'
  UNION
  SELECT wt.work_id, t.parent_id FROM work_themes wt JOIN themes t ON t.id = wt.theme_id
  WHERE wt.role = 'subtopic';

CREATE VIEW v_faculty_theme AS
  SELECT fw.faculty_slug, t.id AS theme_id, t.name,
         COUNT(*) AS n_works,
         ROUND(1.0 * COUNT(*) / (SELECT COUNT(*) FROM faculty_works x
                                 WHERE x.faculty_slug = fw.faculty_slug AND x.status = 'keep'), 3) AS share
  FROM faculty_works fw
  JOIN work_themes wt ON wt.work_id = fw.work_id AND wt.role = 'primary_theme'
  JOIN themes t ON t.id = wt.theme_id
  WHERE fw.status = 'keep'
  GROUP BY fw.faculty_slug, t.id;

CREATE VIEW v_faculty_subtopic AS
  SELECT fw.faculty_slug, t.id AS subtopic_id, t.name, t.parent_id AS theme_id,
         COUNT(*) AS n_works
  FROM faculty_works fw
  JOIN work_themes wt ON wt.work_id = fw.work_id AND wt.role = 'subtopic'
  JOIN themes t ON t.id = wt.theme_id
  WHERE fw.status = 'keep'
  GROUP BY fw.faculty_slug, t.id;

-- which outside institutions work with SAFS on which theme
CREATE VIEW v_theme_institution AS
  SELECT a.theme_id, i.id AS institution_id, i.name, i.country, i.type,
         COUNT(DISTINCT a.work_id) AS n_works
  FROM v_work_theme_any a
  JOIN (SELECT DISTINCT work_id FROM faculty_works WHERE status = 'keep') k ON k.work_id = a.work_id
  JOIN authorship_institutions ai ON ai.work_id = a.work_id
  JOIN institutions i ON i.id = ai.institution_id
  WHERE i.is_uw = 0
  GROUP BY a.theme_id, i.id;

CREATE VIEW v_theme_org AS
  SELECT a.theme_id, o.id AS org_id, o.name, o.country, o.type,
         COUNT(DISTINCT a.work_id) AS n_works
  FROM v_work_theme_any a
  JOIN (SELECT DISTINCT work_id FROM faculty_works WHERE status = 'keep') k ON k.work_id = a.work_id
  JOIN authorship_institutions ai ON ai.work_id = a.work_id
  JOIN institutions i ON i.id = ai.institution_id
  JOIN institutions o ON o.id = COALESCE(i.org_id, i.id)
  WHERE i.is_uw = 0 AND o.is_uw = 0
  GROUP BY a.theme_id, o.id;
"""


def sid(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


def abstract_text(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos = [(i, word) for word, idxs in inv.items() for i in idxs]
    return " ".join(word for _, word in sorted(pos))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", type=Path, default=DATA / "topic_tags.jsonl")
    ap.add_argument("--out", type=Path, default=DB)
    args = ap.parse_args()

    roster = json.loads((DATA / "roster.json").read_text())
    overrides = yaml.safe_load((CONFIG / "faculty_overrides.yaml").read_text())
    index = json.loads((DATA / "works_index.json").read_text())
    attribution = json.loads((DATA / "attribution.json").read_text())
    # M4 outputs (optional: without them institutions come straight from the works records)
    norm_path, match_path = DATA / "institutions.json", DATA / "affiliation_matches.json"
    norm = json.loads(norm_path.read_text()) if norm_path.exists() else {}
    aff_matches = json.loads(match_path.read_text()) if match_path.exists() else {}
    con_rows_missing: list[tuple] = []
    orcid_path = DATA / "orcid.json"
    orcid = json.loads(orcid_path.read_text()) if orcid_path.exists() else {}
    # byline slot of the faculty member in works found only through ORCID
    for slug, o in orcid.items():
        o["confirmed_set"] = set(o["confirmed"])
        for r in o["not_in_openalex"] + o["no_doi"]:
            con_rows_missing.append((slug, r["title"], r["year"], (r["dois"] or [None])[0], r["type"]))
    orcid_slot = {(wid, idx): slug for slug, o in orcid.items() for wid, idx in o["added"].items()}

    args.out.unlink(missing_ok=True)
    con = sqlite3.connect(args.out)
    con.executescript(SCHEMA)

    # ---- faculty
    author_to_slug: dict[str, str] = {}
    for f in roster:
        ov = overrides.get(f["slug"], {})
        con.execute("INSERT INTO faculty VALUES (?,?,?,?,?,?,?,?,?,?)", (
            f["slug"], f["name"], f["rank"], json.dumps(f["titles"]),
            json.dumps(f["research_areas"]), f["profile_url"], ov.get("orcid") or f.get("orcid"),
            f.get("google_scholar_id"), f["research_blurb"], ov.get("status")))
        for e in ov.get("openalex", []):
            con.execute("INSERT INTO faculty_author_ids VALUES (?,?,?,?,?,?)", (
                f["slug"], e["id"], e["decision"], e["method"], e.get("confidence"), e.get("reason")))
            if e["decision"] == "include":
                author_to_slug[e["id"]] = f["slug"]

    # ---- works, authorships, institutions
    seen_works: set[str] = set()
    insts: dict[str, tuple] = {}
    for slug, meta in index.items():
        path = RAW / "works" / f"{slug}.jsonl"
        if not path.exists():
            continue
        flags = attribution.get(slug, {}).get("works", {})
        my_ids = set(meta["author_ids"])
        for line in path.open():
            w = json.loads(line)
            wid = sid(w["id"])
            auths = w.get("authorships") or []

            if wid not in seen_works:
                seen_works.add(wid)
                pt = w.get("primary_topic") or {}
                loc = (w.get("primary_location") or {}).get("source") or {}
                oa = w.get("open_access") or {}
                con.execute("INSERT INTO works VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    wid, (w.get("doi") or "").replace("https://doi.org/", "") or None,
                    w.get("title"), w.get("publication_year"), w.get("publication_date"),
                    w.get("type"), w.get("language"), loc.get("display_name"), loc.get("type"),
                    w.get("cited_by_count"), int(bool(oa.get("is_oa"))), oa.get("oa_status"),
                    pt.get("display_name"), (pt.get("subfield") or {}).get("display_name"),
                    (pt.get("field") or {}).get("display_name"),
                    (pt.get("domain") or {}).get("display_name"),
                    json.dumps([t["display_name"] for t in (w.get("topics") or [])]),
                    json.dumps([k["display_name"] for k in (w.get("keywords") or [])]),
                    abstract_text(w.get("abstract_inverted_index")), len(auths),
                    int(bool(w.get("is_authors_truncated"))), int(bool(w.get("is_retracted"))),
                    int(bool(w.get("is_paratext")))))
                for idx, a in enumerate(auths):
                    aid = sid(a["author"].get("id"))
                    con.execute("INSERT INTO authorships VALUES (?,?,?,?,?,?,?,?)", (
                        wid, idx, aid, a["author"].get("display_name"), a.get("raw_author_name"),
                        a.get("author_position"), int(bool(a.get("is_corresponding"))),
                        author_to_slug.get(aid) or orcid_slot.get((wid, idx))))
                    if not a.get("institutions"):
                        for raw in a.get("raw_affiliation_strings") or []:
                            m = aff_matches.get(" ".join(raw.split()))
                            if m:
                                con.execute("INSERT OR IGNORE INTO authorship_institutions VALUES (?,?,?,?)",
                                            (wid, idx, m["openalex_id"], m["source"]))
                    for inst in a.get("institutions") or []:
                        iid = sid(inst.get("id"))
                        if not iid:
                            continue
                        con.execute("INSERT OR IGNORE INTO authorship_institutions VALUES (?,?,?,?)",
                                    (wid, idx, iid, "openalex"))
                        insts.setdefault(iid, (
                            iid, inst.get("display_name"),
                            (inst.get("ror") or "").replace("https://ror.org/", "") or None,
                            inst.get("country_code"), inst.get("type"),
                            json.dumps([sid(x) for x in inst.get("lineage") or []]),
                            int(f"https://openalex.org/{UW_OPENALEX_ID}" in (inst.get("lineage") or [])
                                or iid == UW_OPENALEX_ID)))

            o = orcid.get(slug)
            added = (o or {}).get("added", {})
            mine = next((a for a in auths if sid(a["author"].get("id")) in my_ids), {})
            if not mine and added.get(wid, -1) >= 0:
                mine = auths[added[wid]]
            in_orcid = None if o is None else int(wid in added or wid in o["confirmed_set"])
            flag = flags.get(wid, {})
            con.execute("INSERT OR REPLACE INTO faculty_works VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                slug, wid, flag.get("status", "keep"), json.dumps(flag.get("reasons", [])),
                flag.get("method"), flag.get("note"), mine.get("author_position"),
                int(bool(mine.get("is_corresponding"))),
                int(any(sid(i.get("id")) == UW_OPENALEX_ID for i in mine.get("institutions") or [])),
                "orcid" if wid in added else "openalex", in_orcid))

    # institutions: every one referenced by an authorship, plus the orgs they roll up to
    used = {r[0] for r in con.execute("SELECT DISTINCT institution_id FROM authorship_institutions")}
    need = used | {norm[i]["org_id"] for i in used if i in norm}
    rows = []
    for iid in need:
        n = norm.get(iid)
        base = insts.get(iid)
        if n:
            rows.append((iid, n["name"], n["ror"], n["country"], n["type"],
                         base[5] if base else None, n["is_uw"], n["city"], n["region"],
                         n["lat"], n["lon"], n["parent_id"], n["org_id"]))
        elif base:
            rows.append(base + (None, None, None, None, None, iid))
    con.executemany("INSERT INTO orcid_missing VALUES (?,?,?,?,?)", con_rows_missing)
    con.executemany("INSERT INTO institutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    n_tagged = load_topics(con, args.tags)
    con.commit()

    q = lambda sql: con.execute(sql).fetchone()[0]
    st = dict(con.execute("SELECT status, COUNT(*) FROM faculty_works GROUP BY status").fetchall())
    print(f"Built {args.out}")
    print(f"  faculty              {q('SELECT COUNT(*) FROM faculty')}")
    print(f"  works                {q('SELECT COUNT(*) FROM works')}")
    print(f"  faculty-work links   {sum(st.values())} "
          f"(keep {st.get('keep', 0)}, suspect {st.get('suspect', 0)}, drop {st.get('drop', 0)})")
    print(f"  authorships          {q('SELECT COUNT(*) FROM authorships')}")
    print(f"  institutions         {q('SELECT COUNT(*) FROM institutions')} "
          f"(with coordinates {q('SELECT COUNT(*) FROM institutions WHERE lat IS NOT NULL')}, "
          f"partner orgs {q('SELECT COUNT(DISTINCT org_id) FROM institutions')})")
    n_ror = q("SELECT COUNT(*) FROM authorship_institutions WHERE source != 'openalex'")
    print(f"  ror-matched affils   {n_ror}")
    print(f"  truncated bylines    {q('SELECT COUNT(*) FROM works WHERE authors_truncated=1')}")
    print(f"  topic-tagged works   {n_tagged}")
    if orcid:
        n_listed = q("SELECT COUNT(*) FROM faculty_works WHERE in_orcid=1")
        n_only = q("SELECT COUNT(*) FROM faculty_works WHERE source='orcid'")
        print(f"  ORCID                {len(orcid)} faculty; {n_listed} links listed in ORCID, "
              f"{n_only} found only through ORCID, {len(con_rows_missing)} ORCID works missing from OpenAlex")
    con.close()


def load_topics(con: sqlite3.Connection, tags_path: Path) -> int:
    """Themes from the taxonomy; tags from topic_tags.jsonl (only current-taxonomy rows)."""
    tax_file = CONFIG / "topic_taxonomy.yaml"
    if not tax_file.exists():
        return 0
    tax = yaml.safe_load(tax_file.read_text())
    sort = 0
    for t in tax["themes"]:
        sort += 1
        con.execute("INSERT INTO themes VALUES (?,?,?,?,?)", (t["id"], t["name"], None, None, sort))
        for s in t["subtopics"]:
            sort += 1
            con.execute("INSERT INTO themes VALUES (?,?,?,?,?)",
                        (s["id"], s["name"], s.get("description"), t["id"], sort))
    if not tags_path.exists():
        return 0
    known = {r[0] for r in con.execute("SELECT id FROM works")}
    valid = {r[0] for r in con.execute("SELECT id FROM themes")}
    n = 0
    for line in tags_path.open():
        r = json.loads(line)
        wid = r["work_id"]
        if wid not in known or r.get("primary_theme") not in valid:
            continue
        n += 1
        con.execute("INSERT OR IGNORE INTO work_themes VALUES (?,?,?,?)",
                    (wid, r["primary_theme"], "primary_theme", 0))
        for rank, s in enumerate(r.get("subtopics", [])):
            if s in valid:
                con.execute("INSERT OR IGNORE INTO work_themes VALUES (?,?,?,?)", (wid, s, "subtopic", rank))
        for kind, key in [("taxon_group", "taxon_groups"), ("species", "species"),
                          ("ecosystem", "ecosystems"), ("region", "regions"), ("method", "methods")]:
            for v in r.get(key, []):
                if v and v != "none":
                    con.execute("INSERT OR IGNORE INTO work_tags VALUES (?,?,?)", (wid, kind, v.strip()))
        con.execute("INSERT OR REPLACE INTO work_summaries VALUES (?,?,?)",
                    (wid, r.get("summary"), r.get("model")))
    return n


if __name__ == "__main__":
    main()
