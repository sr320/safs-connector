# SAFS Connector: Plan

Goal: collect every publication by School of Aquatic and Fishery Sciences (SAFS) faculty, store it in a versioned database, and publish an explorer on GitHub Pages. The explorer should show how **faculty ↔ topics ↔ co-author institutions** connect.

---

## 0. What we checked (2026-11-19)

| Check | Finding | What it means for the design |
|---|---|---|
| `fish.uw.edu/faculty-research/` | 33 faculty profile URLs (`/faculty/<slug>/`), in static HTML | Easy to scrape. This is the roster source of truth. |
| Faculty profile pages | Some link to Google Scholar, but Scholar links to students and collaborators show up too | Scholar is only a hint. We can't scrape it reliably (ToS and bot blocking). |
| OpenAlex author search + UW filter (`I201448701`) | Trevor Branch → `A5044538187` (ORCID, 182 works). Steven Roberts → `A5022496160` (ORCID, 235 works) **plus 4 small split profiles** | OpenAlex is the main data source. Author disambiguation needs a manual check and merge step. |

Rough scale: about 33 faculty × about 150 works each ≈ **5,000 papers** and about 30–60k author-institution links. That fits easily in a static site.

---

## 1. Architecture

```
roster scrape ──► author resolution ──► works harvest ──► enrichment (Claude) ──► build DB ──► static explorer
 (fish.uw.edu)    (OpenAlex + ORCID      (OpenAlex works   (topic tags, inst.     (SQLite +     (GitHub Pages)
                   + manual overrides)    API, cursor)      normalization)         JSON/Parquet)
                         ▲                                                         │
                         └──────── GitHub Actions (monthly cron) ◄─────────────────┘
```

- **Language:** Python 3.12 (`httpx`, `pydantic`, `anthropic`, `duckdb`/`sqlite3`).
- **Storage:** the source of truth is a **SQLite file committed to the repo** (or released as a build artifact if it grows past about 50 MB). The build also exports compact JSON and Parquet for the front end.
- **Hosting:** GitHub Pages serves a static front end. The site needs **no server and no API key in the browser**. All Claude calls run in Actions or locally.

### Repo layout
```
safs-connector/
├── config/
│   ├── faculty_overrides.yaml   # hand-curated: slug → OpenAlex IDs / ORCID, merges, exclusions
│   └── topic_taxonomy.yaml      # SAFS topic vocabulary (Claude-drafted, human-edited)
├── pipeline/
│   ├── 01_roster.py             # scrape faculty list + profile metadata          (M1)
│   ├── 02_resolve_authors.py    # map faculty → OpenAlex author IDs               (M1)
│   ├── 03_harvest_works.py      # pull all works + authorships + institutions     (M2)
│   ├── 04_check_attribution.py  # flag papers by namesakes (heuristic + Claude)   (M2)
│   ├── 05_enrich_topics.py      # Claude batch topic tagging                      (M3)
│   ├── 06_normalize_insts.py    # ROR-based institution cleanup (+ Claude)        (M4)
│   ├── 07_build_db.py           # SQLite schema + derived views                   (M2)
│   ├── 08_coverage_report.py    # per-faculty coverage table                      (M2)
│   ├── 09_spotcheck.py          # human spot-check of topic tags                  (M3)
│   └── 10_export_site_data.py   # JSON/Parquet for the explorer                   (M5)
├── data/                        # safs.sqlite, raw cache (gitignored), exports
├── site/                        # explorer front end
└── .github/workflows/
    ├── refresh.yml              # monthly cron: pipeline → commit data → deploy
    └── pages.yml
```

---

## 2. Pipeline steps

### Step 1: Roster (`01_roster.py`)
- Scrape `/faculty-research/` and each `/faculty/<slug>/` page. Capture name, title (Professor, Associate, Affiliate, Emeritus, and so on), research-area blurb, lab URL, and any ORCID or Scholar link.
- Emit `roster.json`. Keep a `status` field so affiliate and emeritus faculty can be filtered later.
- Be polite: one request per second, a cached HTML snapshot, and a generic User-Agent.

