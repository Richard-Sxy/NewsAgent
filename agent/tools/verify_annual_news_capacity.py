"""Full streaming verification of synthetic annual capacity artifacts."""
from pathlib import Path
from collections import Counter
from datetime import datetime
import argparse,hashlib,json,sys,time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"agent"))
from evaluation.annual_capacity import CapacityProfile,sha_file,tier_at

def main():
    import numpy as np
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset",type=Path,default=ROOT/"var/news-capacity-20261027/synthetic-annual-v1")
    p.add_argument("--output",type=Path,default=ROOT/"output/annual-capacity-20261027/validation.json")
    args=p.parse_args()
    if args.output.exists():
        p.error("existing validation preserved; choose new --output")
    started=time.perf_counter()
    base=args.dataset
    manifest=json.loads((base/"manifest.json").read_text())
    profile=CapacityProfile(**manifest["capacity_plan"]["profile"]).validated()
    for name,info in manifest["files"].items():
        if Path(name).name!=name or sha_file(base/name)!=info["sha256"]:
            raise ValueError("artifact hash mismatch: "+name)
    active,other,old={},set(),{}
    counts=Counter()
    with (base/"synthetic-news.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row=json.loads(line)
            if row["synthetic"] is not True:
                raise ValueError("missing synthetic provenance")
            if hashlib.sha256(row["body"].encode()).hexdigest()!=row["body_sha256"]:
                raise ValueError("body hash mismatch")
            expected=tier_at(datetime.fromisoformat(row["publish_time"]),profile.clock(),profile)
            counts[row["status"]]+=1
            if row["status"]=="active":
                if row["news_id"] in active or row["target_tier"]!=expected or expected=="expired":
                    raise ValueError("active serving policy mismatch")
                active[row["news_id"]]=row["event_id"]
                counts[expected+"_documents"]+=1
            else:
                other.add(row["news_id"])
                if row["status"]=="expired" and expected!="expired":
                    raise ValueError("invalid expiry")
                if row["status"]=="superseded_synthetic":
                    old[row["news_id"]]=row["replacement_news_id"]
    if len(set(active.values()))!=len(active):
        raise ValueError("duplicate active event in constructed scenario")
    if not all(nid in active for nid in old.values()):
        raise ValueError("replacement not serving")
    masks={}
    chunk_count=Counter()
    with (base/"synthetic-chunks.jsonl").open() as stream:
        for line in stream:
            row=json.loads(line)
            nid=row["news_id"]
            if nid not in active or nid in other or row["synthetic"] is not True:
                raise ValueError("retired or expired news leaked into vector membership")
            bit=1<<row["chunk_index"]
            if masks.get(nid,0)&bit:
                raise ValueError("duplicate chunk index")
            masks[nid]=masks.get(nid,0)|bit
            chunk_count[row["tier"]]+=1
    if len(masks)!=len(active) or any(v!=(1<<profile.chunks_per_document)-1 for v in masks.values()):
        raise ValueError("missing document chunks")
    if manifest["raw_materialized"]:
        raw=0; admitted=0
        with (base/"synthetic-intake.jsonl").open() as stream:
            for line in stream:
                row=json.loads(line)
                if row["synthetic"] is not True or type(row["scenario_admitted"]) is not bool:
                    raise ValueError("raw provenance mismatch")
                raw+=1
                admitted+=row["scenario_admitted"]
        if raw!=profile.raw_per_day*profile.horizon_days or admitted!=len(active):
            raise ValueError("raw/admission counts mismatch")
    vector_rows=0
    for tier,info in manifest["vectors"].items():
        path=base/info["path"]
        if sha_file(path)!=info["sha256"]:
            raise ValueError("synthetic vector hash mismatch")
        array=np.load(path,allow_pickle=False,mmap_mode="r")
        if array.shape!=(chunk_count[tier],profile.dimensions) or array.dtype!=np.dtype("float32"):
            raise ValueError("vector shape/dtype drift")
        for start in range(0,len(array),4096):
            values=array[start:start+4096]
            if not np.isfinite(values).all() or not np.allclose(np.linalg.norm(values,axis=1),1,atol=1e-5):
                raise ValueError("invalid synthetic vectors")
        vector_rows+=len(array)
    plan=manifest["capacity_plan"]
    if len(active)!=plan["admitted_documents_upper_bound"] or sum(chunk_count.values())!=plan["active_chunk_vectors"]:
        raise ValueError("annual capacity counts drift")
    for tier,expected in plan["tier_documents"].items():
        if counts[tier+"_documents"]!=expected:
            raise ValueError("tier count drift")
    result={"status":"verified_synthetic_capacity","synthetic":True,
        "documents_read":sum(counts[s] for s in ["active","expired","superseded_synthetic"]),
        "active_documents":len(active),"superseded_versions":len(old),
        "expired_documents":counts["expired"],"active_chunk_memberships":sum(chunk_count.values()),
        "vector_rows_verified":vector_rows,"tier_documents":{t:counts[t+"_documents"] for t in ["hot","warm","cold"]},
        "retired_or_expired_vectors_leaked":0,"real_news_quality_validated":False,
        "production_ann_or_ttl_service_validated":False,"elapsed_seconds":time.perf_counter()-started}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(result,ensure_ascii=False))

if __name__=="__main__":
    main()
