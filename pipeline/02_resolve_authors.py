"""Step 2: map each SAFS faculty member to their OpenAlex author ID(s).

OpenAlex often splits one person into a large "primary" profile plus small
fragments, and sometimes merges different people into a fragment. This step:

  1. searches OpenAlex for UW-affiliated authors matching each faculty name
     (with nickname / accent variants),
  2. keeps name-compatible candidates and gathers evidence for each
     (top topics, UW years, most-cited titles),
  3. decides which candidates are the faculty member:
       - with ANTHROPIC credentials: Claude judges every candidate against the
         faculty member's SAFS research profile (structured output);
       - otherwise: a topic-overlap heuristic, flagging anything uncertain,
  4. writes config/faculty_overrides.yaml for human review.

Human edits win: any faculty entry with `verified: true`, and any candidate
whose `method` is `human`, is preserved verbatim on re-runs.

Usage:
  uv run python pipeline/02_resolve_authors.py            # auto: Claude if creds, else heuristic
  uv run python pipeline/02_resolve_authors.py --no-llm   # force heuristic
  uv run python pipeline/02_resolve_authors.py --only steven-roberts
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from datetime import date
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from common import CONFIG, DATA, OPENALEX, UW_OPENALEX_ID, cached_get

OVERRIDES = CONFIG / "faculty_overrides.yaml"
EVIDENCE = DATA / "author_candidates.json"
CLAUDE_MODEL = "claude-opus-5-5"

NICKNAMES = {
    "chris": ["christopher", "christian"], "tom": ["thomas"], "tim": ["timothy"],
    "ken": ["kenneth"], "dan": ["daniel"], "steve": ["steven", "stephen"],
    "greg": ["gregory"], "andy": ["andrew"], "jim": ["james"], "bob": ["robert"],
    "mike": ["michael"], "rob": ["robert"], "ray": ["raymond"], "liz": ["elizabeth"],
    "kate": ["katherine", "kathryn"], "sam": ["samuel", "samantha"],
}
# reverse map: formal name -> nicknames
FORMAL = {f: [n] for n, fs in NICKNAMES.items() for f in fs}


# --------------------------------------------------------------------------- names
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.replace("‐", "-").replace("‑", "-")
    return re.sub(r"[^a-z\- ]", "", s.lower()).strip()


def first_variants(first: str) -> set[str]:
    f = norm(first)
    return {f, *NICKNAMES.get(f, []), *FORMAL.get(f, [])}


def query_variants(name: str) -> list[str]:
    parts = name.split()
    first, last = parts[0], " ".join(parts[1:])
    out = [name]
    for v in sorted(first_variants(first) - {norm(first)}):
        out.append(f"{v.title()} {last}")
    ascii_name = norm(name).title()
    if ascii_name.lower() != name.lower():
        out.append(ascii_name)
    return list(dict.fromkeys(out))


def _given_ok(toks: list[str], firsts: set[str], last_tokens: set[str],
              allow_initial: bool) -> bool:
    given = toks[0]
    if given in firsts:
        return True
    if len(given) == 1 and allow_initial and any(f.startswith(given) for f in firsts):
        # "C. Daniel Smith": a spelled-out second name that isn't the surname is
        # the name the person actually goes by -> must also match.
        nxt = next((t for t in toks[1:] if t not in last_tokens), None)
        return nxt is None or len(nxt) == 1 or nxt in firsts
    return False


def name_compatible(faculty_name: str, author: dict) -> bool:
    """Display name may match on an initial; alternatives must spell the first name.

    (OpenAlex alternatives like "C. Anderson" are too ambiguous on their own.)
    """
    parts = faculty_name.split()
    first, last = parts[0], parts[-1]
    last_tokens = {t for t in re.split(r"[\s\-]", norm(last)) if t}
    firsts = first_variants(first)
    names = [(author["display_name"], True)] + [
        (n, False) for n in (author.get("display_name_alternatives") or [])]
    for n, allow_initial in names:
        toks = [t for t in re.split(r"[\s\-\.]", norm(n)) if t]
        if len(toks) < 2 or not last_tokens & set(toks[1:]):
            continue
        if _given_ok(toks, firsts, last_tokens, allow_initial):
            return True
    return False


def middle_initial(display_name: str, faculty_name: str) -> str | None:
    """'Thomas P. Quinn' -> 'p'. None when the name has no middle part."""
    last_tokens = {t for t in re.split(r"[\s\-]", norm(faculty_name.split()[-1])) if t}
    toks = [t for t in re.split(r"[\s\.]", norm(display_name)) if t]
    mids = [t for t in toks[1:] if t not in last_tokens and not (set(t.split("-")) & last_tokens)]
    return mids[0][0] if mids else None


# --------------------------------------------------------------------------- OpenAlex
def short_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def search_candidates(name: str, uw_only: bool = True) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for q in query_variants(name):
        params = {"search": q, "per-page": 50}
        if uw_only:
            params["filter"] = f"affiliations.institution.id:{UW_OPENALEX_ID}"
        data = cached_get(f"{OPENALEX}/authors", params)
        for a in data.get("results", []):
            if name_compatible(name, a):
                found[short_id(a["id"])] = a
    return found


def top_titles(author_id: str, n: int = 8) -> list[str]:
    data = cached_get(f"{OPENALEX}/works", {
        "filter": f"author.id:{author_id}",
        "sort": "cited_by_count:desc",
        "per-page": n,
        "select": "title,publication_year",
    })
    return [f"{w.get('publication_year')}: {w.get('title')}" for w in data.get("results", [])
            if w.get("title")]


def summarize(author: dict) -> dict:
    uw_years = []
    other_insts = []
    for aff in author.get("affiliations") or []:
        if short_id(aff["institution"]["id"]) == UW_OPENALEX_ID:
            uw_years = aff.get("years") or []
        else:
            other_insts.append(aff["institution"]["display_name"])
    topics = author.get("topics") or []
    aid = short_id(author["id"])
    return {
        "id": aid,
        "display_name": author["display_name"],
        "orcid": (author.get("orcid") or "").replace("https://orcid.org/", "") or None,
        "works_count": author.get("works_count", 0),
        "cited_by_count": author.get("cited_by_count", 0),
        "uw_years": [min(uw_years), max(uw_years)] if uw_years else None,
        "other_institutions": other_insts[:8],
        "topic_ids": [short_id(t["id"]) for t in topics[:25]],
        "topics": [t["display_name"] for t in topics[:8]],
        "fields": sorted({t["field"]["display_name"] for t in topics[:8]}),
        "top_titles": top_titles(aid),
    }


# --------------------------------------------------------------------------- deciders
class CandidateDecision(BaseModel):
    openalex_id: str
    same_person: Literal["yes", "no", "unsure"]
    confidence: float = Field(description="0-1 confidence in the same_person judgment")
    reason: str = Field(description="One sentence citing the evidence used")


class Resolution(BaseModel):
    decisions: list[CandidateDecision]


# Resolve postponed annotations (from __future__ import annotations) up front.
CandidateDecision.model_rebuild(_types_namespace={"Literal": Literal})
Resolution.model_rebuild(_types_namespace={"CandidateDecision": CandidateDecision})


SYSTEM = """You resolve author identities for a publication database of University of \
Washington School of Aquatic and Fishery Sciences (SAFS) faculty.