### Step 2: Author resolution (`02_resolve_authors.py`). **This step needs a human check.**
1. If the profile or ORCID gives an ORCID, query `authors?filter=orcid:<id>`. This is the best match.
2. Otherwise, search `authors?search=<name>&filter=affiliations.institution.id:I201448701`. Use `affiliations` so it also matches people whose *last known* institution isn't UW.
3. **Collect split profiles.** Gather every candidate author ID that shares the name and has UW or prior-institution overlap. Then run a **Claude disambiguation pass**: give it each candidate's top titles, venues, years, and institutions next to the faculty member's SAFS research blurb, and get back structured output `{author_id, same_person: bool, confidence, reason}`.
4. Write the results to `config/faculty_overrides.yaml` for **human review**. Each faculty member can confirm their own row in a PR. Later runs treat this file as authoritative.

### Step 3: Works harvest (`03_harvest_works.py`)
- `works?filter=author.id:A1|A2|…&per-page=200&cursor=*` for each faculty member, with all merged IDs combined.
- Keep: DOI, title, year, venue, type, abstract (rebuilt from `abstract_inverted_index`), OpenAlex `topics`/`keywords`, citation count, OA status, and the full `authorships` list (author ID, position, raw affiliation strings, institution IDs, ROR, country, type).
- Deduplicate by DOI, falling back to the OpenAlex work ID. Drop paratext, errata, and retracted items (flagged).
- Use an OpenAlex API key or the `mailto` polite pool (a project contact address in an env var, not committed). Cache raw JSON so reruns are incremental (`from_updated_date`).
- *Optional cross-checks:* Crossref and ORCID works lists catch papers OpenAlex missed. A per-faculty coverage report compares the count against Scholar's total.

### Step 4: Topic enrichment with the Claude API (`05_enrich_topics.py`)
OpenAlex `topics` are generic (for example, "Fish Ecology and Management"). SAFS needs a sharper vocabulary that matches how the school actually describes its work.
1. **Build the taxonomy, once.** Send Claude a sample of about 400 titles and abstracts plus all 33 faculty research blurbs. Ask it to propose a 2-level taxonomy of about 12 themes and about 60 subtopics, such as *Fisheries Management → Stock assessment*, *Aquaculture → Shellfish physiology*, *Conservation genetics*, *Climate & ocean change*, *Marine mammals*, *Freshwater ecology*, and *Seafood trade & food systems*. A person edits the result in `topic_taxonomy.yaml`.
2. **Tag each paper.** Use the **Message Batches API** (50% cheaper and async) with **structured outputs** that return:
   `{primary_theme, subtopics[≤4], taxa[], ecosystems[], regions[], methods[], one_line_summary}`.
   Put the system prompt and taxonomy behind a `cache_control` breakpoint so the shared prefix is cached across requests.
3. **Model:** `claude-opus-5-5` at `effort: "low"`. Estimate for about 5k papers: about 3M input / 0.75M output tokens. At batch pricing that's **roughly $15**, and less with caching. Switching to `claude-haiku-5-5` would cut that a lot more. I'd only do that after spot-checking about 100 papers against the Opus tags.
4. Store the model ID, prompt hash, and timestamp per row so a rerun only tags new or changed papers.

Other good uses of Claude:
- Disambiguating split author profiles (Step 2).
- Normalizing institutions that have no ROR ID (Step 5).
- Writing a short "research portrait" for each faculty member and each theme for the explorer's side panels.

### Step 5: Institution normalization (`06_normalize_insts.py`)
- Prefer the OpenAlex institution ID with ROR. Roll child institutions up to a parent where that helps (for example, NOAA NWFSC → NOAA), using the ROR `relationships` field.
- For affiliation strings with no ID, send batches to Claude. It maps the raw string to a ROR candidate (looked up through the ROR API) or flags it as unknown.
- Add type (education, government, nonprofit, company, and so on), country, and lat/lon from ROR for the map.
- Exclude UW itself from "external collaborator" metrics, but keep it as a node.

