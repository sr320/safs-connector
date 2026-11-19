"""Step 5: tag every paper with SAFS themes, subtopics, taxa, ecosystems, regions
and methods using Claude via the Message Batches API (50% price, async).

Vocabulary: config/topic_taxonomy.yaml
Output:     data/topic_tags.jsonl   one line per work (committed; the source of truth)
State:      data/enrichment/        batch ids and request manifests (committed, small)

Subcommands
  plan     build requests, report counts and a cost estimate; calls no API
  submit   create batch(es) for untagged papers   [--limit N --sample]  [--retag]
  collect  wait for submitted batches and append their results to topic_tags.jsonl
  status   show submitted batches

Typical flow
  uv run python pipeline/05_enrich_topics.py plan
  uv run python pipeline/05_enrich_topics.py submit --limit 100 --sample   # spot-check first
  uv run python pipeline/05_enrich_topics.py collect
  uv run python pipeline/09_spotcheck.py                                   # review sample
  uv run python pipeline/05_enrich_topics.py submit                        # everything else
  uv run python pipeline/05_enrich_topics.py collect
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Literal

import yaml
from pydantic import BaseModel, Field, create_model

from common import CONFIG, DATA

MODEL = "claude-opus-5-5"
EFFORT = "low"
PAPERS_PER_REQUEST = 8
MAX_ABSTRACT_CHARS = 1800
TAGS = DATA / "topic_tags.jsonl"
STATE = DATA / "enrichment"
DB = DATA / "safs.sqlite"

# Batch pricing for claude-opus-5-5 (50% of $4 / $20 per MTok); cache reads $0.20 list.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ = 2.0, 10.0, 0.10


# --------------------------------------------------------------------------- taxonomy & schema
def load_taxonomy() -> dict:
    return yaml.safe_load((CONFIG / "topic_taxonomy.yaml").read_text())


def build_schema_model(tax: dict) -> type[BaseModel]:
    themes = tuple(t["id"] for t in tax["themes"])
    subs = tuple(s["id"] for t in tax["themes"] for s in t["subtopics"])
    Theme = Literal[themes]  # type: ignore[valid-type]
    Sub = Literal[subs]  # type: ignore[valid-type]
    Taxon = Literal[tuple(tax["taxon_groups"])]  # type: ignore[valid-type]
    Eco = Literal[tuple(tax["ecosystems"])]  # type: ignore[valid-type]
    Method = Literal[tuple(tax["methods"])]  # type: ignore[valid-type]

    PaperTags = create_model(
        "PaperTags",
        work_id=(str, ...),
        primary_theme=(Theme, ...),
        subtopics=(list[Sub], Field(description="1-4 subtopic ids, most central first")),
        taxon_groups=(list[Taxon], ...),
        species=(list[str], Field(description="Focal species, common names, max 5")),
        ecosystems=(list[Eco], ...),
        regions=(list[str], Field(description="Specific places studied, max 3, e.g. 'Puget Sound', 'Bristol Bay, Alaska', 'Global'")),
        methods=(list[Method], ...),
        summary=(str, Field(description="One plain-language sentence, max 25 words")),
    )
    return create_model("PaperBatchTags", papers=(list[PaperTags], ...))


def taxonomy_hash(tax: dict) -> str:
    return hashlib.sha1(json.dumps(tax, sort_keys=True).encode()).hexdigest()[:10]


def system_prompt(tax: dict) -> str:
    lines = [
        "You tag scientific papers by faculty of the University of Washington School of "
        "Aquatic and Fishery Sciences (SAFS) so they can be explored by topic.",
        "",
        "For EACH paper in the input, return one entry (same work_id) with:",
        "- primary_theme: the single best theme id for what the paper is ABOUT (its subject or question), "
        "not the method it uses. A stock-assessment model is fisheries-management; use "
        "quantitative-methods only when a new method, model or software is itself the main contribution.",
        "- subtopics: 1-4 subtopic ids from ANY theme, most central first. Prefer precision: "
        "only tag a subtopic the paper substantially addresses.",
        "- taxon_groups: focal organism groups ('none' if no focal organism).",
        "- species: up to 5 focal species by common name (empty if none or many).",
        "- ecosystems: where the work is set ('laboratory' for lab-only, 'global-multiple' for "
        "syntheses spanning many systems, 'none' if not applicable).",
        "- regions: up to 3 specific geographic places studied, as they'd appear on a map "
        "('Global' for global analyses, empty if not place-based).",
        "- methods: main approaches used.",
        "- summary: one plain-language sentence (max 25 words) saying what the paper found or did.",
        "Base tags on the title, abstract and keywords. When there is no abstract, tag "
        "conservatively from the title and venue. Datasets, software and errata still get "
        "tags describing their subject.",
        "",
        "THEMES AND SUBTOPICS (id: name - description)",
    ]
    for t in tax["themes"]:
        lines.append(f"[{t['id']}] {t['name']}")
        for s in t["subtopics"]:
            lines.append(f"  - {s['id']}: {s['name']} - {s['description']}")
    lines += ["", "TAXON GROUPS: " + ", ".join(tax["taxon_groups"]),
              "ECOSYSTEMS: " + ", ".join(tax["ecosystems"]),
              "METHODS: " + ", ".join(tax["methods"])]
    return "\n".join(lines)


# --------------------------------------------------------------------------- papers
def papers_to_tag(retag: bool, tax_hash: str) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT w.id, w.title, w.year, w.type, w.venue, w.abstract, w.keywords, w.primary_topic
        FROM works w
        WHERE EXISTS (SELECT 1 FROM faculty_works fw WHERE fw.work_id = w.id AND fw.status != 'drop')
          AND w.title IS NOT NULL
          AND COALESCE(w.type, '') != 'peer-review'  -- reviewer reports / author responses, not papers
        ORDER BY w.id""").fetchall()
    done = set()
    if TAGS.exists() and not retag:
        for line in TAGS.open():
            r = json.loads(line)
            if r.get("taxonomy_hash") == tax_hash:
                done.add(r["work_id"])
    return [dict(r) for r in rows if r["id"] not in done]


