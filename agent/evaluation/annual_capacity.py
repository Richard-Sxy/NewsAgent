"""Explicitly synthetic annual capacity data; never a news relevance gold set."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

DAY=86400
SCHEMA="synthetic-news-capacity-v1"


@dataclass(frozen=True)
class CapacityProfile:
    raw_per_day: int=8000
    admitted_per_day: int=500
    horizon_days: int=365
    hot_days: int=7
    warm_days: int=30
    retention_days: int=365
    chunks_per_document: int=5
    dimensions: int=1024
    seed: int=20261027
    replacement_probes: int=1000
    expired_probe_days: int=7
    as_of: str="2026-10-27T12:00:00+00:00"

    def validated(self):
        for name in ("raw_per_day","admitted_per_day","horizon_days","hot_days","warm_days",
                     "retention_days","chunks_per_document","dimensions","seed"):
            if type(getattr(self,name)) is not int or getattr(self,name)<1:
                raise ValueError("positive integer required: "+name)
        for name in ("replacement_probes","expired_probe_days"):
            if type(getattr(self,name)) is not int or getattr(self,name)<0:
                raise ValueError("nonnegative integer required: "+name)
        if self.admitted_per_day>self.raw_per_day:
            raise ValueError("admission cannot exceed intake")
        if not 0<self.hot_days<self.warm_days<self.retention_days:
            raise ValueError("tier bounds must be strictly increasing")
        if self.horizon_days!=self.retention_days:
            raise ValueError("this annual profile requires horizon == retention")
        if self.chunks_per_document>32 or self.dimensions>4096:
            raise ValueError("synthetic generator memory budget exceeded")
        if self.replacement_probes>self.admitted_per_day*self.horizon_days:
            raise ValueError("too many replacement probes")
        self.clock()
        return self

    def clock(self):
        value=datetime.fromisoformat(self.as_of)
        if value.tzinfo is None:
            raise ValueError("as_of requires a timezone")
        return value.astimezone(timezone.utc)


def tier_at(published, now, profile):
    if published.tzinfo is None or now.tzinfo is None:
        raise ValueError("timezone-aware business times required")
    age=(now-published).total_seconds()
    if age<0:
        raise ValueError("future article")
    # TTL is checked first: re-embedding or touching a row cannot extend its age.
    if age>=profile.retention_days*DAY:
        return "expired"
    return "hot" if age<profile.hot_days*DAY else "warm" if age<profile.warm_days*DAY else "cold"


def capacity_plan(profile):
    p=profile.validated()
    documents=p.admitted_per_day*p.horizon_days
    tier_docs={"hot":p.hot_days*p.admitted_per_day,
        "warm":(p.warm_days-p.hot_days)*p.admitted_per_day,
        "cold":(p.retention_days-p.warm_days)*p.admitted_per_day}
    vectors=documents*p.chunks_per_document
    raw_bytes=vectors*p.dimensions*4
    return {"schema":SCHEMA,"source":"user_provided_business_estimate_plus_explicit_planning_assumptions",
        "raw_articles_per_year":p.raw_per_day*p.horizon_days,
        "admitted_documents_upper_bound":documents,
        "admission_ratio":p.admitted_per_day/p.raw_per_day,
        "interpretation":"500/day treated as net-new upper bound; actual stock is lower if this includes updates",
        "tier_documents":tier_docs,
        "tier_vectors":{k:v*p.chunks_per_document for k,v in tier_docs.items()},
        "active_chunk_vectors":vectors,"float32_vector_bytes":raw_bytes,
        "float32_vector_gib":raw_bytes/(1024**3),
        "index_and_replication_overhead_included":False,
        "assumed_chunks_per_document":p.chunks_per_document,
        "expired_probe_documents":p.expired_probe_days*p.admitted_per_day,
        "replacement_probe_actions":p.replacement_probes,
        "profile":asdict(p)}


def sha_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


def document_record(profile, age_day, slot, *, detailed=False, superseded=False):
    ordinal=age_day*profile.admitted_per_day+slot
    event=f"capacity-event-{ordinal:09d}"
    news_id=f"capacity-news-{ordinal:09d}-"+("v2" if detailed else "v1")
    published=profile.clock()-timedelta(days=age_day,hours=6 if not detailed else 3)
    tier=tier_at(published,profile.clock(),profile)
    # Distinct scenario fields are intentionally synthetic, not claims about real entities.
    claims=[f"合成事件编号={event}；模拟对象编号={ordinal}；模拟阶段=收到记录。"]
    if detailed:
        claims+=["模拟阶段补充=数据完整性检查已完成。","模拟处理结果=进入容量测试队列。"]
    core="【合成容量压测，非真实新闻】"+"".join(claims)
    target_chars=480+(profile.chunks_per_document-1)*420
    filler=f" 此段为{event}的合成存储填充，不提供真实新闻事实，不作为检索质量答案。"
    body=(core+filler*math.ceil(target_chars/len(filler)))[:target_chars]
    state="expired" if tier=="expired" else "superseded_synthetic" if superseded else "active"
    return {"schema":SCHEMA,"synthetic":True,"tenant_id":"synthetic-capacity-only",
        "news_id":news_id,"event_id":event,"source_revision":2 if detailed else 1,
        "title":"【合成压测】"+event+("详细版本" if detailed else "初始版本"),
        "publish_time":published.isoformat(),"publication_date":published.date().isoformat(),
        "body":body,"body_sha256":hashlib.sha256(body.encode()).hexdigest(),
        "facts":claims,"status":state,"target_tier":None if tier=="expired" else tier,
        "replacement_news_id":f"capacity-news-{ordinal:09d}-v2" if superseded else None,
        "source":"synthetic_capacity_generator_not_enterprise_news",
        "quality_status":"scenario_admission_not_model_or_human_quality_verdict"}


def replacement_probe(old, new):
    # A declarative test oracle for a constructed pair, NOT a model assessment.
    if old["event_id"]!=new["event_id"] or not set(old["facts"])<=set(new["facts"]):
        raise ValueError("synthetic replacement does not cover original facts")
    return {"synthetic":True,"oracle":"constructed_scenario_not_llm_or_human_gold",
        "event_id":old["event_id"],"source_news_id":old["news_id"],
        "replacement_news_id":new["news_id"],"fact_coverage_complete_by_construction":True,
        "additional_fact_count":len(new["facts"])-len(old["facts"]),
        "retirement_prerequisite":"replacement index verified and both source revisions still current",
        "expected_source_serving":False,"expected_replacement_serving":True,
        "independent_evidence_in_this_constructed_pair":False}


def emit_dataset(profile, output_dir, *, emit_raw=False, emit_vectors=False, progress=None):
    p=profile.validated()
    out=Path(output_dir)
    if out.exists():
        raise ValueError("output directory must be new; existing datasets are preserved")
    out.parent.mkdir(parents=True,exist_ok=True)
    plan=capacity_plan(p)
    minimum_free=plan["float32_vector_bytes"] if emit_vectors else 0
    minimum_free+=2_000_000_000 if emit_raw else 1_200_000_000
    if shutil.disk_usage(out.parent).free<minimum_free:
        raise ValueError("insufficient disk for the bounded capacity workload")
    out.mkdir()
    started=time.perf_counter()
    notify=progress or (lambda value:None)
    total=plan["admitted_documents_upper_bound"]
    replaced={i*total//p.replacement_probes for i in range(p.replacement_probes)} if p.replacement_probes else set()
    counts=Counter()
    with (out/"synthetic-news.jsonl").open("x",encoding="utf-8") as docs, \
         (out/"synthetic-chunks.jsonl").open("x",encoding="utf-8") as chunks, \
         (out/"replacement-probes.jsonl").open("x",encoding="utf-8") as replacements:
        for day in range(p.horizon_days+p.expired_probe_days):
            for slot in range(p.admitted_per_day):
                ordinal=day*p.admitted_per_day+slot
                old=document_record(p,day,slot,superseded=ordinal in replaced)
                docs.write(canonical(old)+"\n")
                counts["document_versions"]+=1
                current=old
                if ordinal in replaced:
                    current=document_record(p,day,slot,detailed=True)
                    docs.write(canonical(current)+"\n")
                    replacements.write(canonical(replacement_probe(old,current))+"\n")
                    counts["document_versions"]+=1
                    counts["replacement_actions"]+=1
                    counts["superseded_versions"]+=1
                if current["status"]=="expired":
                    counts["expired_documents"]+=1
                    continue
                counts[current["target_tier"]+"_documents"]+=1
                for chunk in range(p.chunks_per_document):
                    chunks.write(canonical({"synthetic":True,"news_id":current["news_id"],
                        "chunk_id":current["news_id"]+f":chunk:{chunk}",
                        "chunk_index":chunk,"tier":current["target_tier"],
                        "embedding_version":"synthetic-random-normal-v1",
                        "dimensions":p.dimensions})+"\n")
                    counts["active_chunks"]+=1
            if day%30==0 or day==p.horizon_days+p.expired_probe_days-1:
                notify({"phase":"documents","days_completed":day+1,"counts":dict(counts)})
    if emit_raw:
        with (out/"synthetic-intake.jsonl").open("x",encoding="utf-8") as stream:
            for day in range(p.horizon_days):
                for slot in range(p.raw_per_day):
                    stream.write(canonical({"synthetic":True,"day_age":day,
                        "raw_id":f"capacity-raw-{day:03d}-{slot:05d}",
                        "scenario_admitted":slot<p.admitted_per_day})+"\n")
                if day%60==0:
                    notify({"phase":"raw_intake","days_completed":day+1})
        counts["raw_records"]=p.raw_per_day*p.horizon_days
    vector_files={}
    if emit_vectors:
        import numpy as np
        rng=np.random.default_rng(p.seed)
        for tier,count in plan["tier_vectors"].items():
            path=out/f"synthetic-vectors-{tier}.npy"
            matrix=np.lib.format.open_memmap(path,mode="w+",dtype=np.float32,shape=(count,p.dimensions))
            for start in range(0,count,4096):
                length=min(4096,count-start)
                values=rng.standard_normal((length,p.dimensions),dtype=np.float32)
                values/=np.linalg.norm(values,axis=1,keepdims=True)
                matrix[start:start+length]=values
                if start%131072==0:
                    notify({"phase":"synthetic_vectors","tier":tier,"rows_written":start+length,"rows":count})
            matrix.flush()
            del matrix
            vector_files[tier]={"path":path.name,"rows":count,"dimensions":p.dimensions,
                "dtype":"float32","sha256":sha_file(path),
                "kind":"normalized_isotropic_random_not_BGE_embeddings"}
    assert counts["active_chunks"]==plan["active_chunk_vectors"]
    for tier,count in plan["tier_documents"].items():
        assert counts[tier+"_documents"]==count
    assert counts["expired_documents"]==plan["expired_probe_documents"]
    files={f.name:{"bytes":f.stat().st_size,"sha256":sha_file(f)}
           for f in sorted(out.iterdir()) if f.is_file() and f.suffix==".jsonl"}
    result={"schema":SCHEMA,"synthetic":True,"capacity_plan":plan,"materialized_counts":dict(counts),
        "files":files,"vectors":vector_files,"raw_materialized":emit_raw,
        "vectors_materialized":emit_vectors,"elapsed_seconds":time.perf_counter()-started,
        "quality_metrics":None,"production_acceptance":False,
        "ttl_scope":"offline workload policy only; no production deletion job was enabled",
        "ranking_scope":"random vectors are for storage/load only, never relevance or BGE quality evaluation"}
    (out/"manifest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    return result
