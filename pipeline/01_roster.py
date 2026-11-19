"""Step 1: scrape the SAFS faculty roster from fish.uw.edu.

Output: data/roster.json
"""
from __future__ import annotations

import argparse
import json
import re

from bs4 import BeautifulSoup

from common import DATA, cached_get

LIST_URL = "https://fish.uw.edu/faculty-research/"
PROFILE_RE = re.compile(r"^https://fish\.uw\.edu/faculty/([a-z0-9-]+)/?$")


def classify_rank(titles: list[str]) -> str:
    t = " ".join(titles).lower()
    for key, label in [
        ("emerit", "emeritus"),
        ("affiliate", "affiliate"),
        ("adjunct", "adjunct"),
        ("research professor", "research"),
        ("research associate professor", "research"),
        ("research assistant professor", "research"),
        ("teaching professor", "teaching"),
        ("lecturer", "teaching"),
        ("assistant professor", "assistant"),
        ("associate professor", "associate"),
        ("professor", "professor"),
    ]:
        if key in t:
            return label
    return "other"


def parse_profile(slug: str, url: str, html: str, list_areas: list[str]) -> dict:
    s = BeautifulSoup(html, "html.parser")
    info = s.select_one(".faculty-info")
    name_el = s.select_one("h1.article__title")
    name = name_el.get_text(strip=True) if name_el else slug.replace("-", " ").title()
    titles = [li.get_text(" ", strip=True) for li in (info.select("ul.job-titles li") if info else [])]

    def contact(cls: str) -> str | None:
        a = info.select_one(f"li.{cls} a") if info else None
        return a["href"].strip() if a and a.has_attr("href") else None

    orcid = None
    for a in s.select(".faculty-info a[href], .article__content a[href]"):
        m = re.search(r"orcid\.org/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", a["href"])
        if m and (a.find_parent(class_="faculty-info") or name.split()[-1].lower() in a.get_text().lower()):
            orcid = m.group(1)
            break

    scholar = contact("google-scholar")
    scholar_id = None
    if scholar:
        m = re.search(r"user=([\w-]+)", scholar)
        scholar_id = m.group(1) if m else None

    cats = s.select_one(".article__categories")
    areas = [a.get_text(strip=True) for a in cats.select("a")] if cats else []
    if not areas and cats:
        areas = [li.get_text(strip=True) for li in cats.select("li")]

    content = s.select_one(".article__content")
    blurb = content.get_text(" ", strip=True) if content else ""
    blurb = re.sub(r"\s+", " ", blurb)

    lab = contact("website") or contact("lab-website") or contact("web")

    return {
        "slug": slug,
        "name": name,
        "profile_url": url,
        "titles": titles,
        "rank": classify_rank(titles),
        "research_areas": areas or list_areas,
        "orcid": orcid,
        "google_scholar_id": scholar_id,
        "lab_url": lab,
        "research_blurb": blurb[:3000],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download pages")
    args = ap.parse_args()

    listing = BeautifulSoup(cached_get(LIST_URL, as_json=False, refresh=args.refresh,
                                       min_interval=1.0), "html.parser")
    entries: dict[str, dict] = {}
    for li in listing.select("li.faculty-list-item"):
        a = li.select_one("h3 a[href]") or li.select_one("a[href]")
        m = PROFILE_RE.match(a["href"]) if a else None
        if not m:
            continue
        areas = [c.replace("-", " ") for c in li.get("class", []) if c != "faculty-list-item"]
        entries[m.group(1)] = {"url": a["href"], "list_areas": areas}

    roster = []
    for slug, e in sorted(entries.items()):
        html = cached_get(e["url"], as_json=False, refresh=args.refresh, min_interval=1.0)
        roster.append(parse_profile(slug, e["url"], html, e["list_areas"]))
        print(f"  {slug:28s} {roster[-1]['rank']:10s} orcid={roster[-1]['orcid']}")

    out = DATA / "roster.json"
    out.write_text(json.dumps(roster, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(roster)} faculty -> {out.relative_to(DATA.parent)}")


if __name__ == "__main__":
    main()
