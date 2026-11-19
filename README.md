# safs-connector

A publication database and explorer for UW School of Aquatic and Fishery Sciences (SAFS) faculty. It shows how faculty, research topics, and co-author institutions connect. See [PLAN.md](PLAN.md) for the full design.

## Setup

```bash
uv sync
```

Optional environment variables:
- `ANTHROPIC_API_KEY`: turns on Claude for author disambiguation and for reviewing papers that may be misattributed. Without it, heuristics are used.
- `OPENALEX_API_KEY` or `OPENALEX_MAILTO`: puts requests in OpenAlex's polite pool.

## Pipeline (M1: roster and author resolution)

```bash
uv run python pipeline/01_roster.py              # -> data/roster.json
uv run python pipeline/02_resolve_authors.py     # -> config/faculty_overrides.yaml
```

| File | What it is |
|---|---|
| `data/roster.json` | 33 faculty scraped from fish.uw.edu: titles, rank, research areas, Google Scholar ID, bio |
| `data/author_candidates.json` | Evidence for every OpenAlex candidate: topics, UW years, most-cited titles |
| `config/faculty_overrides.yaml` | **Human-reviewed** mapping of faculty to OpenAlex author IDs |

HTTP responses are cached in `data/raw/` (gitignored), so reruns don't re-download.

### How resolution works
1. Search OpenAlex for UW-affiliated authors matching each name, including nickname and accent variants. If nothing matches, search without the UW filter.
2. Keep only name-compatible candidates. A different spelled-out first name or a conflicting middle initial rules a candidate out.
3. Decide for each candidate: **include**, **exclude**, or **review**.
   - **Claude** (`claude-opus-5-5`, structured output) compares each candidate's titles and topics to the faculty member's SAFS profile. This is used whenever Anthropic credentials are available. Server-side refusal fallback is turned on.
   - **Heuristic** (`--no-llm`): the primary profile is the one with the most works whose topic fields are mostly aquatic or fisheries related, not simply the largest. Fragments are compared to it by topic overlap.

### Reviewing `config/faculty_overrides.yaml`
- To correct a candidate, change its `decision` and set `method: human`.
- To freeze a faculty member's whole entry, set `verified: true`.
- To add a missing profile, add a new candidate with `method: human`.

Reruns keep all human edits. Entries with `status: needs_review` need attention.

## Pipeline (M2: papers and database)

```bash
uv run python pipeline/03_harvest_works.py       # -> data/raw/works/*.jsonl, data/works_index.json
uv run python pipeline/04_check_attribution.py   # -> data/attribution.json
uv run python pipeline/07_build_db.py            # -> data/safs.sqlite
uv run python pipeline/08_coverage_report.py     # -> data/coverage.md
```

| File | What it is |
|---|---|
| `data/safs.sqlite` | The database. It is rebuilt from scratch on every run. |
| `data/coverage.md` | Per-faculty counts of kept, suspect, and dropped papers, year span, and top partners, with a Google Scholar link for comparison |
| `data/attribution.json` | Flags on papers that may belong to someone else with the same name |
| `config/work_overrides.yaml` | **Human** keep or drop decisions on individual papers |

### Misattributed papers
Even the right OpenAlex profile can contain a namesake's papers, such as 1960s urology, 1970s astronomy, or 1940s metallurgy. `04_check_attribution.py` builds an "anchor" set: the papers whose byline for that faculty member lists UW. It then flags each other paper as:
- **off-topic**: no topic overlap with the anchor set,
- **pre-career**: 12 or more years before the first anchor paper and a weak topic fit, or
- **implausibly early**: 25 or more years before the first anchor paper.

With Anthropic credentials, Claude makes the final keep or drop call on each suspect. Without them, suspects stay in the database with `status = 'suspect'` but are left out of every `v_*` view. Errata and retractions are dropped.

### Querying
```sql
-- top external partner institutions for one faculty member
SELECT name, country, n_works FROM v_faculty_institution
WHERE faculty_slug = 'trevor-branch' ORDER BY n_works DESC LIMIT 10;

-- internal SAFS co-authorship network
SELECT * FROM v_faculty_faculty ORDER BY n_works DESC;
```

Note: OpenAlex lists at most 100 authors per paper in list responses, and 12 papers have truncated bylines. Without an API key, OpenAlex blocks cursor paging, so the harvester uses page numbers (up to 10k results per query).

## Pipeline (M3: topic tagging with Claude)

The vocabulary is in **`config/topic_taxonomy.yaml`**. It has 15 themes and 68 subtopics, plus controlled lists of taxon groups, ecosystems, and methods. Claude drafted it from faculty profiles and each person's most-cited papers. **Edit it freely before tagging.** Renaming a `name` is safe, but changing an `id` means re-tagging.

```bash
uv run --env-file .env python pipeline/05_enrich_topics.py plan                 # no API calls: counts and cost estimate
uv run --env-file .env python pipeline/05_enrich_topics.py submit --limit 100 --sample
uv run --env-file .env python pipeline/05_enrich_topics.py collect              # waits for the batch, writes data/topic_tags.jsonl
uv run python pipeline/07_build_db.py && uv run python pipeline/09_spotcheck.py sample
# open data/spotcheck.csv, mark agree_theme / agree_subtopics as y or n, then:
uv run python pipeline/09_spotcheck.py score                                    # target: at least 90%
uv run --env-file .env python pipeline/05_enrich_topics.py submit               # tag all remaining papers
uv run --env-file .env python pipeline/05_enrich_topics.py collect
uv run python pipeline/07_build_db.py
```

