"""Verify the transferred frozen evaluation without GPU, model weights or APIs."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"agent"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=ROOT)
    args=parser.parse_args()
    root=args.root.resolve()
    import numpy as np
    from app.modules.retrieval_lab.comparison import digest,load_review,sha_file
    from app.modules.retrieval_lab.engine import chunk_news,_json_bytes

    review_dir=root/"output/news-events-annual-20261027/human-review-v1"
    directory=root/"output/news-events-annual-20261027/comparison-v1"
    package=root/"var/news-events-annual-20261027/retrieval-lab-v1"
    review=load_review(review_dir)
    comp=json.loads((directory/"comparison.json").read_text())
    evidence=json.loads((directory/"evidence-manifest.json").read_text())
    if sha_file(directory/"comparison.json")!=evidence["comparison_sha256"]:
        raise ValueError("comparison checksum mismatch")
    if digest(comp["config"])!=comp["config_sha256"]:
        raise ValueError("comparison config checksum mismatch")
    if sha_file(review_dir/"human-review.export.json")!=comp["config"]["review_export_sha256"]:
        raise ValueError("human review identity mismatch")
    manifest=json.loads((package/"manifest.json").read_text())
    for name,info in manifest["files"].items():
        if Path(name).name!=name or sha_file(package/name)!=info["sha256"]:
            raise ValueError("corpus artifact checksum mismatch: "+name)
    if manifest!=review["manifest"]:
        raise ValueError("review and corpus manifests differ")
    index=package/"vector-index"
    index_manifest=json.loads((index/"manifest.json").read_text())
    if sha_file(index/"manifest.json")!=evidence["source_index_manifest_sha256"]:
        raise ValueError("index manifest checksum mismatch")
    if sha_file(index/"vectors.npy")!=index_manifest["vectors_sha256"]:
        raise ValueError("document vectors checksum mismatch")
    news=[json.loads(s) for s in (package/"news.jsonl").read_text().splitlines() if s]
    chunks=chunk_news(sorted(news,key=lambda n:n["news_id"]))
    if hashlib.sha256(_json_bytes(chunks)).hexdigest()!=index_manifest["identity"]["chunks_sha256"]:
        raise ValueError("chunk policy or corpus drift")
    vectors=np.load(index/"vectors.npy",allow_pickle=False)
    if vectors.shape!=(len(chunks),1024) or not np.isfinite(vectors).all():
        raise ValueError("invalid document vector shape or coordinates")
    source={r["query_id"]:r for r in review["runs"]}
    required=set()
    for q in comp["queries"]:
        query_file=directory/"query-vectors"/(q["query_id"]+".npy")
        if sha_file(query_file)!=evidence["query_vectors"][query_file.name]:
            raise ValueError("query vector checksum mismatch")
        query=np.load(query_file,allow_pickle=False)
        if query.shape!=(1024,) or not np.isfinite(query).all():
            raise ValueError("invalid query vector")
        similarity=vectors@query
        rrf,all_hits={},[]
        for tier in ("hot","warm","cold"):
            positions=sorted((i for i,c in enumerate(chunks) if c["tier"]==tier),
                key=lambda i:(-float(similarity[i]),chunks[i]["chunk_id"]))
            seen=set()
            for i in positions:
                nid=chunks[i]["news_id"]
                if nid in seen:
                    continue
                seen.add(nid)
                rrf[nid]=comp["config"]["weights"][tier]/(comp["config"]["rrf_k"]+len(seen))
                all_hits.append((nid,chunks[i]["chunk_id"],float(similarity[i])))
                if len(seen)>=comp["config"]["candidate_limit_per_tier"]:
                    break
        expected_rrf=sorted(rrf,key=lambda nid:(-rrf[nid],nid))[:10]
        expected_global=[r[0] for r in sorted(all_hits,key=lambda r:(-r[2],r[0],r[1]))[:10]]
        actual_rrf=[r["news_id"] for r in q["predictions"]["legacy_rrf"]]
        actual_global=[r["news_id"] for r in q["predictions"]["global_similarity"]]
        if expected_rrf!=actual_rrf or expected_global!=actual_global:
            raise ValueError("ranking replay failed: "+q["query_id"])
        if source[q["query_id"]]["query"]!=q["query"] or actual_global!=[c["news_id"] for c in source[q["query_id"]]["candidates"]]:
            raise ValueError("frozen query task drift")
        required.update((q["query_id"],nid) for nid in actual_rrf+actual_global)
    with sqlite3.connect("file:"+(directory/"supplemental.sqlite3").as_posix()+"?mode=ro",uri=True) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("supplemental database integrity failed")
        meta,expected=db.execute("SELECT payload,sha256 FROM lab_manifest").fetchone()
        if hashlib.sha256(meta.encode()).hexdigest()!=expected or json.loads(meta)["comparison_sha256"]!=evidence["comparison_sha256"]:
            raise ValueError("supplemental manifest drift")
        tasks=db.execute("SELECT COUNT(*) FROM lab_run_candidates").fetchone()[0]
        judgments=dict(db.execute("SELECT label,COUNT(*) FROM lab_judgments GROUP BY label"))
    differences=[name for name,old in comp["config"]["source_code"].items()
                 if not (root/name).is_file() or sha_file(root/name)!=old]
    print(json.dumps({"status":"verified","root":str(root),"queries_replayed":len(comp["queries"]),
        "original_judgments":sum(len(r["candidates"]) for r in review["runs"]),
        "union_pairs":len(required),"supplemental_tasks":tasks,"supplemental_labels":judgments,
        "historical_source_differences":differences,
        "note":"Historical execution hashes are preserved. Rankings were independently replayed; changed runner code is listed, not silently rehashed."},
        ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