### Step 6: Database schema (`07_build_db.py`)
```sql
faculty(id, slug, name, rank, status, profile_url, orcid, research_blurb)
faculty_author_ids(faculty_id, openalex_author_id, verified_by, verified_at)
works(id, doi, title, year, venue, type, abstract, cited_by, oa_status, openalex_topics_json)
authorships(work_id, position, openalex_author_id, display_name, is_safs_faculty, faculty_id NULL)
authorship_institutions(work_id, openalex_author_id, institution_id)
institutions(id, ror, name, parent_id, type, country, lat, lon)
themes(id, name, parent_id)                       -- from topic_taxonomy.yaml
work_themes(work_id, theme_id, role)              -- primary / secondary
work_tags(work_id, kind, value)                   -- taxa, ecosystem, region, method
-- derived / materialized
faculty_theme(faculty_id, theme_id, n_works, share)
faculty_institution(faculty_id, institution_id, n_works, first_year, last_year)
theme_institution(theme_id, institution_id, n_works)
faculty_faculty(a_id, b_id, n_coauthored)         -- internal SAFS collaboration
```
The export step writes `graph.json` (nodes and weighted edges), `works.json` (a slim search index), and `institutions.geojson`.

---

## 3. Explorer (GitHub Pages)

A static site built with **Observable Framework**, or plain Vite with D3. Observable Framework fits well: its data loaders can run the export step at build time, it deploys cleanly to Pages, and DuckDB-WASM can query Parquet in the browser.

Views:
1. **Connection graph.** A tripartite force or bipartite network of Faculty ↔ Themes ↔ Institutions. Filters: year range, theme, institution type, country. Clicking any node highlights its neighbors and lists the underlying papers.
2. **Theme map.** A UMAP/t-SNE scatter of paper embeddings, colored by theme. It shows where faculty overlap and where there are bridging papers.
3. **Collaborator map.** A world map of co-author institutions, sized by paper count and filterable by faculty or theme.
4. **Faculty cards.** Publication timeline, theme mix (stacked bars), top external partners, internal SAFS co-authors, and a Claude-written research portrait.
5. **"Who else works on…?"** Pick a theme, taxon, or region and see the SAFS faculty and outside institutions ranked by activity. This is the view most likely to spark new collaborations.
6. **Search and table.** Full-text search over titles and abstracts, downloadable as CSV.

Embeddings for the theme map: compute them at build time with a local open model (for example, `sentence-transformers`). The Claude API doesn't provide embeddings. Store 2-D coordinates only.

---

## 4. Automation and governance
- **GitHub Actions `refresh.yml`:** monthly cron plus manual dispatch. It runs the pipeline incrementally, opens a PR with the data diff and a coverage report, and deploys after merge.
- **Secrets:** `ANTHROPIC_API_KEY` and `OPENALEX_API_KEY` (or the mailto address) as repo secrets. They are never shipped to the site.
- **Human checks:** `faculty_overrides.yaml` (identity) and `topic_taxonomy.yaml` (vocabulary) change only through reviewed PRs. Add a "report a missing or incorrect paper" link to a GitHub issue template.
- **Licensing:** OpenAlex data is CC0. Store abstracts only where OpenAlex provides them, and link out to the DOI for full text.

---

## 5. Milestones

| # | Milestone | Done when |
|---|---|---|
| M1 | Roster and author resolution | `faculty_overrides.yaml` lists all 33 faculty with verified OpenAlex IDs. Split profiles are merged. |
| M2 | Harvest and DB | `safs.sqlite` holds all works and authorships. The coverage report has been compared against Scholar counts. |
| M3 | Claude enrichment | Taxonomy approved. Every paper is tagged. A 100-paper spot check reaches ≥90% agreement. |
| M4 | Institutions | ≥95% of authorship institutions resolve to ROR. Map coordinates are filled in. |
| M5 | Explorer MVP | Graph, faculty cards, and search are live on GitHub Pages. |
| M6 | Automation | The monthly Action refreshes the data through a PR. Theme map and collaborator map are added. |

## 6. Open decisions
- **Scope of "faculty":** core only, or also affiliate, adjunct, emeritus, and research faculty? The roster scrape keeps rank so this can be a filter either way.
- **Time window:** full career, or only papers written while at SAFS? Affiliation history in OpenAlex makes an "at UW" toggle possible.
- **Enrichment model:** Opus 5.5 (default, about $15) or Haiku 5.5 (cheaper) after the spot check.