- **How it runs:** the Message Batches API at half price, using `claude-opus-5-5` at low effort. Each request covers 8 papers and returns structured JSON that must match the taxonomy. The taxonomy system prompt is cached across requests.
- **Cost:** about **$11** for all ~5,300 papers.
- **Each paper gets:** a primary theme, 1–4 subtopics, taxon groups, species, ecosystems, regions, methods, and a one-sentence summary.
- **Reruns:** only untagged papers are sent. Use `--retag` after changing taxonomy ids. Failed or refused requests are recorded for retry.
- **New database tables and views:** tables `themes`, `work_themes`, `work_tags`, and `work_summaries`, and views `v_faculty_theme`, `v_faculty_subtopic`, and `v_theme_institution`, which shows which outside institutions work with SAFS on each theme.

## Pipeline (M4: co-author institutions)

```bash
uv run python pipeline/06_normalize_insts.py     # -> data/institutions.json, data/affiliation_matches.json
uv run python pipeline/07_build_db.py
```

| File | What it is |
|---|---|
| `data/institutions.json` | Every co-author institution and its ancestors: ROR id, type, city, coordinates, parent, partner organization |
| `data/affiliation_matches.json` | Free-text affiliations (no OpenAlex institution) matched to ROR |
| `config/institution_overrides.yaml` | **Human** corrections to the rollup and to affiliation matches |

- **Coverage:** 95.7% of authorships that list any affiliation resolve to a ROR institution, and every institution has map coordinates.
- **Partner organizations:** each institution rolls up to the organization a person would name as the partner. For example, the NOAA fisheries science centers roll up to NOAA, Hopkins Marine Station to Stanford, and Scripps to UC San Diego. Rollup stops below national governments and ministries, university systems (so UC campuses stay separate), and very broad umbrellas such as the UN, EU, and CGIAR. To change a rollup, add a line under `rollup:` in the overrides file.
- **Free-text affiliations:** ROR's affiliation matcher is used, and only its confident `chosen` matches are kept. That recovers 31 authorships. The rest are mostly addresses without an organization name.
- **New views:** `v_faculty_org`, `v_org_summary` (with city and coordinates, ready for the map), and `v_theme_org`. The institution-level views are unchanged. `data/coverage.md` now lists top partners by organization.

```sql
-- SAFS's top partner organizations
SELECT name, country, n_faculty, faculty_work_links FROM v_org_summary
ORDER BY faculty_work_links DESC LIMIT 10;
```

## Explorer (M5)

A static site in `site/` (plain HTML, D3, no build step), deployed to GitHub Pages by `.github/workflows/pages.yml` on every push to `main`.

```bash
uv run python pipeline/10_export_site_data.py    # -> site/data/meta.json, site/data/works.json
python3 -m http.server 8791 --directory site     # preview at http://localhost:8791
```

- **Network:** research themes on the left, SAFS faculty in the middle, and partner organizations on the right. Line width shows shared papers. Click a circle for its connections. Click a connection for the papers behind it.
- **Faculty:** a card for each person with papers per year, theme mix, top subtopics, partner organizations, SAFS co-authors, organisms and species, and most-cited papers. Each card has its own shareable URL, such as `#faculty/steven-roberts`.
- **Search:** full-text search over titles, Claude summaries, species, regions, and subtopics, with CSV download.
- **Filters:** the year range and theme filters apply to every view. All views are computed in the browser from `works.json`, which holds only kept papers.

**One-time setup:** in the repo, go to Settings → Pages → Build and deployment → Source, and choose **GitHub Actions**.

## M6: maps and monthly refresh

**Two new explorer tabs**
- **Collaborator map:** a world map of partner organizations, sized by shared papers. It can be filtered by faculty member and by the global year and theme filters. Click a circle for the faculty and papers behind it.
- **Theme map:** every paper is one dot, and papers on similar subjects sit close together. Pick one or two faculty members to see where their work sits, where it overlaps, and which papers they wrote together. Theme names mark the center of each theme.

```bash
uv run python pipeline/11_theme_map.py    # -> site/data/theme_map.json
```
The theme map embeds each paper's title, Claude summary, subtopics, and abstract opening with a small local model (`BAAI/bge-small-en-v1.5` via fastembed). The Claude API has no embeddings endpoint. UMAP then projects the embeddings to 2-D. Embeddings are cached in `data/raw/`, so later runs only embed new papers.

**Monthly refresh** (`.github/workflows/refresh.yml`, which runs on the 2nd of each month or manually from the Actions tab):
1. Re-scrapes the roster. Claude resolves only newly listed faculty (`02_resolve_authors.py --new-only`).
2. Re-harvests papers. Claude reviews only new suspect papers; earlier decisions are reused.
3. Claude tags only papers that are not yet tagged. The run stops, instead of spending, if more than 1,500 papers need tagging, which usually means the taxonomy changed.
4. Rebuilds institutions, the database, the coverage report, and the site data. It then opens a pull request. Merging it redeploys the site.

The SQLite database is no longer committed; at 26 MB per monthly change it would bloat the repo. Each refresh attaches `safs.sqlite.gz` to a `data-YYYY-MM-DD` release. Locally, `07_build_db.py` rebuilds it.

**One-time setup** (in repo Settings):
- Add `ANTHROPIC_API_KEY` as an Actions secret. `OPENALEX_API_KEY` is optional.
- Under Actions → General → Workflow permissions, allow GitHub Actions to create pull requests.
