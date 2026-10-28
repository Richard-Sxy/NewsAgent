"""Bounded local ANN-stage soak over saved synthetic capacity indexes."""
from pathlib import Path
import argparse,itertools,json,os,sys,time
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[2]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir",type=Path,default=ROOT/"output/annual-capacity-benchmark-20261028/run-v1")
    p.add_argument("--index-dir",type=Path,default=ROOT/"var/news-capacity-20261027/ivf-benchmark-20261028-v1")
    p.add_argument("--output-dir",type=Path,default=ROOT/"output/annual-capacity-benchmark-20261028/soak-v1")
    p.add_argument("--faiss-path",type=Path,default=ROOT/"var/news-capacity-20261027/runtime/faiss-1.12.0")
    p.add_argument("--concurrency",type=int,default=10)
    p.add_argument("--duration",type=float,default=60)
    args=p.parse_args()
    if not 1<=args.concurrency<=16 or not 10<=args.duration<=600:
        p.error("bounded concurrency/duration required")
    if args.output_dir.exists():p.error("output directory must be new")
    os.environ["OMP_NUM_THREADS"]="1";os.environ["OPENBLAS_NUM_THREADS"]="1"
    sys.path.insert(0,str(ROOT/"agent"))
    if args.faiss_path.is_dir():sys.path.insert(0,str(args.faiss_path))
    import numpy as np,faiss
    from evaluation.annual_capacity_benchmark import AnnualANN,dump,percentiles,sha
    config=json.loads((args.run_dir/"config.json").read_text())
    report=json.loads((args.run_dir/"report.json").read_text())
    args.output_dir.mkdir(parents=True)
    parameters={"concurrency":args.concurrency,"duration_seconds":args.duration,"nprobe":256,
        "query_vectors_sha256":sha(args.run_dir/"query-vectors.npy"),"max_requests":10000,
        "load":"closed_loop","scope":"local index stage; pre-encoded synthetic query, no HTTP/LLM/PG",
        "completion_definition":"ten actual news results and exact vector-neighbor oracle match",
        "enterprise_business_correctness":False}
    dump(args.output_dir/"config.json",parameters)
    indexes={}
    counts={t:report["index_build"][t]["vectors"] for t in ("hot","warm","cold")}
    offsets={};offset=0
    for tier in ("hot","warm","cold"):
        path=args.index_dir/(tier+".faiss")
        if sha(path)!=report["index_build"][tier]["index_sha256"]:
            raise ValueError("saved index checksum mismatch")
        index=faiss.read_index(str(path));index.nprobe=index.nlist
        if index.ntotal!=counts[tier]:raise ValueError("index size mismatch")
        indexes[tier]=index;offsets[tier]=offset;offset+=counts[tier]//5
    engine=AnnualANN(indexes,counts,offsets,5)
    queries=np.load(args.run_dir/"query-vectors.npy",allow_pickle=False)
    exact=np.load(args.run_dir/"exact-news-top10.npy",allow_pickle=False)
    faiss.omp_set_num_threads(1)
    for q in queries[:10]:engine.search(q)
    sequence=itertools.count()
    started=time.perf_counter();deadline=started+args.duration
    def worker(_):
        faiss.omp_set_num_threads(1)
        local=[]
        while time.perf_counter()<deadline:
            number=next(sequence)
            if number>=parameters["max_requests"]:break
            qi=number%len(queries);begin=time.perf_counter()
            try:
                results=engine.search(queries[qi])
                ids=[r["news_key"] for r in results]
                oracle_match=len(ids)==10 and set(ids)==set(exact[qi].tolist())
                row={"request":number,"query_id":qi,"elapsed_ms":(time.perf_counter()-begin)*1000,
                     "status":"ok" if oracle_match else "oracle_mismatch","news_keys":ids}
            except Exception as exc:
                row={"request":number,"query_id":qi,"elapsed_ms":(time.perf_counter()-begin)*1000,
                     "status":"error","error_type":type(exc).__name__}
            local.append(row)
        return local
    print(json.dumps({"phase":"soak_started",**parameters}),flush=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        samples=[r for batch in pool.map(worker,range(args.concurrency)) for r in batch]
    elapsed=time.perf_counter()-started
    samples.sort(key=lambda r:r["request"])
    good=[r for r in samples if r["status"]=="ok"]
    with (args.output_dir/"requests.jsonl").open("x") as stream:
        for row in samples:stream.write(json.dumps(row)+"\n")
    result={"status":"completed","synthetic":True,"parameters":parameters,
        "measured_wall_seconds":elapsed,"attempted_requests":len(samples),
        "completed_oracle_matched_requests":len(good),"failed_requests":len(samples)-len(good),
        "request_completion_rate":len(good)/len(samples) if samples else None,
        "completed_requests_per_second":len(good)/elapsed,
        "latency_ms":percentiles(r["elapsed_ms"] for r in good),
        "all_requests_latency_ms":percentiles(r["elapsed_ms"] for r in samples),
        "ran_full_duration":elapsed>=args.duration and len(samples)<parameters["max_requests"],
        "enterprise_online_latency":None,"news_relevance_correctness":None}
    dump(args.output_dir/"report.json",result)
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=="__main__":
    main()
