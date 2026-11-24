"""Weekly research blog: Claude summarizes two faculty papers per week.

Each week two faculty are picked at random from those not yet featured in the current
cycle, and the most recent paper of each is summarized for a general audience. Once every
faculty member with papers has been featured, a new cycle starts. A paper is never
featured twice: in later cycles the next most recent unfeatured paper is used.

Reads  site/data/works.json, site/data/meta.json, data/roster.json (profile blurbs)
Writes site/data/blog.json   (newest post first; it is also the rotation state)

    uv run python pipeline/12_blog.py              # add this week's post (skips if it exists)
    uv run python pipeline/12_blog.py --dry-run    # show the picks without calling Claude
    uv run python pipeline/12_blog.py --force      # replace this week's post
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys

from pydantic import BaseModel, Field

from common import DATA, OPENALEX, ROOT, cached_get

SITE_DATA = ROOT / "site" / "data"
OUT = SITE_DATA / "blog.json"
CLAUDE_MODEL = "claude-opus-5-5"
PER_WEEK = 2
# Paper types worth a blog post (no datasets, peer reviews, errata, abstracts...).
POSTABLE = {"article", "review", "preprint", "book-chapter", "book", "data-paper"}

SYSTEM = (
    "You write short posts for the research blog of the University of Washington School of "
    "Aquatic and Fishery Sciences (SAFS). Each post explains one recent paper by a SAFS faculty "
    "member to a curious general reader: students, alumni, managers, the public.\n"
    "- Use only what the title, abstract and metadata support. Do not invent numbers, methods, "
    "places or species. If there is no abstract, keep the post brief and say less, not more.\n"
    "- Plain language; define any unavoidable term in a few words. No hype, no exclamation marks.\n"
    "- Say what question the work asked, what was done, what was found, and why it matters.\n"
    "- Refer to the faculty member by full name on first mention, then by surname. They may be "
    "one of many co-authors: describe it as work they contributed to, not their solo work, "
    "unless they are clearly the lead."
)


class Post(BaseModel):
    headline: str = Field(description="Engaging but accurate headline, at most 12 words, not the paper title")
    dek: str = Field(description="One-sentence teaser under the headline, at most 30 words")
    paragraphs: list[str] = Field(description="2 to 4 short paragraphs of the post body, plain text")
    why_it_matters: str = Field(description="One or two sentences on why this matters beyond the field")


# --------------------------------------------------------------------------- data
def load_works() -> list[dict]:
    w = json.loads((SITE_DATA / "works.json").read_text())
    return [dict(zip(w["cols"], r)) for r in w["rows"]]


def openalex_work(work_id: str) -> dict:
    return cached_get(f"{OPENALEX}/works/{work_id}")


def abstract_text(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos = sorted((i, word) for word, idx in inv.items() for i in idx)
    return " ".join(word for _, word in pos)


def latest_unfeatured(slug: str, works: list[dict], featured_ids: set[str]) -> dict | None:
    """Most recent postable paper by this faculty member that was never featured.
    works.json only has years, so publication dates come from OpenAlex for the top year."""
    mine = [w for w in works if slug in w["faculty"] and w["type"] in POSTABLE
            and w["id"] not in featured_ids and w["year"]]
    if not mine:
        return None
    # Prefer peer-reviewed work over preprints when both exist in the latest year.
    top_year = max(w["year"] for w in mine)
    pool = [w for w in mine if w["year"] == top_year]
    best, best_key = None, None
    for w in pool:
        oa = openalex_work(w["id"])
        key = (oa.get("publication_date") or "", w["type"] != "preprint",
               bool(oa.get("abstract_inverted_index")), w["cited_by"] or 0)
        if best_key is None or key > best_key:
            best, best_key = {**w, "_oa": oa}, key
    return best


# --------------------------------------------------------------------------- rotation
def pick_faculty(posts: list[dict], eligible: list[str], rng: random.Random) -> tuple[list[tuple[str, int]], int]:
    """Return [(slug, cycle)] for this week's picks. A cycle ends once everyone has been featured."""
    cycle = max((p["cycle"] for p in posts), default=1)
    done = {p["faculty"] for p in posts if p["cycle"] == cycle}
    remaining = [s for s in eligible if s not in done]
    picks: list[tuple[str, int]] = []
    while len(picks) < PER_WEEK:
        if not remaining:
            cycle += 1
            taken = {s for s, _ in picks}
            remaining = [s for s in eligible if s not in taken]
            if not remaining:
                break
        s = rng.choice(sorted(remaining))
        remaining.remove(s)
        picks.append((s, cycle))
    return picks, cycle


