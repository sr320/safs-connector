"""Step 4: flag papers wrongly attributed to a faculty member.

Even a correct OpenAlex profile can contain another person's papers (e.g. 1960s
urology or 1940s metallurgy by a namesake). For each faculty member:

  1. Anchor set = works whose byline for this faculty member lists UW.
     Their topics define the person's research profile; their earliest year
     approximates the start of the UW-era record.
  2. Each work gets heuristic reasons to doubt it:
       off_topic        none of its topics appear in the anchor profile and its
                        subfield is rare (<2%) in the anchor set
       pre_career       > 12 years before the first anchor work and weak topic fit
       implausibly_early > 25 years before the first anchor work
       paratext         erratum / paratext / retracted
  3. Suspects (any reason except paratext) go to Claude for a keep/drop call when
     credentials are available; otherwise they stay `suspect`.
  4. Works listed in the faculty member's ORCID record (data/orcid.json, from
     03b_orcid.py) are kept without review, except errata/paratext.
  5. config/work_overrides.yaml (human) beats everything.

Writes data/attribution.json: {slug: {work_id: {status, reasons, method, note}}}
  status: keep | suspect | drop     (works not listed are `keep`)

Usage:
  uv run python pipeline/04_check_attribution.py            # Claude if creds
  uv run python pipeline/04_check_attribution.py --no-llm
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from common import CONFIG, DATA, RAW, UW_OPENALEX_ID

CLAUDE_MODEL = "claude-opus-5-5"
OUT = DATA / "attribution.json"
WORK_OVERRIDES = CONFIG / "work_overrides.yaml"
UW = f"https://openalex.org/{UW_OPENALEX_ID}"


def sid(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


def faculty_authorship(work: dict, author_urls: set[str]) -> dict:
    for a in work.get("authorships") or []:
        if a["author"].get("id") in author_urls:
            return a
    return {}


def topic_ids(work: dict) -> list[str]:
    return [sid(t["id"]) for t in (work.get("topics") or [])[:3]]


def subfield(work: dict) -> str | None:
    pt = work.get("primary_topic") or {}
    return (pt.get("subfield") or {}).get("display_name")


def assess(works: list[dict], author_ids: list[str]) -> tuple[dict, dict]:
    urls = {f"https://openalex.org/{a}" for a in author_ids}
    anchors = [w for w in works
               if any(i["id"] == UW for i in faculty_authorship(w, urls).get("institutions", []))]
    if len(anchors) < 5:  # sparse affiliation data: fall back to recent works
        anchors = sorted(works, key=lambda w: -(w.get("publication_year") or 0))[:max(5, len(works) // 3)]

    anchor_topics = collections.Counter(t for w in anchors for t in topic_ids(w))
    anchor_sub = collections.Counter(subfield(w) for w in anchors)
    n_anchor = max(len(anchors), 1)
    anchor_ids = {w["id"] for w in anchors}
    first_anchor = min((w["publication_year"] for w in anchors if w.get("publication_year")), default=None)

    profile = {
        "n_anchor": len(anchors),
        "first_anchor_year": first_anchor,
        "top_subfields": [s for s, _ in anchor_sub.most_common(8)],
    }
    flags: dict[str, dict] = {}
    for w in works:
        reasons = []
        year = w.get("publication_year")
        tids = topic_ids(w)
        fit = sum(t in anchor_topics for t in tids) / len(tids) if tids else 0.0
        share = anchor_sub.get(subfield(w), 0) / n_anchor
        if w.get("is_paratext") or w.get("is_retracted") or w.get("type") in ("erratum", "retraction"):
            reasons.append("paratext")
        if w["id"] not in anchor_ids:
            if fit == 0 and share < 0.02:
                reasons.append("off_topic")
            if first_anchor and year:
                if year < first_anchor - 25:
                    reasons.append("implausibly_early")
                elif year < first_anchor - 12 and fit < 0.67:
                    reasons.append("pre_career")
        if reasons:
            flags[sid(w["id"])] = {
                "status": "drop" if reasons == ["paratext"] else "suspect",
                "reasons": reasons, "method": "heuristic",
                "topic_fit": round(fit, 2), "subfield_share": round(share, 3),
            }
    return flags, {**profile, "anchor_ids": [sid(w["id"]) for w in anchors]}


# --------------------------------------------------------------------------- Claude
class WorkDecision(BaseModel):
    work_id: str
    keep: Literal["yes", "no", "unsure"]
    reason: str = Field(description="One short sentence")


class WorkDecisions(BaseModel):
    decisions: list[WorkDecision]


WorkDecision.model_rebuild(_types_namespace={"Literal": Literal})
WorkDecisions.model_rebuild(_types_namespace={"WorkDecision": WorkDecision})

SYSTEM = """You audit a publication list for one University of Washington School of \
Aquatic and Fishery Sciences (SAFS) faculty member. The list comes from OpenAlex, \
which sometimes attaches papers by a different person with the same name.

You get the faculty member's SAFS profile, a sample of papers that are certainly \
theirs (their byline lists UW), and a list of SUSPECT papers. For each suspect decide:
- "yes": plausibly theirs (fits their research arc, including earlier training such as \
PhD/postdoc work in a related field, or a plausible side collaboration).
- "no": clearly a different person (unrelated discipline, impossible timing, \
inconsistent affiliation history).
- "unsure": genuinely ambiguous.
Researchers' early careers can differ from their current focus; weigh timing, \
co-author institutions and subject together rather than topic alone."""


