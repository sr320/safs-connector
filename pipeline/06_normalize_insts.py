"""Step 6: institution normalization (M4).

1. Institution details from OpenAlex for every co-author institution and all of its
   ancestors: ROR id, type, city, coordinates, parent links.
2. Roll each institution up to a partner "organization" (e.g. NOAA Northwest Fisheries
   Science Center -> NOAA; Hopkins Marine Station -> Stanford), stopping below national
   government umbrellas and university systems so NOAA, USGS and the UC campuses stay apart.
3. Authorships that only carry free-text affiliation strings are matched to ROR with its
   affiliation matcher (confident "chosen" matches only), then mapped back to OpenAlex.
4. Human corrections in config/institution_overrides.yaml win over everything.

  uv run python pipeline/06_normalize_insts.py   # -> data/institutions.json, data/affiliation_matches.json
"""
from __future__ import annotations

import glob
import json
import re

import yaml

from common import CONFIG, DATA, OPENALEX, RAW, UW_OPENALEX_ID, cached_get

OUT_INSTS = DATA / "institutions.json"
OUT_MATCHES = DATA / "affiliation_matches.json"
OVERRIDES = CONFIG / "institution_overrides.yaml"
ROR_API = "https://api.ror.org/v2/organizations"
SELECT = "id,display_name,ror,type,country_code,geo,lineage,associated_institutions"

# Never roll up INTO these: national governments, ministries, university systems, and
# umbrellas so broad they would merge distinct partners (UN agencies, EU, CGIAR centers).
STOP_NAME = re.compile(
    r"^(government of|united states department of|u\.s\. department of|department of|ministry|ministerio|"
    r"minist[eè]re)|government$|\bsystem\b|^state of |^commonwealth of |public universities|"
    r"^united nations$|^european union$|^cgiar$",
    re.I)
STOP_TYPES = {"funder"}
JUNK_AFFIL = re.compile(r"^(none|null|n/?a)$|supplementary|not provided|independent\b", re.I)