You get one faculty member's SAFS profile and a list of OpenAlex author records that \
match their name and have a UW affiliation. OpenAlex often splits one real person into \
a large profile plus small fragments, and small fragments can mix in papers by a \
different person with the same name (e.g. a UW medical researcher).

For EACH candidate decide whether it is this faculty member:
- "yes": topics/titles clearly fit the faculty member's research (aquatic, fisheries, \
marine, freshwater, ecology, genetics, aquaculture, fisheries economics, etc. as \
appropriate to THEIR profile), and timing is plausible.
- "no": the work is clearly in an unrelated field (e.g. clinical medicine, housing \
economics) or the career timing is impossible.
- "unsure": mixed or too little evidence. A fragment mixing relevant and irrelevant \
papers is "unsure".
Judge from the evidence only; do not assume the largest profile is correct."""


def decide_with_claude(fac: dict, cands: list[dict]) -> dict[str, dict]:
    import anthropic

    client = anthropic.Anthropic()
    payload = {
        "faculty": {
            "name": fac["name"],
            "titles": fac["titles"],
            "research_areas": fac["research_areas"],
            "profile_excerpt": fac["research_blurb"][:1500],
        },
        "candidates": [{k: c[k] for k in (
            "id", "display_name", "orcid", "works_count", "uw_years",
            "other_institutions", "topics", "fields", "top_titles")} for c in cands],
    }
    resp = client.beta.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_format=Resolution,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        print(f"    ! Claude returned no decision ({resp.stop_reason}); using heuristic")
        return decide_heuristic(fac, cands)

    out = {}
    for d in resp.parsed_output.decisions:
        out[d.openalex_id] = {
            "decision": {"yes": "include", "no": "exclude", "unsure": "review"}[d.same_person],
            "confidence": round(d.confidence, 2),
            "reason": d.reason,
            "method": f"claude:{resp.model}",
        }
    for c in cands:  # anything Claude skipped
        out.setdefault(c["id"], {"decision": "review", "confidence": 0.0,
                                 "reason": "not judged by model", "method": "claude"})
    return out


SAFS_FIELDS = {
    "Agricultural and Biological Sciences", "Environmental Science",
    "Earth and Planetary Sciences", "Biochemistry, Genetics and Molecular Biology",
    "Immunology and Microbiology", "Economics, Econometrics and Finance",
    "Mathematics", "Decision Sciences", "Social Sciences",
}


def field_fit(c: dict) -> float:
    return len(set(c["fields"]) & SAFS_FIELDS) / max(len(c["fields"]), 1)


def decide_heuristic(fac: dict, cands: list[dict]) -> dict[str, dict]:
    """Pick the primary by SAFS relevance (not raw size), then compare the rest to it."""
    out: dict[str, dict] = {}
    rel = lambda c: c["works_count"] * field_fit(c) if field_fit(c) > 0.5 else 0
    primary = max(cands, key=rel)
    if rel(primary) == 0:  # nothing looks like aquatic science
        for c in cands:
            out[c["id"]] = {"decision": "review", "confidence": 0.2,
                            "reason": f"no SAFS-relevant profile found; fields={c['fields']}",
                            "method": "heuristic"}
        return out

    ptopics = set(primary["topic_ids"])
    pmid = middle_initial(primary["display_name"], fac["name"])
    for c in cands:
        fit = field_fit(c)
        if c is primary:
            if c["works_count"] >= 15 and fit >= 0.5:
                dec, conf = "include", 0.8
            else:
                dec, conf = "review", 0.5
            out[c["id"]] = {"decision": dec, "confidence": conf,
                            "reason": f"most SAFS-relevant profile (works={c['works_count']}, field fit={fit:.0%})",
                            "method": "heuristic"}
            continue
        overlap = len(set(c["topic_ids"]) & ptopics) / max(len(c["topic_ids"]), 1)
        cmid = middle_initial(c["display_name"], fac["name"])
        if pmid and cmid and pmid != cmid:
            dec, conf, why = "exclude", 0.8, f"middle initial {cmid.upper()} ≠ primary's {pmid.upper()}"
        elif fit < 0.5 and overlap < 0.3:
            dec, conf, why = "exclude", 0.7, f"off-field ({fit:.0%} fit), topic overlap {overlap:.0%}"
        elif overlap >= 0.6 and fit >= 2 / 3:
            dec, conf, why = "include", 0.6, f"topic overlap with primary {overlap:.0%}"
        else:
            dec, conf, why = "review", 0.3, f"topic overlap {overlap:.0%}, field fit {fit:.0%}"
        out[c["id"]] = {"decision": dec, "confidence": conf,
                        "reason": f"{why}; fields={c['fields']}", "method": "heuristic"}
    return out


def have_claude_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` profile, which the SDK picks up automatically
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="use heuristic only")
    ap.add_argument("--only", nargs="*", help="faculty slugs to (re)resolve")
    args = ap.parse_args()

    roster = json.loads((DATA / "roster.json").read_text())
    existing = yaml.safe_load(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    existing = existing or {}
    evidence = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}

    use_llm = not args.no_llm and have_claude_credentials()
    print(f"Decider: {'Claude (' + CLAUDE_MODEL + ')' if use_llm else 'heuristic'}")

    result: dict[str, dict] = {}
    for fac in roster:
        slug = fac["slug"]
        prev = existing.get(slug)
        if prev and (prev.get("verified") or (args.only and slug not in args.only)):
            result[slug] = prev
            continue

        found = search_candidates(fac["name"])
        if not found:  # e.g. recent hires whose UW affiliation isn't indexed yet
            found = search_candidates(fac["name"], uw_only=False)
            found = dict(sorted(found.items(), key=lambda kv: -kv[1].get("works_count", 0))[:10])
        cands = [summarize(a) for a in found.values()]
        cands.sort(key=lambda c: -c["works_count"])
        evidence[slug] = cands

        if not cands:
            result[slug] = {"name": fac["name"], "orcid": None, "verified": False,
                            "status": "no_candidates", "openalex": []}
            print(f"  {slug:28s} NO CANDIDATES")
            continue

        if use_llm:
            import anthropic
            try:
                decisions = decide_with_claude(fac, cands)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                # Stop before writing anything so existing overrides are untouched.
                raise SystemExit(
                    f"\nAnthropic API rejected the credentials ({e.status_code}). "
                    "Check ANTHROPIC_API_KEY, or run with --no-llm.\n"
                    f"{OVERRIDES.name} was not modified.")
        else:
            decisions = decide_heuristic(fac, cands)

        # keep any human-set candidate decisions from a previous run
        human = {e["id"]: e for e in (prev or {}).get("openalex", []) if e.get("method") == "human"}

        entries = []
        for c in cands:
            d = human.get(c["id"]) or {**decisions[c["id"]], "id": c["id"]}
            entries.append({
                "id": c["id"],
                "display_name": c["display_name"],
                "works_count": c["works_count"],
                "uw_years": c["uw_years"],
                "decision": d["decision"],
                "confidence": d["confidence"],
                "method": d["method"],
                "reason": d["reason"],
            })

        included = [e for e in entries if e["decision"] == "include"]
        orcid = next((c["orcid"] for c in cands if c["orcid"] and
                      any(e["id"] == c["id"] for e in included)), None) or fac.get("orcid")
        needs_review = any(e["decision"] == "review" for e in entries) or not included
        result[slug] = {
            "name": fac["name"],
            "orcid": orcid,
            "verified": False,
            "status": "needs_review" if needs_review else "auto",
            "resolved_on": date.today().isoformat(),
            "openalex": entries,
        }
        tally = {k: sum(e["decision"] == k for e in entries) for k in ("include", "review", "exclude")}
        print(f"  {slug:28s} {result[slug]['status']:13s} "
              f"incl={tally['include']} review={tally['review']} excl={tally['exclude']} "
              f"works={sum(e['works_count'] for e in included)}")

    EVIDENCE.write_text(json.dumps(evidence, indent=1, ensure_ascii=False) + "\n")
    header = (
        "# SAFS faculty -> OpenAlex author IDs.\n"
        "# Generated by pipeline/02_resolve_authors.py, then reviewed by humans.\n"
        "# To correct an entry: set a candidate's `decision` (include/exclude) and\n"
        "# `method: human`, or set `verified: true` to freeze the whole faculty entry.\n"
        "# Missing profiles can be added by hand as a new candidate with method: human.\n\n"
    )
    OVERRIDES.write_text(header + yaml.safe_dump(result, sort_keys=False, allow_unicode=True, width=100))
    n_review = sum(v.get("status") != "auto" for v in result.values())
    print(f"Wrote {OVERRIDES.relative_to(CONFIG.parent)}  ({len(result)} faculty, {n_review} need review)")


if __name__ == "__main__":
    main()
