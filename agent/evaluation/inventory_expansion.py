"""Offline inventory review material, never gold labels or live indexes.

Python 3.11+. Run: python -m evaluation.inventory_expansion build|verify.
Only explicit frozen files are read; no Settings, dotenv, models or database.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
import math
from pathlib import Path
import re
import tempfile
import unicodedata

from app.modules.news_storage.model import RECORD_KEYS, canonical_json, normalize_record
from app.modules.news_storage.package_io import load_package

ROOT = Path(__file__).resolve().parents[2]
VERSION = "inventory-review-builder-v2"
NEWS_ID = re.compile(r"20\d{6}[A-Za-z0-9_-]{5,}")


def digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path):
    h = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def strict_json(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value
    def invalid(value):
        raise ValueError(f"invalid JSON constant: {value}")
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        return [strict_json(line) for line in handle if line.strip()]


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps must have an explicit timezone")
    return result


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def body_fingerprint(body):
    return sha256(normalize(body).encode("utf-8")).hexdigest() if body else None


def source_document_hash(source):
    # Same text identity convention as related_news_review_v1; record identity
    # additionally binds metadata/fragments and has its own separate field.
    return sha256((source["title"] + "\x1f" + source["body"]).encode("utf-8")).hexdigest()


def source_topic_allowed(row, topic):
    return (topic["id"] in row["sampling_topic_hints"]
            and not any(term.casefold() in (row["title"] or "").casefold()
                        for term in topic.get("source_title_exclusions", [])))


def grams(text, size=3):
    text = normalize(text)
    return {text[i:i + size] for i in range(max(0, len(text) - size + 1))}


def title_hits(title, topics):
    """Sampling hints only: never official boards or health exclusion."""
    hits = []
    for topic in topics:
        for term in topic["title_terms"]:
            pattern = re.escape(term)
            if re.fullmatch(r"[A-Za-z0-9]+", term):
                pattern = r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])"
            match = re.search(pattern, title or "", re.IGNORECASE)
            if match:
                hits.append({"topic_id": topic["id"], "term": term,
                             "start": match.start(), "end": match.end(),
                             "basis": "title_keyword_sampling_only"})
    return hits


def load_source(package_dir, export_path, export_manifest):
    """Reuse immutable content checks, then bind each joined export row."""
    package = load_package(Path(package_dir))
    wrapper = strict_json(Path(export_manifest).read_text(encoding="utf-8"))
    if file_hash(export_path) != wrapper["sha256"]:
        raise ValueError("tagged export checksum mismatch")
    rows = read_jsonl(export_path)
    if len(rows) != wrapper["counts"]["exported"]:
        raise ValueError("tagged export row count mismatch")
    selected = {r["news_id"]: r for r in package["items"]}
    versions = {(r["news_id"], r["record_sha256"]): r for r in package["versions"]}
    seen = set()
    for row in rows:
        nid = row["news_id"]
        if nid in seen or nid not in selected:
            raise ValueError("duplicate/unknown export news_id")
        seen.add(nid)
        selected_row = selected[nid]
        record = versions[(nid, selected_row["record_sha256"])]
        if any(row.get(key) != value for key, value in record.items()):
            raise ValueError("export differs from frozen selected content")
        if row["selection_quality_flags"] != selected_row["quality_flags"]:
            raise ValueError("export selection quality flags differ")
        for key in ("tenant_id", "release_id"):
            if row[key] != package["manifest"][key] or row[key] != wrapper[key]:
                raise ValueError("export scope/release mismatch")
        tags = row["tags"]
        if (tags["news_id"] != nid or tags["record_sha256"] != row["record_sha256"]
                or tags["body_sha256"] != row["body_sha256"]
                or tags["tag_release_id"] != wrapper["tag_release_id"]):
            raise ValueError("tag identity does not bind selected content")
    if seen != set(selected):
        raise ValueError("export omits selected inventory records")
    return sorted(rows, key=lambda r: r["news_id"]), package["manifest"]


def existing_evaluation_ids(root):
    """Reserve identities already visible in local business evaluations."""
    ids, files = set(), {}
    def visit(value):
        if isinstance(value, str):
            ids.update(NEWS_ID.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for category in ("retrieval", "hot_news", "writing"):
        directory = Path(root) / category
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
                continue
            if "finqa" in path.relative_to(directory).parts:
                continue
            value = read_jsonl(path) if path.suffix == ".jsonl" else strict_json(path.read_text(encoding="utf-8"))
            visit(value)
            files[str(path.relative_to(root))] = file_hash(path)
    return ids, files


class Groups:
    def __init__(self, ids):
        self.parent = {nid: nid for nid in ids}

    def find(self, nid):
        while self.parent[nid] != nid:
            self.parent[nid] = self.parent[self.parent[nid]]
            nid = self.parent[nid]
        return nid

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        self.parent[max(left, right)] = min(left, right)


def propose_groups(rows, threshold):
    """Leakage warnings, not adjudicated semantic event groups."""
    union = Groups(r["news_id"] for r in rows)
    seen_body, seen_title, seen_fragment, titles = {}, {}, {}, {}
    postings, links = defaultdict(list), []
    def merge(a, b, reason, score=1.0):
        union.union(a, b)
        links.append({"left_news_id": a, "right_news_id": b,
                      "reason": reason, "score": round(score, 6)})
    for row in rows:
        nid, body, title = row["news_id"], normalize(row["body"] or ""), normalize(row["title"] or "")
        for key, table, reason in ((body if len(body) >= 80 else "", seen_body, "normalized_body_equal"),
                                   (title if len(title) >= 8 else "", seen_title, "normalized_title_equal")):
            if not key:
                continue
            h = sha256(key.encode("utf-8")).hexdigest()
            if h in table:
                merge(table[h], nid, reason)
            else:
                table[h] = nid
        for part in row["fragments"]:
            text = normalize(part["text"])
            if len(text) < 120:
                continue
            h = sha256(text.encode("utf-8")).hexdigest()
            if h in seen_fragment and seen_fragment[h] != nid:
                merge(seen_fragment[h], nid, "identical_historical_fragment")
            else:
                seen_fragment[h] = nid
        tokens = grams(title)
        if len(tokens) < 6:
            continue
        candidates = set()
        for token in tokens:
            candidates.update(postings[token])
        for other in sorted(candidates):
            prior = titles[other]
            if min(len(tokens), len(prior)) / max(len(tokens), len(prior)) < threshold:
                continue
            score = len(tokens & prior) / len(tokens | prior)
            if score >= threshold:
                merge(other, nid, "title_trigram_jaccard", score)
        titles[nid] = tokens
        for token in tokens:
            postings[token].append(nid)
    members = defaultdict(list)
    for row in rows:
        members[union.find(row["news_id"])].append(row["news_id"])
    by_id, records = {}, []
    for values in members.values():
        values.sort()
        gid = "leakage-" + digest(values)[:20]
        records.append({"proposed_group_id": gid, "news_ids": values,
                        "event_review_status": "pending", "event_group_id": None, "split": None})
        by_id.update({nid: gid for nid in values})
    return by_id, sorted(records, key=lambda r: r["proposed_group_id"]), links


def build_inventory(rows, policy, protected_ids, groups):
    as_of = instant(policy["as_of"])
    protected_groups = {groups[nid] for nid in protected_ids if nid in groups}
    output = []
    for row in rows:
        body = row["body"] or ""
        flags = sorted(set(row["quality_flags"] + row["selection_quality_flags"]))
        published = instant(row["publish_time"]) if row["publish_time"] else None
        complete = all(row.get(key) for key in ("title", "publish_time", "url"))
        future = published is not None and published > as_of
        candidate = (row["content_state"] == "body_available"
                     and len(body.strip()) >= policy["min_candidate_body_chars"]
                     and complete and not future and "metadata_review_required" not in flags
                     and row["content_type"] == "article")
        matches = title_hits(row["title"], policy["topics"])
        age = (as_of.astimezone(timezone.utc) - published.astimezone(timezone.utc)).days if published else None
        age_bucket = ("unknown" if age is None else "future" if future else
                      "0_1_days" if age <= 1 else "2_7_days" if age <= 7 else
                      "8_30_days" if age <= 30 else "31_plus_days")
        output.append({
            "news_id": row["news_id"], "release_id": row["release_id"],
            "record_sha256": row["record_sha256"], "body_sha256": row["body_sha256"],
            "normalized_body_sha256": body_fingerprint(row["body"]),
            "title": row["title"], "content_state": row["content_state"],
            "content_type": row["content_type"], "body_chars": len(body.strip()),
            "fragment_count": len(row["fragments"]), "publish_time": row["publish_time"],
            "quality_flags": flags, "metadata_complete": bool(complete),
            "future_at_as_of": future, "age_bucket": age_bucket,
            "source_board": None, "board_review_status": "unknown_source_classification",
            "sampling_topic_hints": sorted({m["topic_id"] for m in matches}),
            "sampling_topic_matches": matches, "topics_are_gold": False,
            "legacy_inferred_topic_ids": row["tags"]["topic_ids"],
            "content_candidate": bool(candidate),
            "source_candidate": bool(candidate and len(body.strip()) >= policy["min_source_body_chars"]),
            "proposed_group_id": groups[row["news_id"]],
            "existing_eval_exposure": row["news_id"] in protected_ids,
            "group_has_existing_eval_exposure": groups[row["news_id"]] in protected_groups,
            "event_group_id": None, "split": None, "human_reviewed": False,
        })
    return output


def select_sources(inventory, policy, forced_topics=None):
    by_id = {r["news_id"]: r for r in inventory}
    selected, used_groups, counts = [], set(), Counter()
    topics = [t["id"] for t in policy["topics"]]
    topic_policy = {t["id"]: t for t in policy["topics"]}
    for nid, hint in (forced_topics or {}).items():
        row = by_id[nid]
        if not row["source_candidate"] or row["group_has_existing_eval_exposure"]:
            raise ValueError(f"QA source is ineligible or already exposed: {nid}")
        if row["proposed_group_id"] in used_groups:
            raise ValueError("QA sources share a proposed leakage group")
        if hint not in topics or not source_topic_allowed(row, topic_policy[hint]):
            raise ValueError(f"QA sampling topic does not match title hints: {nid}")
        if counts[hint] >= policy["sources_per_topic"]:
            raise ValueError("QA sources exceed the configured per-topic sample")
        selected.append((row, hint))
        used_groups.add(row["proposed_group_id"])
        counts[hint] += 1
    pools = {}
    for topic in topics:
        candidates = [r for r in inventory if r["source_candidate"]
                      and not r["group_has_existing_eval_exposure"]
                      and source_topic_allowed(r, topic_policy[topic])]
        dates = defaultdict(list)
        for row in candidates:
            dates[row["publish_time"][:10]].append(row)
        for values in dates.values():
            values.sort(key=lambda r: digest([policy["seed"], topic, r["news_id"]]))
        queue = []
        while any(dates.values()):
            for day in sorted(dates):
                if dates[day]:
                    queue.append(dates[day].pop(0))
        pools[topic] = queue
    while True:
        changed = False
        for topic in topics:
            if counts[topic] >= policy["sources_per_topic"]:
                continue
            while pools[topic]:
                row = pools[topic].pop(0)
                if row["proposed_group_id"] not in used_groups:
                    selected.append((row, topic))
                    counts[topic] += 1
                    used_groups.add(row["proposed_group_id"])
                    changed = True
                    break
        if not changed:
            break
    return selected


def candidate_pool(source, corpus, features, postings, policy, info):
    """Lexical/same-hint/diverse preview, not measured BGE results."""
    nid = source["news_id"]
    tokens = grams((source["title"] or "") + (source["body"] or "")[:160], 2)
    idf = {t: math.log(1 + len(corpus) / (1 + len(postings[t]))) for t in tokens}
    scores = defaultdict(float)
    for token in sorted(tokens, key=lambda t: (-idf[t], t))[:60]:
        for other in postings[token]:
            if other != nid:
                scores[other] += idf[token]
    ranked = sorted(scores, key=lambda n: (-scores[n] / math.sqrt(max(1, len(features[n]))), n))
    result = []
    seen = {other for other in info if other == nid or (
        info[other]["normalized_body_sha256"] is not None
        and info[other]["normalized_body_sha256"] == info[nid]["normalized_body_sha256"])}
    def add(other, origin):
        if other not in seen:
            seen.add(other)
            result.append({"news_id": other, "selection_origin": origin,
                           "lexical_score": round(scores.get(other, 0.0), 6)})
    count = policy["candidates_per_query"]
    for other in ranked[:max(1, count - 4)]:
        add(other, "lexical_candidate_not_relevance_label")
    hints = set(info[nid]["sampling_topic_hints"])
    same = [r["news_id"] for r in corpus if hints & set(info[r["news_id"]]["sampling_topic_hints"])]
    same.sort(key=lambda other: digest([policy["seed"], nid, other]))
    for other in same:
        if len(result) >= count - 2:
            break
        add(other, "same_hint_candidate_not_negative_label")
    diverse = sorted((r["news_id"] for r in corpus),
                     key=lambda other: digest([policy["seed"], "diverse", nid, other]))
    for other in diverse:
        if len(result) >= count:
            break
        add(other, "diversity_candidate_not_negative_label")
    return result


def quality_cases(inventory, policy):
    predicates = {
        "long_body": lambda r: r["source_candidate"] and r["body_chars"] >= 800,
        "medium_body": lambda r: r["source_candidate"] and 300 <= r["body_chars"] < 800,
        "short_body_information_review": lambda r: r["content_state"] == "body_available" and 100 <= r["body_chars"] < 300,
        "very_short_body": lambda r: r["content_state"] == "body_available" and r["body_chars"] < 100,
        "historical_fragments_missing_metadata": lambda r: r["content_state"] == "fragments_only",
        "title_only_article": lambda r: r["content_state"] == "title_only" and r["content_type"] == "article",
        "video_without_verified_text": lambda r: r["content_type"] == "video",
        "metadata_review_required": lambda r: "metadata_review_required" in r["quality_flags"],
        "health_topic_is_not_source_board": lambda r: "health" in r["legacy_inferred_topic_ids"],
        "no_priority_title_match": lambda r: r["source_candidate"] and not r["sampling_topic_hints"],
    }
    output, used = [], set()
    for name, predicate in predicates.items():
        candidates = sorted((r for r in inventory if predicate(r)),
                            key=lambda r: digest([policy["seed"], name, r["news_id"]]))
        selected = 0
        for row in candidates:
            if row["news_id"] in used:
                continue
            used.add(row["news_id"])
            output.append({"case_id": "quality-" + row["news_id"], "news_id": row["news_id"],
                           "record_sha256": row["record_sha256"], "stratum": name,
                           "observed_content_state": row["content_state"],
                           "source_board": None, "reviewed_board": None,
                           "content_sufficiency": None, "index_recommendation": None,
                           "reviewer_id": None, "review_notes": None, "status": "pending",
                           "human_reviewed": False, "split": None, "event_group_id": None,
                           "proposed_group_id": row["proposed_group_id"]})
            selected += 1
            if selected >= policy["quality_cases_per_stratum"]:
                break
    return output


def qa_drafts(seeds, by_id, info, policy):
    output = []
    for seed in seeds:
        source = by_id[seed["news_id"]]
        if not info[seed["news_id"]]["source_candidate"] or info[seed["news_id"]]["group_has_existing_eval_exposure"]:
            raise ValueError("QA source must be an unexposed complete-body candidate")
        spans = []
        for quote in seed["evidence_quotes"]:
            if not quote or quote not in source["body"]:
                raise ValueError(f"QA quote not in frozen body: {seed['news_id']}")
            start = source["body"].index(quote)
            spans.append({"news_id": source["news_id"], "record_sha256": source["record_sha256"],
                          "body_sha256": source["body_sha256"], "start": start,
                          "end": start + len(quote), "quote": quote})
        if not spans or not seed["question"].strip() or not seed["candidate_answer"].strip():
            raise ValueError("QA requires question, answer and exact quotes")
        output.append({
            "case_id": seed["case_id"], "task": "evidence_grounded_qa",
            "question": seed["question"], "candidate_answer": seed["candidate_answer"],
            "source_news_id": source["news_id"], "sampling_topic": seed["sampling_topic"],
            "as_of": policy["as_of"], "evidence": spans,
            "answer_origin": "assistant_authored_pending_independent_human_review",
            "status": "draft", "human_reviewed": False, "reviewer_id": None,
            "reviewed_answer": None, "event_group_id": None, "split": None,
            "proposed_group_id": info[seed["news_id"]]["proposed_group_id"], "training_eligible": False,
            "factual_scope": "frozen_cached_report_not_independent_fact_verification",
        })
    if len({r["case_id"] for r in output}) != len(output):
        raise ValueError("duplicate QA case_id")
    return output


def build(rows, policy, protected_ids, qa_seeds=()):
    rows = sorted(rows, key=lambda r: r["news_id"])
    if len({r["news_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate source news_id")
    if not 0 < policy["near_title_jaccard"] <= 1:
        raise ValueError("near_title_jaccard must be in (0, 1]")
    for key in ("min_source_body_chars", "min_candidate_body_chars", "sources_per_topic",
                "candidates_per_query", "quality_cases_per_stratum"):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if policy["candidates_per_query"] < 5:
        raise ValueError("candidate pool needs at least 5 entries")
    groups, group_records, links = propose_groups(rows, policy["near_title_jaccard"])
    inventory = build_inventory(rows, policy, protected_ids, groups)
    info, by_id = {r["news_id"]: r for r in inventory}, {r["news_id"]: r for r in rows}
    forced = {}
    for seed in qa_seeds:
        nid, topic = seed["news_id"], seed["sampling_topic"]
        if nid in forced and forced[nid] != topic:
            raise ValueError("QA seeds disagree on source sampling topic")
        forced[nid] = topic
    selected = select_sources(inventory, policy, forced)
    corpus = [r for r in rows if info[r["news_id"]]["content_candidate"]]
    features, postings = {}, defaultdict(set)
    for row in corpus:
        nid = row["news_id"]
        features[nid] = grams((row["title"] or "") + (row["body"] or "")[:800], 2)
        for token in features[nid]:
            postings[token].add(nid)
    queries, pairs, pool_audit, evidence_ids = [], [], [], set()
    for item, topic in selected:
        source = by_id[item["news_id"]]
        nid, qid = source["news_id"], "inventory-related-" + source["news_id"]
        summary = source["body"][:300]
        queries.append({
            "query_id": qid, "task": "related_news", "source_news_id": nid,
            "query": source["title"] + " " + summary, "source_title": source["title"],
            "source_summary": summary, "summary_policy": "body_prefix_300_chars_extractive_proxy_v1",
            "source_publish_time": source["publish_time"], "source_url": source["url"],
            "source_record_sha256": source["record_sha256"],
            "source_document_sha256": source_document_hash(source),
            "source_evidence_ref": "evidence.jsonl#" + nid,
            "as_of": policy["as_of"], "exclude_news_ids": [nid],
            "candidate_limit": policy["candidates_per_query"], "related_limit": 5,
            "sampling_topic": topic, "sampling_topic_is_gold": False,
            "status": "draft", "human_reviewed": False, "review_decision": "pending",
            "event_group_id": None, "split": None, "expected_empty": None,
            "judgment_scope": "pooled", "proposed_group_id": groups[nid],
        })
        evidence_ids.add(nid)
        pool = candidate_pool(source, corpus, features, postings, policy, info)
        pool.sort(key=lambda r: digest([policy["seed"], "blind", qid, r["news_id"]]))
        for candidate in pool:
            other = by_id[candidate["news_id"]]
            evidence_ids.add(other["news_id"])
            pair_id = qid + ":" + other["news_id"]
            pairs.append({"pair_id": pair_id, "query_id": qid, "task": "related_news",
                          "source_news_id": nid, "news_id": other["news_id"],
                          "candidate_title": other["title"], "candidate_publish_time": other["publish_time"],
                          "evidence_ref": "evidence.jsonl#" + other["news_id"],
                          "evidence_sha256": other["record_sha256"], "as_of": policy["as_of"],
                          "judgment_status": "pending", "pool_status": "provisional_lexical_not_ab_union"})
            pool_audit.append({"pair_id": pair_id, **candidate})
    quality = quality_cases(inventory, policy)
    evidence_ids.update(r["news_id"] for r in quality)
    qa = qa_drafts(qa_seeds, by_id, info, policy)
    evidence_ids.update(r["source_news_id"] for r in qa)
    evidence = [{key: by_id[nid][key] for key in (
        "news_id", "release_id", "record_sha256", "body_sha256", "title", "body", "content_type",
        "content_state", "fragments", "url", "publish_time", "quality_flags", "selection_quality_flags", "provenance")}
        for nid in sorted(evidence_ids)]
    judgments = [{"pair_id": r["pair_id"], "query_id": r["query_id"], "news_id": r["news_id"],
                  "reviewer_id": None, "grade": None, "reason": None, "judgment_status": "pending"} for r in pairs]
    dataset = {
        "inventory.jsonl": inventory, "proposed-groups.jsonl": group_records,
        "duplicate-review-links.jsonl": links, "queries.review.jsonl": queries,
        "annotation.review.jsonl": pairs, "pool-audit.jsonl": pool_audit,
        "judgments-reviewer-a.jsonl": judgments, "judgments-reviewer-b.jsonl": [dict(r) for r in judgments],
        "quality.review.jsonl": quality, "qa.review.jsonl": qa, "evidence.jsonl": evidence,
        "retrieval-corpus-membership.jsonl": [
            {"news_id": r["news_id"], "record_sha256": r["record_sha256"], "release_id": r["release_id"],
             "as_of": policy["as_of"], "status": "candidate_membership_not_indexed"} for r in corpus],
    }
    validate_dataset(dataset)
    report = {
        "status": "draft_pending_human_review", "builder_version": VERSION,
        "policy_version": policy["version"], "source_count": len(rows),
        "content_states": dict(Counter(r["content_state"] for r in rows)),
        "body_lengths": dict(Counter(
            "none" if not r["body"] else "lt100" if len(r["body"].strip()) < 100 else
            "100_299" if len(r["body"].strip()) < 300 else
            "300_799" if len(r["body"].strip()) < 800 else "800_plus" for r in rows)),
        "source_candidates": sum(r["source_candidate"] for r in inventory),
        "corpus_candidates": len(corpus), "related_news_queries": len(queries),
        "review_pairs": len(pairs), "quality_cases": len(quality), "qa_drafts": len(qa),
        "topic_source_counts": dict(Counter(q["sampling_topic"] for q in queries)),
        "topic_candidate_counts": {
            t["id"]: sum(r["source_candidate"] and t["id"] in r["sampling_topic_hints"] for r in inventory)
            for t in policy["topics"]},
        "topic_source_pool_counts": {
            t["id"]: sum(r["source_candidate"] and not r["group_has_existing_eval_exposure"]
                         and source_topic_allowed(r, t) for r in inventory) for t in policy["topics"]},
        "age_bucket_counts": dict(Counter(info[q["source_news_id"]]["age_bucket"] for q in queries)),
        "groups_with_multiple_news": sum(len(g["news_ids"]) > 1 for g in group_records),
        "duplicate_review_links": len(links),
        "existing_eval_ids_in_inventory": sum(r["existing_eval_exposure"] for r in inventory),
        "existing_eval_group_quarantined": sum(r["group_has_existing_eval_exposure"] for r in inventory),
        "training_ready": 0, "frozen_test_queries": 0, "model_calls": 0, "index_writes": 0,
        "limitations": [
            "Title keywords are sampling hints, not official board labels or gold topics.",
            "Missing source boards prevent a verified health-board exclusion claim.",
            "Cached bodies are not fact-checked; fragments are not reconstructed into full text.",
            "Duplicate groups are incomplete event hypotheses; event_group_id/split remain null.",
            "Existing evaluation exposure is reserved by ID/group; semantic overlap review is still needed.",
            "Candidate pools are lexical/diverse previews, not real A/B retrieval outputs or final qrels.",
            "No relevance, quality or QA labels are approved; no training samples are export-ready.",
            "Age buckets are sampling strata, not production hot/warm/cold settings or measured benefits.",
        ],
    }
    return dataset, report


def validate_dataset(dataset):
    inventory = dataset["inventory.jsonl"]
    info = {r["news_id"]: r for r in inventory}
    if len(info) != len(inventory):
        raise ValueError("duplicate inventory identity")
    evidence = {r["news_id"]: r for r in dataset["evidence.jsonl"]}
    queries = {r["query_id"]: r for r in dataset["queries.review.jsonl"]}
    if len(evidence) != len(dataset["evidence.jsonl"]):
        raise ValueError("duplicate evidence")
    if len(queries) != len(dataset["queries.review.jsonl"]):
        raise ValueError("duplicate query")
    for row in dataset["evidence.jsonl"]:
        if normalize_record(row) != {key: row[key] for key in RECORD_KEYS}:
            raise ValueError("evidence record/hash mismatch")
        if row["record_sha256"] != info[row["news_id"]]["record_sha256"]:
            raise ValueError("evidence identity drift")
        if info[row["news_id"]]["normalized_body_sha256"] != body_fingerprint(row["body"]):
            raise ValueError("normalized body fingerprint drift")
        if row["body"] is not None and sha256(row["body"].encode("utf-8")).hexdigest() != row["body_sha256"]:
            raise ValueError("evidence body hash mismatch")
        if row["content_state"] != "body_available" and row["body"] is not None:
            raise ValueError("partial evidence cannot be a full body")
    for query in queries.values():
        nid = query["source_news_id"]
        if not info[nid]["source_candidate"] or info[nid]["group_has_existing_eval_exposure"] or nid not in evidence:
            raise ValueError("ineligible/exposed query source")
        if (query["human_reviewed"] or query["status"] != "draft"
                or query["split"] is not None or query["event_group_id"] is not None
                or query["expected_empty"] is not None):
            raise ValueError("candidate builder cannot promote queries")
        if (query["source_record_sha256"] != evidence[nid]["record_sha256"]
                or query["source_document_sha256"] != source_document_hash(evidence[nid])):
            raise ValueError("query evidence identity drift")
        if instant(evidence[nid]["publish_time"]) > instant(query["as_of"]):
            raise ValueError("future source evidence")
    seen_pairs = set()
    for row in dataset["annotation.review.jsonl"]:
        query, nid = queries[row["query_id"]], row["news_id"]
        if row["pair_id"] in seen_pairs or nid == query["source_news_id"]:
            raise ValueError("duplicate pair or self recall")
        if info[nid]["normalized_body_sha256"] == info[query["source_news_id"]]["normalized_body_sha256"]:
            raise ValueError("self recall through an identical body copy")
        seen_pairs.add(row["pair_id"])
        if nid not in evidence or not info[nid]["content_candidate"]:
            raise ValueError("candidate evidence missing/ineligible")
        if (row["evidence_sha256"] != evidence[nid]["record_sha256"]
                or row["source_news_id"] != query["source_news_id"]
                or row["as_of"] != query["as_of"] or row["judgment_status"] != "pending"):
            raise ValueError("candidate pair identity/status drift")
        if instant(evidence[nid]["publish_time"]) > instant(query["as_of"]):
            raise ValueError("future candidate evidence")
    for name in ("judgments-reviewer-a.jsonl", "judgments-reviewer-b.jsonl"):
        judgments = dataset[name]
        if len(judgments) != len(seen_pairs) or {r["pair_id"] for r in judgments} != seen_pairs:
            raise ValueError("judgment table does not cover candidate pool")
        if any(r["grade"] is not None or r["reviewer_id"] is not None
               or r["judgment_status"] != "pending" for r in judgments):
            raise ValueError("builder must not fabricate human judgments")
        pairs = {r["pair_id"]: r for r in dataset["annotation.review.jsonl"]}
        if any((r["query_id"], r["news_id"]) != (pairs[r["pair_id"]]["query_id"], pairs[r["pair_id"]]["news_id"])
               for r in judgments):
            raise ValueError("judgment identity drift")
    members = dataset["retrieval-corpus-membership.jsonl"]
    expected = {nid for nid, row in info.items() if row["content_candidate"]}
    if len(members) != len(expected) or {r["news_id"] for r in members} != expected:
        raise ValueError("corpus membership drift")
    if any(r["record_sha256"] != info[r["news_id"]]["record_sha256"] for r in members):
        raise ValueError("corpus identity drift")
    grouped = []
    for group in dataset["proposed-groups.jsonl"]:
        if group["event_group_id"] is not None or group["split"] is not None:
            raise ValueError("proposed groups cannot become approved events/splits")
        for nid in group["news_ids"]:
            if info[nid]["proposed_group_id"] != group["proposed_group_id"]:
                raise ValueError("group identity drift")
        grouped.extend(group["news_ids"])
    if len(grouped) != len(info) or set(grouped) != set(info):
        raise ValueError("groups must cover inventory exactly once")
    for row in dataset["quality.review.jsonl"]:
        if row["news_id"] not in evidence or row["record_sha256"] != evidence[row["news_id"]]["record_sha256"]:
            raise ValueError("quality evidence missing/drifted")
        if (row["status"] != "pending" or row["human_reviewed"] or row["split"] is not None
                or row["reviewer_id"] is not None or row["index_recommendation"] is not None):
            raise ValueError("builder cannot approve quality labels")
    for row in dataset["qa.review.jsonl"]:
        if (row["training_eligible"] or row["human_reviewed"] or row["split"] is not None
                or row["status"] != "draft" or row["reviewer_id"] is not None
                or row["event_group_id"] is not None):
            raise ValueError("unreviewed QA cannot become training data")
        if not row["evidence"]:
            raise ValueError("QA evidence missing")
        for span in row["evidence"]:
            source = evidence[span["news_id"]]
            body = source["body"]
            if (span["news_id"] != row["source_news_id"]
                    or span["record_sha256"] != source["record_sha256"]
                    or span["body_sha256"] != source["body_sha256"]):
                raise ValueError("QA evidence identity drift")
            if (not isinstance(body, str) or not 0 <= span["start"] < span["end"] <= len(body)
                    or body[span["start"]:span["end"]] != span["quote"]):
                raise ValueError("QA span mismatch")


def render_preview(dataset, report):
    """Static escaped reading aid; no scripts, remote assets or simulated reviews."""
    evidence = {r["news_id"]: r for r in dataset["evidence.jsonl"]}
    pairs = defaultdict(list)
    for row in dataset["annotation.review.jsonl"]:
        pairs[row["query_id"]].append(row)
    esc = lambda value: html.escape(str(value), quote=True)
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>库存新闻标注候选</title>',
             '<style>body{max-width:1100px;margin:40px auto;font:16px/1.7 system-ui;color:#172b3a;padding:0 24px}'
             'h1,h2{color:#064d55}pre{white-space:pre-wrap;background:#f1f5f7;padding:18px;overflow-wrap:anywhere}'
             'details{border:1px solid #cddce1;border-radius:8px;margin:14px 0;padding:14px}'
             'summary{cursor:pointer;font-weight:bold}small{color:#526772}</style>',
             '<h1>库存新闻 · 评测与训练标注候选</h1>',
             '<p>所有标签待审核。此页用于阅读，请在对应 JSONL 标注表填写意见。'
             '主题是抽样提示，重复组是候选；没有正式训练／测试划分。</p>',
             '<pre>' + esc(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre>',
             '<h2>基于证据的问答草稿</h2>']
    for row in dataset["qa.review.jsonl"]:
        parts += ['<details><summary>' + esc(row["case_id"] + ' · ' + row["question"]) + '</summary>',
                  '<p>候选答案（未审核）：' + esc(row["candidate_answer"]) + '</p>',
                  '<pre>' + esc("\n\n".join(s["quote"] for s in row["evidence"])) + '</pre></details>']
    parts.append('<h2>相关文章检索 · 独立双审</h2>')
    for query in dataset["queries.review.jsonl"]:
        source = evidence[query["source_news_id"]]
        parts += ['<details><summary>' + esc(query["sampling_topic"] + ' · ' + source["title"]) + '</summary>',
                  '<small>' + esc(query["query_id"]) + '</small><pre>' + esc(source["body"]) + '</pre>']
        for pair in pairs[query["query_id"]]:
            candidate = evidence[pair["news_id"]]
            parts += ['<details><summary>' + esc(candidate["title"]) + '</summary>',
                      '<small>' + esc(pair["pair_id"]) + '</small><pre>' + esc(candidate["body"]) + '</pre></details>']
        parts.append('</details>')
    parts.append('<h2>内容质量与边界样本</h2>')
    for row in dataset["quality.review.jsonl"]:
        source = evidence[row["news_id"]]
        parts += ['<details><summary>' + esc(row["stratum"] + ' · ' + (source["title"] or source["news_id"])) + '</summary>',
                  '<small>' + esc(row["case_id"]) + '</small><pre>',
                  esc(source["body"] if source["body"] is not None else
                      json.dumps({"title": source["title"], "fragments": source["fragments"]},
                                 ensure_ascii=False, indent=2)), '</pre></details>']
    return "\n".join(parts) + "</html>\n"


def publish(output, dataset, report, provenance):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("choose a new output directory; existing artifacts are immutable")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + output.name + ".building-", dir=output.parent) as tmp:
        staging, files = Path(tmp), {}
        for name, rows in dataset.items():
            path = staging / name
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(canonical_json(row) + "\n")
            files[name] = {"sha256": file_hash(path), "rows": len(rows), "bytes": path.stat().st_size}
        for name, text in (("report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                           ("review.html", render_preview(dataset, report))):
            path = staging / name
            path.write_text(text, encoding="utf-8", newline="\n")
            files[name] = {"sha256": file_hash(path), "bytes": path.stat().st_size}
        manifest = {"schema_version": 1, "builder_version": VERSION,
                    "status": "draft_pending_human_review", "files": files, "inputs": provenance}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        verify(staging)
        output.mkdir()
        for path in sorted(staging.iterdir(), key=lambda p: p.name == "manifest.json"):
            path.rename(output / path.name)
    return manifest


def verify(directory):
    directory = Path(directory)
    manifest = strict_json((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("builder_version") != VERSION:
        raise ValueError("review package builder version is unsupported")
    dataset = {}
    for name, expected in manifest["files"].items():
        if Path(name).name != name or name.startswith("."):
            raise ValueError("invalid artifact name")
        path = directory / name
        if path.is_symlink() or path.stat().st_size != expected["bytes"] or file_hash(path) != expected["sha256"]:
            raise ValueError(f"artifact hash/size mismatch: {name}")
        if name.endswith(".jsonl"):
            dataset[name] = read_jsonl(path)
            if len(dataset[name]) != expected["rows"]:
                raise ValueError("artifact row count mismatch")
    validate_dataset(dataset)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-package", type=Path, default=ROOT / "var/news-storage/unified-public-news-20261023")
    parser.add_argument("--export", type=Path, default=ROOT / "var/news-storage/exports/20261023/all-news-with-tags.jsonl")
    parser.add_argument("--export-manifest", type=Path, default=ROOT / "var/news-storage/exports/20261023/all-news-with-tags.manifest.json")
    parser.add_argument("--policy", type=Path, default=ROOT / "agent/evaluation/seeds/inventory_expansion_v2.json")
    parser.add_argument("--qa-seeds", type=Path)
    parser.add_argument("--existing-evaluation-root", type=Path, default=ROOT / "agent/evaluation/datasets")
    args = parser.parse_args()
    if args.command == "verify":
        result = verify(args.output_dir)
        print(json.dumps({"status": "verified_draft", "files": len(result["files"])}, ensure_ascii=False))
        return
    policy = strict_json(args.policy.read_text(encoding="utf-8"))
    rows, release = load_source(args.source_package, args.export, args.export_manifest)
    protected, existing = existing_evaluation_ids(args.existing_evaluation_root)
    seeds = read_jsonl(args.qa_seeds) if args.qa_seeds else []
    dataset, report = build(rows, policy, protected, seeds)
    background = ROOT / "项目背景.md"
    provenance = {
        "content_release": release, "source_package": str(args.source_package),
        "source_manifest_sha256": file_hash(args.source_package / "manifest.json"),
        "tagged_export": str(args.export), "tagged_export_sha256": file_hash(args.export),
        "policy": policy, "policy_sha256": file_hash(args.policy),
        "background_file": str(background), "background_sha256": file_hash(background),
        "builder_sha256": file_hash(Path(__file__)),
        "existing_evaluation_file_hashes": existing,
        "qa_seed_sha256": file_hash(args.qa_seeds) if args.qa_seeds else None,
    }
    publish(args.output_dir, dataset, report, provenance)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