def sid(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


def short_ror(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


# --------------------------------------------------------------------------- OpenAlex
def fetch_institutions(ids: set[str]) -> dict[str, dict]:
    """Fetch institutions (and, iteratively, their ancestors) 50 ids per request."""
    got: dict[str, dict] = {}
    todo = set(ids)
    while todo:
        batch = sorted(todo)[:50]
        todo -= set(batch)
        d = cached_get(f"{OPENALEX}/institutions",
                       {"filter": "openalex_id:" + "|".join(batch), "per-page": 50, "select": SELECT})
        for r in d["results"]:
            got[sid(r["id"])] = r
        for b in batch:
            got.setdefault(b, None)  # retired / merged ids
        for r in d["results"]:
            for x in (r.get("lineage") or []):
                if sid(x) not in got:
                    todo.add(sid(x))
            for a in r.get("associated_institutions") or []:
                if a.get("relationship") == "parent" and sid(a["id"]) not in got:
                    todo.add(sid(a["id"]))
    return {k: v for k, v in got.items() if v}


def institutions_by_ror(rors: set[str]) -> dict[str, dict]:
    out = {}
    rors = sorted(rors)
    for i in range(0, len(rors), 50):
        chunk = rors[i:i + 50]
        d = cached_get(f"{OPENALEX}/institutions",
                       {"filter": "ror:" + "|".join(chunk), "per-page": 50, "select": SELECT})
        for r in d["results"]:
            out[short_ror(r.get("ror"))] = r
    return out


# --------------------------------------------------------------------------- rollup
def parents(inst: dict) -> list[str]:
    """Direct parents, preferring ones that also appear in the lineage."""
    lineage = {sid(x) for x in inst.get("lineage") or []}
    ps = [sid(a["id"]) for a in inst.get("associated_institutions") or [] if a.get("relationship") == "parent"]
    return sorted(ps, key=lambda p: p not in lineage)


def is_stop(inst: dict | None) -> bool:
    return (inst is None or inst.get("type") in STOP_TYPES
            or bool(STOP_NAME.search(inst.get("display_name") or "")))


def rollup(iid: str, insts: dict[str, dict], forced: dict[str, str]) -> str:
    seen = {iid}
    cur = iid
    while True:
        if cur in forced:
            return forced[cur]
        ps = [p for p in parents(insts[cur]) if p in insts and p not in seen]
        if not ps or is_stop(insts[ps[0]]):
            return cur
        # a university never merges into another education body (UC / SUNY / state systems);
        # only its labs, field stations and hospitals roll up into it
        if insts[cur].get("type") == "education" and insts[ps[0]].get("type") == "education":
            return cur
        cur = ps[0]
        seen.add(cur)


# --------------------------------------------------------------------------- ROR matching
def ror_match(text: str) -> dict | None:
    d = cached_get(ROR_API, {"affiliation": text}, min_interval=0.25)
    for it in d.get("items") or []:
        if it.get("chosen"):
            org = it["organization"]
            name = next((n["value"] for n in org["names"] if "ror_display" in n["types"]), None)
            return {"ror": short_ror(org["id"]), "ror_name": name, "score": it["score"],
                    "matching_type": it["matching_type"]}
    return None


# --------------------------------------------------------------------------- main
def main() -> None:
    overrides = yaml.safe_load(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    overrides = overrides or {}
    forced_org = {k: v for k, v in (overrides.get("rollup") or {}).items()}
    forced_aff = overrides.get("affiliations") or {}

    inst_ids: set[str] = set()
    raw_only: dict[str, int] = {}
    seen = set()
    for fn in glob.glob(str(RAW / "works" / "*.jsonl")):
        for line in open(fn):
            w = json.loads(line)
            if w["id"] in seen:
                continue
            seen.add(w["id"])
            for a in w.get("authorships") or []:
                if a.get("institutions"):
                    inst_ids |= {sid(i["id"]) for i in a["institutions"] if i.get("id")}
                else:
                    for s in a.get("raw_affiliation_strings") or []:
                        s = " ".join(s.split())
                        if s and not JUNK_AFFIL.search(s):
                            raw_only[s] = raw_only.get(s, 0) + 1

    # 1. free-text affiliations -> ROR -> OpenAlex
    print(f"Matching {len(raw_only)} free-text affiliation strings against ROR ...")
    matches = {}
    for n, s in enumerate(sorted(raw_only), 1):
        if s in forced_aff:
            matches[s] = {"openalex_id": forced_aff[s], "source": "human"}
            continue
        m = ror_match(s[:500])
        if m:
            matches[s] = m
        if n % 200 == 0:
            print(f"  {n}/{len(raw_only)}  matched={len(matches)}")
    by_ror = institutions_by_ror({m["ror"] for m in matches.values() if m.get("ror")})
    for s, m in matches.items():
        if "openalex_id" not in m:
            inst = by_ror.get(m["ror"])
            m["openalex_id"] = sid(inst["id"]) if inst else None
            m["source"] = "ror-affiliation"
    matches = {s: m for s, m in matches.items() if m.get("openalex_id")}
    inst_ids |= {m["openalex_id"] for m in matches.values()}

    # 2. institution details + ancestors
    print(f"Fetching {len(inst_ids)} institutions (plus ancestors) from OpenAlex ...")
    insts = fetch_institutions(inst_ids)

    # 3. rollup + output
    out = {}
    for iid, r in insts.items():
        geo = r.get("geo") or {}
        org = rollup(iid, insts, forced_org)
        ps = parents(r)
        out[iid] = {
            "id": iid, "name": r["display_name"], "ror": short_ror(r.get("ror")),
            "type": r.get("type"), "country": r.get("country_code"),
            "city": geo.get("city"), "region": geo.get("region"),
            "lat": geo.get("latitude"), "lon": geo.get("longitude"),
            "parent_id": ps[0] if ps else None, "org_id": org,
            "is_uw": int(UW_OPENALEX_ID in {sid(x) for x in r.get("lineage") or []} or org == UW_OPENALEX_ID),
        }
    OUT_INSTS.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    OUT_MATCHES.write_text(json.dumps(matches, indent=1, ensure_ascii=False, sort_keys=True))

    used = [out[i] for i in inst_ids if i in out]
    n_orgs = len({o["org_id"] for o in used})
    print(f"Wrote {OUT_INSTS.relative_to(DATA.parent)}: {len(out)} institutions "
          f"({len(used)} used by authorships -> {n_orgs} partner organizations), "
          f"{sum(o['lat'] is not None for o in used)}/{len(used)} with coordinates")
    hit = sum(raw_only[s] for s in matches)
    print(f"Wrote {OUT_MATCHES.relative_to(DATA.parent)}: {len(matches)}/{len(raw_only)} strings matched "
          f"({hit}/{sum(raw_only.values())} authorships)")


if __name__ == "__main__":
    main()
