"""Annual synthetic-vector ANN benchmark; no enterprise or news-quality claims."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import time

import numpy as np

TIERS=("hot","warm","cold")


def percentiles(values):
    values=sorted(float(v) for v in values)
    if any(not math.isfinite(v) or v<0 for v in values):
        raise ValueError("finite nonnegative timings required")
    def percentile(p):
        if not values:return None
        x=(len(values)-1)*p; lo=int(x); hi=min(lo+1,len(values)-1)
        return values[lo]*(hi-x)+values[hi]*(x-lo) if hi!=lo else values[lo]
    return {"count":len(values),"p50":percentile(.5),"p95":percentile(.95),"p99":percentile(.99)}


def fuse_candidates(rows_by_tier,tier_vector_counts,tier_doc_offsets,chunks_per_document=5,k=10):
    best={}
    for tier in TIERS:
        scores,ids=rows_by_tier.get(tier,((),()))
        for score,idx in zip(scores,ids,strict=True):
            score=float(score); idx=int(idx)
            if not math.isfinite(score):
                raise ValueError("non-finite candidate score")
            if idx<0 or idx>=tier_vector_counts[tier]:
                continue
            key=tier_doc_offsets[tier]+idx//chunks_per_document
            value={"news_key":key,"tier":tier,"score":score,"chunk_id":idx}
            old=best.get(key)
            if old is None or (-score,idx)<(-old["score"],old["chunk_id"]):
                best[key]=value
    return sorted(best.values(),key=lambda x:(-x["score"],x["news_key"],x["chunk_id"]))[:k]


def exact_top_news(vectors_by_tier,queries,chunks_per_document=5,k=10,block_docs=1024):
    queries=np.asarray(queries,dtype=np.float32)
    if queries.ndim!=2 or not np.isfinite(queries).all() or block_docs<1:
        raise ValueError("invalid query matrix or block size")
    nq=len(queries); keys=np.empty((nq,0),dtype=np.int64); scores=np.empty((nq,0),dtype=np.float32)
    offset=0
    for tier in TIERS:
        vectors=vectors_by_tier[tier]
        if len(vectors)%chunks_per_document:
            raise ValueError("incomplete document chunk group")
        documents=len(vectors)//chunks_per_document
        for start in range(0,documents,block_docs):
            count=min(block_docs,documents-start)
            chunk=vectors[start*chunks_per_document:(start+count)*chunks_per_document]
            product=np.asarray(chunk,dtype=np.float32)@queries.T
            doc_scores=product.reshape(count,chunks_per_document,nq).max(axis=1)
            take=min(k,count)
            selected=np.argsort(-doc_scores,axis=0,kind="stable")[:take]
            new_scores=np.take_along_axis(doc_scores,selected,axis=0).T
            new_keys=(selected+start+offset).T.astype(np.int64)
            merged_keys=np.concatenate((keys,new_keys),axis=1)
            merged_scores=np.concatenate((scores,new_scores),axis=1)
            keep=min(k,merged_keys.shape[1])
            order=np.lexsort((merged_keys,-merged_scores),axis=1)[:,:keep]
            keys=np.take_along_axis(merged_keys,order,axis=1)
            scores=np.take_along_axis(merged_scores,order,axis=1)
        offset+=documents
    if keys.shape!=(nq,k):
        raise ValueError("fewer than k actual documents")
    return keys


def ann_recall(exact_ids,approx_ids):
    exact=np.asarray(exact_ids); approx=np.asarray(approx_ids)
    if exact.ndim!=2 or exact.shape!=approx.shape or exact.shape[0]==0 or exact.shape[1]==0:
        raise ValueError("matching nonempty query result matrices required")
    return float(np.mean([len(set(a.tolist()) & set(b.tolist()))/len(a) for a,b in zip(exact,approx)]))


def sha(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda:f.read(8*1024*1024),b""):h.update(block)
    return h.hexdigest()


def dump(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


class AnnualANN:
    def __init__(self,indexes,counts,offsets,chunks=5):
        self.indexes,self.counts,self.offsets,self.chunks=indexes,counts,offsets,chunks

    def search(self,query,k=10):
        rows={}
        for tier in TIERS:
            # Read-only synthetic membership gate; not a PostgreSQL lifecycle implementation.
            distance,ids=self.indexes[tier].search(query.reshape(1,-1),100)
            rows[tier]=(distance[0],ids[0])
        return fuse_candidates(rows,self.counts,self.offsets,self.chunks,k)


def measure_case(engine,queries,exact,faiss,*,nprobe,requests,concurrency,warmup=20):
    for index in engine.indexes.values():
        index.nprobe=min(nprobe,index.nlist)
    faiss.omp_set_num_threads(1)
    for i in range(warmup):engine.search(queries[i%len(queries)])
    def request(i):
        faiss.omp_set_num_threads(1)
        started=time.perf_counter()
        try:
            found=engine.search(queries[i%len(queries)])
            return {"request":i,"query_id":i%len(queries),"elapsed_ms":(time.perf_counter()-started)*1000,
                "status":"ok" if len(found)==10 else "short_result",
                "news_keys":[r["news_key"] for r in found]}
        except Exception as exc:
            return {"request":i,"query_id":i%len(queries),"elapsed_ms":(time.perf_counter()-started)*1000,
                    "status":"error","error_type":type(exc).__name__}
    started=time.perf_counter()
    if concurrency==1:
        samples=[request(i) for i in range(requests)]
    else:
        # Closed-loop workers; no queued batch of hundreds of future requests.
        def worker(worker_id):
            return [request(i) for i in range(worker_id,requests,concurrency)]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples=[s for batch in pool.map(worker,range(concurrency)) for s in batch]
    elapsed=time.perf_counter()-started
    samples.sort(key=lambda s:s["request"])
    good=[s for s in samples if s["status"]=="ok"]
    recall_values=[len(set(s["news_keys"]) & set(exact[s["query_id"]].tolist()))/10 for s in good]
    return {"nprobe":nprobe,"concurrency":concurrency,"search_threads_per_worker":1,
        "warmup_requests":warmup,"planned_requests":requests,"completed_requests":len(good),
        "failed_requests":requests-len(good),"elapsed_seconds":elapsed,
        "completed_requests_per_second":len(good)/elapsed,
        "latency_ms":percentiles(s["elapsed_ms"] for s in good),
        "latency_all_requests_ms":percentiles(s["elapsed_ms"] for s in samples),
        "ann_document_recall_at10":float(np.mean(recall_values)) if recall_values else None,
        "scope":"process-local pre-encoded query, sequential three-tier ANN + news dedup/fusion; no HTTP/Embedding/SQL"}, samples


def contains_ids(index,wanted,faiss):
    wanted=np.asarray(wanted,dtype=np.int64)
    found=set()
    for number in range(index.nlist):
        size=index.invlists.list_size(number)
        if not size:continue
        ids=faiss.rev_swig_ptr(index.invlists.get_ids(number),size)
        found.update(int(i) for i in ids[np.isin(ids,wanted)])
    return found


def lifecycle_probe(engine,vectors,manifest,faiss):
    chunk_count=manifest["capacity_plan"]["profile"]["chunks_per_document"]
    replacement_count=manifest["capacity_plan"]["replacement_probe_actions"]
    docs=manifest["capacity_plan"]["admitted_documents_upper_bound"]
    old_groups={t:[] for t in TIERS}
    for number in range(replacement_count):
        document=number*docs//replacement_count
        tier=next(t for t in reversed(TIERS) if document>=engine.offsets[t])
        local=(document-engine.offsets[tier])*chunk_count
        old_groups[tier].extend(range(local,local+chunk_count))
    results=[]
    for tier in TIERS:
        ids=old_groups[tier]
        expired_count=manifest["capacity_plan"]["expired_probe_documents"]*chunk_count if tier=="cold" else 0
        targets=np.asarray(ids,dtype=np.int64)
        if not set(ids)<=contains_ids(engine.indexes[tier],targets,faiss):
            raise ValueError("replacement projection missing before retirement probe")
        old_vectors=np.asarray(vectors[tier][targets],dtype=np.float32)
        expired=np.asarray(vectors[tier][:expired_count],dtype=np.float32)
        extra=np.concatenate((old_vectors,expired),axis=0)
        fake_ids=np.arange(engine.counts[tier],engine.counts[tier]+len(extra),dtype=np.int64)
        before=engine.indexes[tier].ntotal
        engine.indexes[tier].add_with_ids(extra,fake_ids)
        if len(contains_ids(engine.indexes[tier],fake_ids,faiss))!=len(fake_ids):
            raise ValueError("stale probe insertion failed")
        # Injected stale physical entries are rejected by the immutable serving membership.
        gated=fuse_candidates({tier:(np.ones(len(fake_ids)),fake_ids)},engine.counts,engine.offsets,chunk_count,10)
        if gated:raise ValueError("stale members escaped serving gate")
        removed=int(engine.indexes[tier].remove_ids(fake_ids))
        if removed!=len(fake_ids) or contains_ids(engine.indexes[tier],fake_ids,faiss):
            raise ValueError("physical stale deletion readback failed")
        if engine.indexes[tier].ntotal!=before:
            raise ValueError("active projection count changed")
        results.append({"tier":tier,"retired_chunk_probes":len(ids),"expired_chunk_probes":expired_count,
            "physically_removed":removed,"readback_remaining":0,"active_count_restored":True})
    return {"status":"passed_isolated_faiss_index_probe","tiers":results,
        "replacement_document_scenarios":replacement_count,
        "expired_document_scenarios":manifest["capacity_plan"]["expired_probe_documents"],
        "production_worker_or_pg_concurrency_tested":False,
        "model_fact_understanding_tested":False}


def run_benchmark(dataset,index_dir,output,faiss,*,query_count=100,requests=500,threads=4):
    dataset,index_dir,output=Path(dataset),Path(index_dir),Path(output)
    if output.exists() or index_dir.exists():
        raise ValueError("benchmark/index directories must be new")
    output.mkdir(parents=True); index_dir.mkdir(parents=True)
    manifest=json.loads((dataset/"manifest.json").read_text())
    if manifest.get("synthetic") is not True or not manifest.get("vectors_materialized"):
        raise ValueError("expected complete explicitly synthetic dataset")
    profile=manifest["capacity_plan"]["profile"]
    dims,chunks=profile["dimensions"],profile["chunks_per_document"]
    counts={t:manifest["vectors"][t]["rows"] for t in TIERS}
    offsets={}; total=0
    for t in TIERS:offsets[t]=total; total+=counts[t]//chunks
    nlists={"hot":64,"warm":128,"cold":256}
    config={"dataset_manifest_sha256":sha(dataset/"manifest.json"),"faiss_version":faiss.__version__,
        "numpy_version":np.__version__,"python":platform.python_version(),"platform":platform.platform(),
        "dimensions":dims,"vectors":sum(counts.values()),"documents":total,"chunks_per_document":chunks,
        "index_type":"IndexIVFFlat","metric":"inner_product_of_L2_normalized_vectors",
        "nlist":nlists,"training_samples_per_cluster":64,"training_iterations":10,
        "seed":20261028,"train_add_threads":threads,"search_threads_per_worker":1,
        "query_count":query_count,"query_mix":"half independently random; half existing vector plus Gaussian noise",
        "cases":[{"nprobe":256,"requests":100,"concurrency":1},
                 {"nprobe":16,"requests":requests,"concurrency":1},
                 {"nprobe":64,"requests":requests,"concurrency":1},
                 {"nprobe":16,"requests":requests,"concurrency":10}],
        "synthetic":True,"enterprise":False,"news_ndcg":None,
        "scope":"ANN stage only; fixed parameters before observation, not production endpoint"}
    dump(output/"config.json",config)
    faiss.omp_set_num_threads(threads)
    arrays={t:np.load(dataset/manifest["vectors"][t]["path"],allow_pickle=False,mmap_mode="r") for t in TIERS}
    for t in TIERS:
        if arrays[t].shape!=(counts[t],dims) or sha(dataset/manifest["vectors"][t]["path"])!=manifest["vectors"][t]["sha256"]:
            raise ValueError("source vectors integrity mismatch")
    rng=np.random.default_rng(config["seed"])
    queries=rng.standard_normal((query_count,dims),dtype=np.float32)
    for i in range(query_count//2):
        position=int(rng.integers(0,sum(counts.values())))
        for tier in TIERS:
            if position<counts[tier]:
                queries[i]=arrays[tier][position]+queries[i]*np.float32(.05/math.sqrt(dims))
                break
            position-=counts[tier]
    queries/=np.linalg.norm(queries,axis=1,keepdims=True)
    np.save(output/"query-vectors.npy",queries,allow_pickle=False)
    print(json.dumps({"phase":"exact_ground_truth","queries":query_count}),flush=True)
    begin=time.perf_counter()
    exact=exact_top_news(arrays,queries,chunks,10)
    exact_seconds=time.perf_counter()-begin
    np.save(output/"exact-news-top10.npy",exact,allow_pickle=False)
    indexes={}; builds={}
    for tier in TIERS:
        begin=time.perf_counter()
        index=faiss.IndexIVFFlat(faiss.IndexFlatIP(dims),dims,nlists[tier],faiss.METRIC_INNER_PRODUCT)
        index.cp.niter=10; index.cp.seed=config["seed"]
        selected=rng.choice(counts[tier],nlists[tier]*64,replace=False)
        training=np.ascontiguousarray(arrays[tier][selected],dtype=np.float32)
        index.train(training); del training
        for start in range(0,counts[tier],4096):
            block=np.asarray(arrays[tier][start:start+4096],dtype=np.float32)
            index.add_with_ids(block,np.arange(start,start+len(block),dtype=np.int64))
        if index.ntotal!=counts[tier]:raise ValueError("incomplete index")
        path=index_dir/(tier+".faiss")
        faiss.write_index(index,str(path))
        indexes[tier]=index
        builds[tier]={"vectors":index.ntotal,"build_seconds":time.perf_counter()-begin,
                      "index_bytes":path.stat().st_size,"index_sha256":sha(path)}
        print(json.dumps({"phase":"index_built","tier":tier,**builds[tier]}),flush=True)
    engine=AnnualANN(indexes,counts,offsets,chunks)
    cases=[]
    for case in config["cases"]:
        result,samples=measure_case(engine,queries,exact,faiss,**case)
        cases.append(result)
        with (output/"requests.jsonl").open("a") as f:
            for sample in samples:f.write(json.dumps({**case,**sample})+"\n")
        print(json.dumps({"phase":"case_completed",**result}),flush=True)
    # Physical insertion/deletion probes happen after all immutable latency cases.
    probes=lifecycle_probe(engine,arrays,manifest,faiss)
    report={"status":"completed","synthetic":True,"config":config,"index_build":builds,
        "exact_oracle_seconds":exact_seconds,"cases":cases,"lifecycle_probes":probes,
        "max_process_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "source_data_generation":"normalized synthetic random vectors, not news embeddings",
        "news_ndcg":None,"enterprise_online_p95":None,
        "limitations":["query vectors are pre-encoded; no model inference, HTTP, PG or real business correctness",
          "ANN recall is neighbor recall versus exact best-chunk news ranking, not news relevance",
          "all lifetime checks are isolated index probes, not production TTL orchestration"]}
    dump(output/"report.json",report)
    return report