def describe(w: dict, urls: set[str]) -> dict:
    fa = faculty_authorship(w, urls)
    src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    return {
        "work_id": sid(w["id"]),
        "year": w.get("publication_year"),
        "type": w.get("type"),
        "title": (w.get("title") or "")[:200],
        "venue": src,
        "byline_name": fa.get("raw_author_name"),
        "byline_institutions": [i["display_name"] for i in fa.get("institutions", [])][:3],
        "subfield": subfield(w),
    }


def claude_review(fac: dict, works_by_id: dict, anchor_ids: list[str],
                  suspects: list[str], author_ids: list[str]) -> dict[str, dict]:
    import anthropic

    client = anthropic.Anthropic()
    urls = {f"https://openalex.org/{a}" for a in author_ids}
    sample = sorted((works_by_id[a] for a in anchor_ids),
                    key=lambda w: -(w.get("cited_by_count") or 0))[:25]
    out: dict[str, dict] = {}
    for i in range(0, len(suspects), 60):
        chunk = suspects[i:i + 60]
        payload = {
            "faculty": {"name": fac["name"], "titles": fac["titles"],
                        "research_areas": fac["research_areas"],
                        "profile_excerpt": fac["research_blurb"][:1200]},
            "certain_papers": [{"year": w.get("publication_year"), "title": (w.get("title") or "")[:160]}
                               for w in sample],
            "suspect_papers": [describe(works_by_id[s], urls) for s in chunk],
        }
        resp = client.beta.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_format=WorkDecisions,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            print(f"    ! no decision for chunk ({resp.stop_reason}); left as suspect")
            continue
        for d in resp.parsed_output.decisions:
            if d.work_id in chunk:
                out[d.work_id] = {"status": {"yes": "keep", "no": "drop", "unsure": "suspect"}[d.keep],
                                  "method": f"claude:{resp.model}", "note": d.reason}
    return out


def have_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.path.isdir(os.path.expanduser("~/.config/anthropic")))


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    roster = {f["slug"]: f for f in json.loads((DATA / "roster.json").read_text())}
    index = json.loads((DATA / "works_index.json").read_text())
    human = (yaml.safe_load(WORK_OVERRIDES.read_text()) if WORK_OVERRIDES.exists() else None) or {}
    previous = json.loads(OUT.read_text()) if OUT.exists() else {}
    orcid_path = DATA / "orcid.json"
    orcid = json.loads(orcid_path.read_text()) if orcid_path.exists() else {}
    use_llm = not args.no_llm and have_claude_credentials()
    print(f"Decider for suspects: {'Claude (' + CLAUDE_MODEL + ')' if use_llm else 'none (heuristic flags only)'}")

    result: dict[str, dict] = {}
    for slug, meta in index.items():
        if args.only and slug not in args.only:
            result[slug] = previous.get(slug, {})
            continue
        path = RAW / "works" / f"{slug}.jsonl"
        if not meta["author_ids"] or not path.exists():
            result[slug] = {"profile": None, "works": {}}
            continue
        works = [json.loads(line) for line in path.open()]
        by_id = {sid(w["id"]): w for w in works}
        flags, profile = assess(works, meta["author_ids"])
        # The person lists these in their own ORCID record: keep without asking Claude.
        o = orcid.get(slug) or {}
        for wid in set(o.get("confirmed") or []) | set(o.get("added") or {}):
            if wid in flags and "paratext" not in flags[wid]["reasons"]:
                flags[wid].update({"status": "keep", "method": "orcid",
                                   "note": "Listed in the faculty member's ORCID record."})

        # Reuse earlier Claude decisions so reruns only send new suspects (stable + cheap).
        prev_flags = (previous.get(slug) or {}).get("works", {})
        for k, v in flags.items():
            pv = prev_flags.get(k) or {}
            if v["status"] == "suspect" and str(pv.get("method", "")).startswith("claude") \
                    and pv.get("status") in ("keep", "drop", "suspect"):
                v.update({key: pv[key] for key in ("status", "method", "note", "confidence") if key in pv})
                v["reviewed"] = True
        suspects = [k for k, v in flags.items() if v["status"] == "suspect" and not v.get("reviewed")]
        for v in flags.values():
            v.pop("reviewed", None)
        if use_llm and suspects:
            import anthropic
            try:
                for k, v in claude_review(roster[slug], by_id, profile["anchor_ids"],
                                          suspects, meta["author_ids"]).items():
                    flags[k].update(v)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise SystemExit(f"Anthropic API rejected the credentials ({e.status_code}). "
                                 "Check ANTHROPIC_API_KEY, or run with --no-llm.")

        for wid, status in (human.get(slug) or {}).items():
            flags.setdefault(wid, {"reasons": []}).update({"status": status, "method": "human"})

        tally = collections.Counter(v["status"] for v in flags.values())
        profile.pop("anchor_ids")
        result[slug] = {"profile": profile, "works": flags}
        print(f"  {slug:28s} works={len(works):4d} anchors={profile['n_anchor']:4d} "
              f"first_anchor={profile['first_anchor_year']}  "
              f"suspect={tally['suspect']:3d} drop={tally['drop']:3d} kept-after-review={tally['keep']:3d}")

    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    tot = collections.Counter(v["status"] for r in result.values() for v in r.get("works", {}).values())
    print(f"Wrote {OUT.relative_to(DATA.parent)}: {dict(tot)}")


if __name__ == "__main__":
    main()
