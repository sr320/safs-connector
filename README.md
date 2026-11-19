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

The vocabulary is in **`config/topic_taxonomy.yaml`**. It has 15 themes and 66 subtopics, plus controlled lists of taxon groups, ecosystems, and methods. Claude drafted it from faculty profiles and each person's most-cited papers. **Edit it freely before tagging.** Renaming a `name` is safe, but changing an `id` means re-tagging.

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