def paper_payload(p: dict) -> dict:
    kw = json.loads(p["keywords"] or "[]")[:6]
    ab = p["abstract"]
    if ab and len(ab) > MAX_ABSTRACT_CHARS:
        ab = ab[:MAX_ABSTRACT_CHARS] + " …"
    return {"work_id": p["id"], "title": p["title"], "year": p["year"], "type": p["type"],
            "venue": p["venue"], "keywords": kw, "abstract": ab}


def build_requests(papers: list[dict], tax: dict) -> list[dict]:
    from anthropic import transform_schema

    schema = transform_schema(build_schema_model(tax))
    system = [{"type": "text", "text": system_prompt(tax), "cache_control": {"type": "ephemeral"}}]
    reqs = []
    for i in range(0, len(papers), PAPERS_PER_REQUEST):
        chunk = papers[i:i + PAPERS_PER_REQUEST]
        ids = [p["id"] for p in chunk]
        cid = "t-" + hashlib.sha1("|".join(ids).encode()).hexdigest()[:20]
        reqs.append({
            "custom_id": cid,
            "work_ids": ids,
            "params": {
                "model": MODEL,
                "max_tokens": 8000,
                "system": system,
                "output_config": {"effort": EFFORT,
                                  "format": {"type": "json_schema", "schema": schema}},
                "messages": [{"role": "user", "content": json.dumps(
                    {"papers": [paper_payload(p) for p in chunk]}, ensure_ascii=False)}],
            },
        })
    return reqs


def estimate(reqs: list[dict]) -> dict:
    # ~4 chars/token is a fair English approximation; good enough for a budget check.
    sys_tok = len(reqs[0]["params"]["system"][0]["text"]) / 4 if reqs else 0
    schema_tok = len(json.dumps(reqs[0]["params"]["output_config"]["format"])) / 4 if reqs else 0
    user_tok = sum(len(r["params"]["messages"][0]["content"]) for r in reqs) / 4
    n_papers = sum(len(r["work_ids"]) for r in reqs)
    out_tok = n_papers * 140
    fresh_in = user_tok + (sys_tok + schema_tok)  # first request writes the cache
    cached_in = (sys_tok + schema_tok) * max(len(reqs) - 1, 0)
    cost_cached = (fresh_in * PRICE_IN + cached_in * PRICE_CACHE_READ + out_tok * PRICE_OUT) / 1e6
    cost_nocache = ((user_tok + (sys_tok + schema_tok) * len(reqs)) * PRICE_IN + out_tok * PRICE_OUT) / 1e6
    return {"requests": len(reqs), "papers": n_papers, "system_tokens": int(sys_tok),
            "input_tokens": int(user_tok + (sys_tok + schema_tok) * len(reqs)),
            "output_tokens_est": out_tok, "cost_usd_if_cached": round(cost_cached, 2),
            "cost_usd_no_cache": round(cost_nocache, 2)}


# --------------------------------------------------------------------------- API actions
def client():
    import anthropic
    return anthropic.Anthropic()


def cmd_plan(args, tax):
    papers = papers_to_tag(args.retag, taxonomy_hash(tax))
    reqs = build_requests(papers, tax)
    est = estimate(reqs)
    for k, v in est.items():
        print(f"  {k:20s} {v}")
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "plan_example_request.json").write_text(json.dumps(reqs[0] if reqs else {}, indent=1, ensure_ascii=False))
    print(f"Example request -> {(STATE / 'plan_example_request.json').relative_to(DATA.parent)}")


