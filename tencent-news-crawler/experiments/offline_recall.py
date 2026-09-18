"""离线语义召回实验：本地 BGE 向量化 + hnswlib 建索引 + recall@k 评测。

与线上 FastGPT(text-embedding-v4/1536d)完全解耦，全程本地：
    1. extract : data/articles/*.json -> 切片 data/experiments/offline_recall/chunks.jsonl
    2. embed   : sentence-transformers(BGE) -> embeddings.npy
    3. index   : hnswlib(m=32, ef_construction=200, 内积/余弦) -> index.bin
    4. eval    : tests/retrieval_questions.json -> recall@k / MRR
    5. hot     : 按发布时间切近 N 天热层 -> hnswlib(fp16 存档) + manifest
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

CRAWLER_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ARTICLES = CRAWLER_DIR / "data" / "articles"
DEFAULT_QUESTIONS = CRAWLER_DIR / "tests" / "retrieval_questions.json"
DEFAULT_MODEL = CRAWLER_DIR / "data" / "models" / "bge-large-zh-v1.5"
EXP_DIR = CRAWLER_DIR / "data" / "experiments" / "offline_recall"

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def news_id_of(url: str) -> str:
    return re.sub(r"[?#].*$", "", url or "").rstrip("/").split("/")[-1]


def iter_articles(articles_dir: Path):
    for path in sorted(articles_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        content = (data.get("content") or "").strip()
        if not content:
            continue
        url = data.get("url") or ""
        yield {
            "news_id": data.get("news_id") or news_id_of(url),
            "url": url,
            "title": (data.get("title") or "").strip(),
            "content": content,
            "publish_time": data.get("publish_time") or "",
        }


def split_chunks(text: str, chunk_size: int, overlap: int):
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start:start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(text):
            break
        start += chunk_size - overlap
    return chunks


def cmd_extract(args) -> None:
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    out = EXP_DIR / "chunks.jsonl"
    n_articles = n_chunks = 0
    with out.open("w", encoding="utf-8") as fh:
        for art in iter_articles(Path(args.articles)):
            body = f"{art['title']}\n{art['content']}" if art["title"] else art["content"]
            chunks = split_chunks(body, args.chunk_size, args.overlap)
            n_articles += 1
            for i, text in enumerate(chunks):
                fh.write(json.dumps({
                    "news_id": art["news_id"],
                    "url": art["url"],
                    "publish_time": art["publish_time"],
                    "chunk_index": i,
                    "text": text,
                }, ensure_ascii=False) + "\n")
                n_chunks += 1
    print(f"extract: articles={n_articles} chunks={n_chunks} -> {out}")


def load_chunks():
    with (EXP_DIR / "chunks.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def cmd_embed(args) -> None:
    import numpy as np
    from sentence_transformers import SentenceTransformer

    chunks = load_chunks()
    texts = [c["text"] for c in chunks]
    print(f"embed: {len(texts)} chunks, model={args.model}, device={args.device}")
    model = SentenceTransformer(args.model, device=args.device)
    model.max_seq_length = args.max_seq_length
    t0 = time.time()
    vecs = model.encode(
        texts,
        batch_size=args.batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype("float32")
    np.save(EXP_DIR / "embeddings.npy", vecs)
    print(f"embed: shape={vecs.shape} elapsed={time.time()-t0:.1f}s "
          f"-> {EXP_DIR / 'embeddings.npy'}")


def cmd_index(args) -> None:
    import hnswlib
    import numpy as np

    vecs = np.load(EXP_DIR / "embeddings.npy")
    dim = vecs.shape[1]
    index = hnswlib.Index(space="ip", dim=dim)
    index.init_index(max_elements=vecs.shape[0], ef_construction=args.ef_construction, M=args.m)
    index.add_items(vecs, np.arange(vecs.shape[0]))
    index.set_ef(args.ef)
    index.save_index(str(EXP_DIR / "index.bin"))
    print(f"index: n={vecs.shape[0]} dim={dim} M={args.m} efC={args.ef_construction} ef={args.ef}")


def _publish_date(chunks):
    date_re = re.compile(r"发布时间[:：]\s*(\d{4}-\d{2}-\d{2})")
    news_date: dict[str, str] = {}
    for c in chunks:
        nid = c["news_id"]
        if nid in news_date:
            continue
        pt = (c.get("publish_time") or "")[:10]
        if not pt:
            m = date_re.search(c["text"])
            pt = m.group(1) if m else ""
        if pt:
            news_date[nid] = pt
    return news_date


def cmd_hot(args) -> None:
    import datetime as dt

    import hnswlib
    import numpy as np

    exp = Path(args.exp_dir)
    chunks = [json.loads(line) for line in (exp / "chunks.jsonl").open(encoding="utf-8")]
    news_date = _publish_date(chunks)
    if not news_date:
        raise SystemExit("no publish date found in chunks; cannot build hot tier")
    now = args.now or max(news_date.values())
    cutoff = (dt.date.fromisoformat(now) - dt.timedelta(days=args.days)).isoformat()
    hot_news = {n for n, d in news_date.items() if d >= cutoff}
    rows = [i for i, c in enumerate(chunks) if c["news_id"] in hot_news]

    vecs = np.load(exp / "embeddings.npy")[rows].astype("float32")
    out = Path(args.out_dir) if args.out_dir else exp / f"hot_{args.days}d"
    out.mkdir(parents=True, exist_ok=True)

    np.save(out / "embeddings.npy", vecs)
    np.save(out / "embeddings_fp16.npy", vecs.astype("float16"))
    with (out / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            d = dict(chunks[r])
            d["global_index"] = r
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    index = hnswlib.Index(space="ip", dim=vecs.shape[1])
    index.init_index(max_elements=vecs.shape[0], ef_construction=args.ef_construction, M=args.m)
    index.add_items(vecs, np.arange(vecs.shape[0]))
    index.set_ef(args.ef)
    index.save_index(str(out / "index.bin"))

    questions = json.loads(Path(args.questions).read_text(encoding="utf-8")) \
        if Path(args.questions).exists() else []
    answerable = [q.get("id") for q in questions
                  if {news_id_of(u) for u in (q.get("expected_urls") or [])} & hot_news]

    manifest = {
        "tier": "hot",
        "now": now,
        "days": args.days,
        "cutoff": cutoff,
        "articles": len(hot_news),
        "chunks": int(vecs.shape[0]),
        "dim": int(vecs.shape[1]),
        "hnsw": {"space": "ip", "M": args.m, "ef_construction": args.ef_construction, "ef": args.ef},
        "storage_bytes": {"fp32": int(vecs.nbytes), "fp16": int(vecs.astype("float16").nbytes)},
        "questions_total": len(questions),
        "questions_answerable_in_hot": len(answerable),
        "answerable_question_ids": answerable,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    print(f"hot: now={now} days={args.days} cutoff={cutoff}")
    print(f"  articles={manifest['articles']} chunks={manifest['chunks']} dim={manifest['dim']}")
    print(f"  hnsw M={args.m} efC={args.ef_construction} ef={args.ef}")
    print(f"  fp32={vecs.nbytes/1048576:.1f}MB fp16={vecs.astype('float16').nbytes/1048576:.1f}MB")
    print(f"  answerable questions={len(answerable)}/{len(questions)}")
    print(f"  -> {out}")


def cmd_eval(args) -> None:
    import hnswlib
    import numpy as np
    from sentence_transformers import SentenceTransformer

    chunks = load_chunks()
    ids = [c["news_id"] for c in chunks]
    index = hnswlib.Index(space="ip", dim=int(np.load(EXP_DIR / "embeddings.npy").shape[1]))
    index.load_index(str(EXP_DIR / "index.bin"), max_elements=len(chunks))
    index.set_ef(args.ef)

    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    model = SentenceTransformer(args.model, device=args.device)
    model.max_seq_length = args.max_seq_length
    queries = [QUERY_INSTRUCTION + q["question"] for q in questions]
    qvecs = model.encode(queries, batch_size=args.batch_size,
                         normalize_embeddings=True, convert_to_numpy=True).astype("float32")

    labels, distances = index.knn_query(qvecs, k=args.topk)
    rows = []
    for q, labs, dists in zip(questions, labels, distances):
        expected = {news_id_of(u) for u in (q.get("expected_urls") or [])}
        got = [ids[i] for i in labs]
        seen, dedup = set(), []
        for g in got:
            if g not in seen:
                seen.add(g)
                dedup.append(g)
        rank = next((r for r, g in enumerate(dedup, 1) if g in expected), 0)
        rows.append({"id": q.get("id"), "topic": q.get("topic"),
                     "expected": sorted(expected), "got": dedup, "rank": rank,
                     "top1_score": float(dists[0])})

    n = len(rows)

    def recall_at(k):
        return sum(1 for r in rows if 0 < r["rank"] <= k) / n

    mrr = sum((1.0 / r["rank"]) for r in rows if r["rank"]) / n
    print(f"\n===== offline BGE + hnswlib (k={args.topk}) =====")
    print(f"questions={n}")
    for k in (1, 5, 10):
        if k <= args.topk:
            print(f"recall@{k:<2} = {recall_at(k):.4f}")
    print(f"MRR        = {mrr:.4f}")
    out = EXP_DIR / "eval_results.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", out)


def main() -> None:
    global EXP_DIR
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--exp-dir", default=str(EXP_DIR))

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", parents=[common])
    p.add_argument("--articles", default=str(DEFAULT_ARTICLES))
    p.add_argument("--chunk-size", type=int, default=500)
    p.add_argument("--overlap", type=int, default=64)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("embed", parents=[common])
    p.add_argument("--model", default=str(DEFAULT_MODEL))
    p.add_argument("--device", default="cuda")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-seq-length", type=int, default=512)
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("index", parents=[common])
    p.add_argument("--m", type=int, default=32)
    p.add_argument("--ef-construction", type=int, default=200)
    p.add_argument("--ef", type=int, default=64)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("hot", parents=[common])
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--now", default=None, help="YYYY-MM-DD, 默认取语料最大发布时间")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    p.add_argument("--m", type=int, default=32)
    p.add_argument("--ef-construction", type=int, default=200)
    p.add_argument("--ef", type=int, default=64)
    p.set_defaults(func=cmd_hot)

    p = sub.add_parser("eval", parents=[common])
    p.add_argument("--model", default=str(DEFAULT_MODEL))
    p.add_argument("--device", default="cuda")
    p.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--max-seq-length", type=int, default=512)
    p.add_argument("--topk", type=int, default=10)
    p.add_argument("--ef", type=int, default=64)
    p.set_defaults(func=cmd_eval)

    args = parser.parse_args()
    EXP_DIR = Path(args.exp_dir)
    args.func(args)


if __name__ == "__main__":
    main()
