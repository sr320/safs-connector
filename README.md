# safs-connector

A publication database and explorer for UW School of Aquatic and Fishery Sciences (SAFS) faculty. It shows how faculty, research topics, and co-author institutions connect. See [PLAN.md](PLAN.md) for the full design.

## Setup

```bash
uv sync
```

Optional environment variables:
- `ANTHROPIC_API_KEY`: turns on Claude for author disambiguation. Without it, a heuristic is used.
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