def cmd_submit(args, tax):
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    tax_hash = taxonomy_hash(tax)
    papers = papers_to_tag(args.retag, tax_hash)
    pending = {w for b in load_batches() if b["status"] != "collected" for w in b["work_ids"]}
    papers = [p for p in papers if p["id"] not in pending]
    if args.sample:
        random.Random(42).shuffle(papers)
    if args.limit:
        papers = papers[:args.limit]
    if not papers:
        print("Nothing to tag.")
        return
    reqs = build_requests(papers, tax)
    est = estimate(reqs)
    print(f"Submitting {est['papers']} papers in {est['requests']} requests "
          f"(est. ${est['cost_usd_if_cached']}–{est['cost_usd_no_cache']})")
    try:
        batch = client().messages.batches.create(requests=[
            Request(custom_id=r["custom_id"], params=MessageCreateParamsNonStreaming(**r["params"]))
            for r in reqs])
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        sys.exit(f"Anthropic API rejected the credentials ({e.status_code}). Check ANTHROPIC_API_KEY.")
    rec = {"batch_id": batch.id, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "status": "submitted", "taxonomy_hash": tax_hash, "model": MODEL,
           "requests": {r["custom_id"]: r["work_ids"] for r in reqs},
           "work_ids": [w for r in reqs for w in r["work_ids"]]}
    save_batch(rec)
    print(f"Batch {batch.id} submitted. Run `collect` to wait for results.")


def load_batches() -> list[dict]:
    path = STATE / "batches.json"
    return json.loads(path.read_text()) if path.exists() else []


def save_batch(rec: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    batches = [b for b in load_batches() if b["batch_id"] != rec["batch_id"]] + [rec]
    (STATE / "batches.json").write_text(json.dumps(batches, indent=1) + "\n")


def cmd_status(args, tax):
    for b in load_batches():
        print(f"  {b['batch_id']}  {b['status']:10s} {len(b['work_ids']):5d} papers  {b['created']}")


def cmd_collect(args, tax):
    tax_obj = tax
    valid = {
        "theme": {t["id"] for t in tax_obj["themes"]},
        "sub": {s["id"] for t in tax_obj["themes"] for s in t["subtopics"]},
    }
    c = client()
    for b in load_batches():
        if b["status"] == "collected":
            continue
        while True:
            info = c.messages.batches.retrieve(b["batch_id"])
            if info.processing_status == "ended":
                break
            rc = info.request_counts
            print(f"  {b['batch_id']}: processing={rc.processing} succeeded={rc.succeeded}; waiting 60s")
            time.sleep(60)

        n_ok = n_bad = 0
        usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        retry: list[str] = []
        with TAGS.open("a") as out:
            for res in c.messages.batches.results(b["batch_id"]):
                ids = b["requests"].get(res.custom_id, [])
                if res.result.type != "succeeded":
                    retry += ids
                    continue
                msg = res.result.message
                u = msg.usage
                usage["input"] += u.input_tokens
                usage["cache_read"] += u.cache_read_input_tokens or 0
                usage["cache_write"] += u.cache_creation_input_tokens or 0
                usage["output"] += u.output_tokens
                if msg.stop_reason in ("refusal", "max_tokens"):
                    retry += ids
                    continue
                text = next((blk.text for blk in msg.content if blk.type == "text"), "")
                try:
                    papers = json.loads(text)["papers"]
                except (json.JSONDecodeError, KeyError):
                    retry += ids
                    continue
                got = set()
                for p in papers:
                    if p.get("work_id") not in ids or p["primary_theme"] not in valid["theme"]:
                        continue
                    got.add(p["work_id"])
                    p["subtopics"] = [s for s in p["subtopics"] if s in valid["sub"]][:4]
                    p["species"] = p.get("species", [])[:5]
                    p["regions"] = p.get("regions", [])[:3]
                    p.update({"model": msg.model, "taxonomy_hash": b["taxonomy_hash"],
                              "batch_id": b["batch_id"]})
                    out.write(json.dumps(p, ensure_ascii=False) + "\n")
                    n_ok += 1
                missing = [w for w in ids if w not in got]
                n_bad += len(missing)
                retry += missing
        b["status"] = "collected"
        b["usage"] = usage
        b["retry"] = retry
        save_batch(b)
        print(f"  {b['batch_id']}: tagged {n_ok}, to retry {len(retry)}  usage={usage}")
    dedupe_tags()


def dedupe_tags() -> None:
    """Keep the latest line per work_id."""
    if not TAGS.exists():
        return
    latest = {}
    for line in TAGS.open():
        r = json.loads(line)
        latest[r["work_id"]] = r
    with TAGS.open("w") as fh:
        for wid in sorted(latest):
            fh.write(json.dumps(latest[wid], ensure_ascii=False) + "\n")
    print(f"topic_tags.jsonl: {len(latest)} tagged works")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "submit", "collect", "status"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sample", action="store_true", help="random sample (seeded) instead of first N")
    ap.add_argument("--retag", action="store_true", help="ignore existing tags")
    args = ap.parse_args()
    tax = load_taxonomy()
    {"plan": cmd_plan, "submit": cmd_submit, "collect": cmd_collect, "status": cmd_status}[args.cmd](args, tax)


if __name__ == "__main__":
    main()