# --------------------------------------------------------------------------- Claude
def write_post(client, fac: dict, profile: dict, work: dict) -> tuple[Post, str]:
    oa = work["_oa"]
    authors = [a["author"]["display_name"] for a in oa.get("authorships", [])]
    payload = {
        "faculty_member": {"name": fac["name"], "research_areas": fac.get("areas", []),
                           "profile_excerpt": (profile.get("research_blurb") or "")[:800]},
        "paper": {
            "title": work["title"], "venue": work["venue"], "type": work["type"],
            "publication_date": oa.get("publication_date"),
            "authors": authors[:15] + ([f"... and {len(authors) - 15} more"] if len(authors) > 15 else []),
            "faculty_author_position": next((i + 1 for i, a in enumerate(authors) if a.split()[-1] in fac["name"]), None),
            "abstract": abstract_text(oa.get("abstract_inverted_index")),
            "one_line_summary": work.get("summary"),
            "species": work.get("species"), "regions": work.get("regions"),
        },
    }
    resp = client.beta.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_format=Post,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"Claude returned no post for {work['id']} ({resp.stop_reason})")
    return resp.parsed_output, resp.model


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show picks, do not call Claude or write")
    ap.add_argument("--force", action="store_true", help="replace this week's posts")
    ap.add_argument("--date", help="pretend today is YYYY-MM-DD (for backfilling)")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.UTC).date()
    iso = today.isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    week_start = dt.date.fromisocalendar(iso.year, iso.week, 1)

    blog = json.loads(OUT.read_text()) if OUT.exists() else {"posts": []}
    posts = blog["posts"]
    if any(p["week"] == week for p in posts):
        if not args.force:
            print(f"{week} already has posts; use --force to replace them.")
            return
        posts = [p for p in posts if p["week"] != week]

    meta = json.loads((SITE_DATA / "meta.json").read_text())
    facs = {f["slug"]: f for f in meta["faculty"]}
    roster = {f["slug"]: f for f in json.loads((DATA / "roster.json").read_text())}
    works = load_works()
    featured_ids = {p["work_id"] for p in posts}
    eligible = sorted(s for s in facs if any(s in w["faculty"] and w["type"] in POSTABLE for w in works))

    rng = random.Random(week)  # reruns in the same week make the same picks
    picks, _ = pick_faculty(posts, eligible, rng)
    chosen = []
    for slug, cycle in picks:
        work = latest_unfeatured(slug, works, featured_ids)
        if work is None:
            print(f"  {slug}: no unfeatured paper left, skipped")
            continue
        chosen.append((slug, cycle, work))
        print(f"  {week} cycle {cycle}: {facs[slug]['name']} -> {work['year']} {work['title'][:90]}")

    if args.dry_run or not chosen:
        return

    import anthropic
    client = anthropic.Anthropic()
    new = []
    for slug, cycle, work in chosen:
        try:
            post, model = write_post(client, facs[slug], roster.get(slug, {}), work)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            sys.exit(f"Anthropic API rejected the credentials ({e.status_code}). Check ANTHROPIC_API_KEY.")
        new.append({
            "week": week, "date": week_start.isoformat(), "cycle": cycle, "faculty": slug,
            "work_id": work["id"], "title": work["title"], "year": work["year"], "venue": work["venue"],
            "doi": work["doi"], "publication_date": work["_oa"].get("publication_date"),
            "theme": work["theme"], **post.model_dump(), "model": model,
        })
    blog["posts"] = new + posts
    blog["eligible"] = len(eligible)
    OUT.write_text(json.dumps(blog, ensure_ascii=False, indent=1) + "\n")
    print(f"Wrote {len(new)} posts to {OUT.relative_to(ROOT)} ({len(blog['posts'])} total)")


if __name__ == "__main__":
    sys.exit(main())
