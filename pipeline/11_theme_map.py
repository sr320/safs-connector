"""Step 11: 2-D "theme map" of all kept papers for the explorer (M6).

Each paper's title, Claude summary, subtopics and the start of its abstract are embedded
with a small local open model (BAAI/bge-small-en-v1.5 via fastembed; the Claude API has no
embeddings endpoint), then projected to 2-D with UMAP. Papers on similar subjects land
near each other, so the map shows where faculty overlap and which papers bridge areas.

  uv run python pipeline/11_theme_map.py   # -> site/data/theme_map.json

Embeddings are cached in data/raw/embeddings.npz (gitignored), so reruns only embed new
or changed papers. The layout uses a fixed random seed, but adding papers still shifts it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import numpy as np

from common import DATA, RAW, ROOT

DB = DATA / "safs.sqlite"
CACHE = RAW / "embeddings.npz"
OUT = ROOT / "site" / "data" / "theme_map.json"
MODEL = "BAAI/bge-small-en-v1.5"


def paper_texts() -> list[tuple[str, str]]:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT w.id, w.title, s.summary, w.abstract,
               (SELECT group_concat(t.name, '; ') FROM work_themes wt JOIN themes t ON t.id = wt.theme_id
                WHERE wt.work_id = w.id AND wt.role = 'subtopic') AS subs
        FROM works w LEFT JOIN work_summaries s ON s.work_id = w.id
        WHERE EXISTS (SELECT 1 FROM faculty_works fw WHERE fw.work_id = w.id AND fw.status = 'keep')
        ORDER BY w.id""").fetchall()
    out = []
    for wid, title, summary, abstract, subs in rows:
        parts = [title or "", summary or "", f"Topics: {subs}" if subs else "", (abstract or "")[:600]]
        out.append((wid, "\n".join(p for p in parts if p)))
    return out


def main() -> None:
    from fastembed import TextEmbedding
    import umap

    items = paper_texts()
    keys = [hashlib.sha1(t.encode()).hexdigest() for _, t in items]
    cache = dict(np.load(CACHE)) if CACHE.exists() else {}
    todo = [(k, t) for k, (_, t) in zip(keys, items) if k not in cache]
    if todo:
        print(f"Embedding {len(todo)} papers with {MODEL} ...")
        model = TextEmbedding(MODEL)
        for (k, _), v in zip(todo, model.embed([t for _, t in todo], batch_size=64)):
            cache[k] = np.asarray(v, dtype=np.float32)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(CACHE, **{k: cache[k] for k in keys})
    X = np.stack([cache[k] for k in keys])

    print(f"UMAP on {len(X)} papers ...")
    xy = umap.UMAP(n_neighbors=20, min_dist=0.15, metric="cosine", random_state=42).fit_transform(X)
    # scale to 0..1 on the 0.5-99.5 percentile range so a few outliers don't squeeze the map
    lo, hi = np.percentile(xy, 0.5, axis=0), np.percentile(xy, 99.5, axis=0)
    xy = np.clip((xy - lo) / (hi - lo), 0, 1)
    rows = [[wid, round(float(x), 4), round(float(y), 4)] for (wid, _), (x, y) in zip(items, xy)]
    OUT.write_text(json.dumps({"cols": ["id", "x", "y"], "rows": rows}, separators=(",", ":")))
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(rows)} papers, {OUT.stat().st_size / 1e3:.0f} kB)")


if __name__ == "__main__":
    main()
