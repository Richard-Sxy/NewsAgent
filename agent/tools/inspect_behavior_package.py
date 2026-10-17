"""Verify a built local package and write compact business-readable evidence."""

import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from app.behavior_tags.runtime import LocalBehaviorRuntime


def inspect(package: Path, report_path: Path, tests_path: Path) -> dict:
    runtime = LocalBehaviorRuntime(package, environment="local")
    manifest = runtime.manifest
    topic_counts, behavior_counts, examples, sources = Counter(), Counter(), {}, Counter()
    with runtime._connect() as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("SQLite integrity failed")
        for (raw,) in db.execute("SELECT payload_json FROM content_labels"):
            item = json.loads(raw)
            topic_counts.update(item["topic_ids"])
        # No UV is aggregated. Counts below are numbers of labeled observations.
        for (raw,) in db.execute("SELECT payload_json FROM behavior_labels"):
            item = json.loads(raw)
            behavior_counts.update(item["label_ids"])
            sources[(item["source_dataset_version"], item["scenario_id"], item["metric_definition_id"])] += 1
            for reason in item["reasons"]:
                examples.setdefault(reason["label_id"], reason)
    event_counts, channel_counts, device_counts, origin_counts = Counter(), Counter(), Counter(), Counter()
    event_rows = 0
    start = datetime.fromisoformat(manifest["simulation_window"]["window_start"])
    end = datetime.fromisoformat(manifest["simulation_window"]["window_end"])
    for line in (package / "synthetic_events.jsonl").open():
        event = json.loads(line)
        stamp = datetime.fromisoformat(event["occurred_at"])
        if not start <= stamp <= end:
            raise ValueError("event is outside the simulation window")
        reference = event.get("sampling_reference")
        if reference and datetime.fromisoformat(reference["window_end"]) > stamp:
            raise ValueError("event sampling used a future source window")
        event_counts[event["event_type"]] += 1
        if event["event_type"] == "impression":
            channel_counts[event["channel"]] += 1
            device_counts[event["device"]] += 1
        origin_counts[event.get("source_dataset_version", "unknown")] += 1
        event_rows += 1
    if event_rows != manifest["counts"]["synthetic_events"]:
        raise ValueError("event row count mismatch")
    cohorts, profile_labels, profile_examples = Counter(), Counter(), []
    last_snapshot = max(manifest["profile_snapshot_times"], key=datetime.fromisoformat)
    for tenant in manifest["tenants"]:
        as_of, profiles = runtime.profiles_at(tenant_id=tenant,
            window_start=start, window_end=end)
        assert datetime.fromisoformat(as_of) <= end
        for profile in profiles:
            if profile.get("events_through") and datetime.fromisoformat(profile["events_through"]) > datetime.fromisoformat(as_of):
                raise ValueError("profile contains future events")
            cohorts[profile["cohort_id"]] += 1
            profile_labels.update(profile["behavior_label_ids"])
            if profile["cohort_id"] not in {item["cohort_id"] for item in profile_examples}:
                profile_examples.append({key: value for key, value in profile.items()
                    if key not in {"synthetic_user_id", "tenant_id"}})
    suites = ET.parse(tests_path).getroot().findall("testsuite")
    if any(int(suite.attrib.get("errors", 0)) or int(suite.attrib.get("failures", 0)) for suite in suites):
        raise ValueError("verification requires passing functional tests")
    search_examples = {}
    for topic in ("tech", "finance", "ai", "education", "sports", "auto"):
        search_examples[topic] = runtime.search(tenant_id=manifest["tenants"][0],
            window_start=max(start, end-timedelta(days=1)), window_end=end,
            include_topic_ids=(topic,), limit=2)
    report = {"status": "passed", "source_mode": "synthetic", "package_sha256": runtime.package_sha256,
        "manifest_identity_sha256": manifest["identity_sha256"], "counts": manifest["counts"],
        "source_count": len(manifest["source_identity"].get("sources", [manifest["source_identity"]])),
        "source_observation_counts": [{"dataset_version": a, "scenario_id": b,
            "metric_definition_id": c, "observation_count": count} for (a,b,c),count in sorted(sources.items())],
        "tests_passed": sum(int(suite.attrib["tests"]) - int(suite.attrib.get("skipped", 0)) for suite in suites),
        "sqlite_integrity": integrity, "event_temporal_and_sampling_checks": "passed",
        "latest_profile_as_of": last_snapshot, "synthetic_user_count": sum(cohorts.values()),
        "cohort_counts": dict(cohorts), "event_type_counts": dict(event_counts),
        "channel_impression_counts": dict(channel_counts), "device_impression_counts": dict(device_counts),
        "event_source_distribution": dict(origin_counts), "profile_label_counts": dict(profile_labels),
        "topic_coverage": [{**topic.model_dump(mode="json"), "content_record_count": topic_counts[topic.topic_id]}
            for topic in runtime.policy.catalog.topics],
        "news_behavior_label_dictionary": [{"label_id": label, "observation_count": count,
            "example_rule_evidence": examples[label]} for label, count in sorted(behavior_counts.items())],
        "profile_examples_without_ids": profile_examples, "search_examples": search_examples,
        "limits": ["functional checks, not recommendation quality evaluation",
            "source scenario metrics remain separate; no UV sum", "profiles and events are synthetic",
            "frontend unrelated port assertions and sandbox listen checks remain unresolved",
            "no running service was restarted or deployed"]}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.package, args.report, args.tests)
    print(json.dumps({key: result[key] for key in ["status", "counts", "source_count", "tests_passed",
        "synthetic_user_count", "sqlite_integrity", "event_temporal_and_sampling_checks"]}))
