"""Plan or materialize explicitly synthetic annual capacity workloads."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"agent"))
from evaluation.annual_capacity import CapacityProfile,capacity_plan,emit_dataset

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile",type=Path,default=ROOT/"deploy/annual-news-capacity.profile.json")
    p.add_argument("--output-dir",type=Path,default=ROOT/"var/news-capacity-20261027/synthetic-annual-v1")
    p.add_argument("--plan-only",action="store_true")
    p.add_argument("--emit-raw",action="store_true")
    p.add_argument("--emit-vectors",action="store_true")
    p.add_argument("--summary",type=Path,default=ROOT/"output/annual-capacity-20261027/summary.json")
    args=p.parse_args()
    config=json.loads(args.profile.read_text())
    profile=CapacityProfile(**config["generator"]).validated()
    if args.plan_only:
        print(json.dumps(capacity_plan(profile),ensure_ascii=False,indent=2))
        return
    if args.summary.exists():
        p.error("summary already exists; choose a new --summary")
    result=emit_dataset(profile,args.output_dir,emit_raw=args.emit_raw,emit_vectors=args.emit_vectors,
        progress=lambda row:print(json.dumps(row,ensure_ascii=False),flush=True))
    result["dataset_path"]=str(args.output_dir.resolve())
    result["profile_path"]=str(args.profile.resolve())
    args.summary.parent.mkdir(parents=True,exist_ok=True)
    args.summary.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"status":"generated_synthetic_only","summary":str(args.summary),
                      "counts":result["materialized_counts"]},ensure_ascii=False))

if __name__=="__main__":
    main()
