"""Run the full annual synthetic ANN workload and save auditable metrics."""
from pathlib import Path
import argparse,json,os,sys
ROOT=Path(__file__).resolve().parents[2]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset",type=Path,default=ROOT/"var/news-capacity-20261027/synthetic-annual-v1")
    p.add_argument("--index-dir",type=Path,default=ROOT/"var/news-capacity-20261027/ivf-benchmark-20261028-v1")
    p.add_argument("--output-dir",type=Path,default=ROOT/"output/annual-capacity-benchmark-20261028/run-v1")
    p.add_argument("--faiss-path",type=Path,default=ROOT/"var/news-capacity-20261027/runtime/faiss-1.12.0")
    p.add_argument("--queries",type=int,default=100)
    p.add_argument("--requests",type=int,default=500)
    p.add_argument("--threads",type=int,default=4)
    args=p.parse_args()
    if not 10<=args.queries<=1000 or not 100<=args.requests<=10000 or not 1<=args.threads<=12:
        p.error("bounded queries/requests/threads required")
    os.environ["OPENBLAS_NUM_THREADS"]=str(args.threads)
    os.environ["OMP_NUM_THREADS"]=str(args.threads)
    sys.path.insert(0,str(ROOT/"agent"))
    if args.faiss_path.is_dir():sys.path.insert(0,str(args.faiss_path))
    import faiss
    from evaluation.annual_capacity_benchmark import run_benchmark
    report=run_benchmark(args.dataset,args.index_dir,args.output_dir,faiss,
        query_count=args.queries,requests=args.requests,threads=args.threads)
    print(json.dumps({"status":report["status"],"cases":report["cases"],
        "lifecycle_probes":report["lifecycle_probes"]},ensure_ascii=False))

if __name__=="__main__":
    main()
