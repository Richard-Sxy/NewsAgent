"""Audit a completed local BGE package's token window without loading a model.

Only tokenizer files are loaded, with remote access disabled. The audit reports
structural token truncation, not semantic loss or retrieval quality. It preserves
the package and writes only the explicitly selected JSON report.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
import platform
from typing import Iterable
from zoneinfo import ZoneInfo


CORPUS_RELATIVE = Path("data/experiments/offline_recall_fg/chunks.jsonl")
MODEL_RELATIVE = Path("data/models/bge-large-zh-v1.5")


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_counts(counts: Iterable[int], *, max_sequence_length: int) -> dict:
    values = sorted(counts)
    if max_sequence_length <= 0 or any(type(value) is not int or value < 0 for value in values):
        raise ValueError("token counts and max_sequence_length must be nonnegative/positive integers")
    total = sum(values)
    dropped = sum(max(0, value - max_sequence_length) for value in values)
    truncated = sum(value > max_sequence_length for value in values)

    def percentile(percent: int) -> int | None:
        return values[max(0, ceil(len(values) * percent / 100) - 1)] if values else None

    return {
        "chunks": len(values),
        "tokens_including_special_tokens": total,
        "min": values[0] if values else None,
        "p50_nearest_rank": percentile(50),
        "p90_nearest_rank": percentile(90),
        "p95_nearest_rank": percentile(95),
        "p99_nearest_rank": percentile(99),
        "max": values[-1] if values else None,
        "mean": total / len(values) if values else None,
        "chunks_exceeding_window": truncated,
        "chunks_exceeding_window_fraction": truncated / len(values) if values else None,
        "tokens_beyond_window": dropped,
        "tokens_beyond_window_fraction": dropped / total if total else None,
        "histogram": dict(Counter(
            "0-256" if value <= 256 else "257-512" if value <= 512
            else "513-768" if value <= 768 else "769-1024" if value <= 1024 else "1025+"
            for value in values
        )),
    }


def audit_rows(rows: list[dict], *, tokenizer, base_chunks: int,
               max_sequence_length: int, batch_size: int = 256) -> dict:
    if not 0 <= base_chunks <= len(rows) or not 1 <= batch_size <= 4096:
        raise ValueError("invalid base boundary or tokenizer batch size")
    if tokenizer.truncation_side != "right":
        raise ValueError("this audit requires the builder's right-side truncation policy")
    counts: list[int] = []
    # Check that the configured limit is applied by this exact local tokenizer,
    # rather than assuming a character-to-token ratio or a special-token count.
    for offset in range(0, len(rows), batch_size):
        texts = [row["text"] for row in rows[offset:offset + batch_size]]
        common = dict(add_special_tokens=True, return_attention_mask=False,
                      return_token_type_ids=False)
        complete = tokenizer(texts, truncation=False, **common)["input_ids"]
        bounded = tokenizer(texts, truncation=True, max_length=max_sequence_length,
                            **common)["input_ids"]
        if len(complete) != len(texts) or len(bounded) != len(texts):
            raise ValueError("tokenizer response row count differs")
        lengths = [len(tokens) for tokens in complete]
        if any(len(actual) != min(length, max_sequence_length)
               for length, actual in zip(lengths, bounded, strict=True)):
            raise ValueError("tokenizer did not apply the expected single-sequence limit")
        counts.extend(lengths)
    output = {}
    for name, first, last in (("all", 0, len(rows)), ("inherited_base", 0, base_chunks),
                              ("new_in_this_package", base_chunks, len(rows))):
        scope_rows = rows[first:last]
        output[name] = {
            "row_range_half_open": [first, last],
            "articles": len({row["news_id"] for row in scope_rows}),
            **summarize_counts(counts[first:last], max_sequence_length=max_sequence_length),
        }
    return output


def uncovered_intervals(length: int, covered: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Complement of a union of half-open character spans, including overlaps."""
    cursor, gaps = 0, []
    for start, end in sorted(covered):
        if not 0 <= start <= end <= length:
            raise ValueError("coverage interval exceeds the canonical body")
        if start == end:
            continue
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps


def length_distribution(values: Iterable[int]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered), "sum": sum(ordered),
        **{f"p{percent}_nearest_rank": ordered[max(0, ceil(len(ordered) * percent / 100) - 1)]
           if ordered else None for percent in (50, 95)},
        "max": ordered[-1] if ordered else None,
    }


def audit_body_coverage(new_rows: list[dict], *, build: dict, tokenizer,
                        max_sequence_length: int, batch_size: int) -> dict:
    """Map actual truncated token offsets to original deterministic body spans."""
    roster = {article["news_id"]: article for article in build["input"]["articles"]}
    source_root = Path(build["input"]["manifest_path"]).parent
    chunk_chars, overlap = build["chunking"]["body_chars"], build["chunking"]["overlap_chars"]
    step = chunk_chars - overlap
    if step <= 0 or not tokenizer.is_fast or tokenizer.truncation_side != "right":
        raise ValueError("coverage requires valid slicing and a fast right-truncating tokenizer")
    documents, definitions, indices = {}, [], {}
    for row in new_rows:
        news_id, index = row["news_id"], row["chunk_index"]
        if news_id not in documents:
            article_record = roster[news_id]
            path = source_root / article_record["relative_path"]
            raw = path.read_bytes()
            if sha256(raw).hexdigest() != article_record["sha256"]:
                raise ValueError("source article file differs from build input")
            article = json.loads(raw)
            document_digest = sha256(f"{article['title']}\x1f{article['content']}".encode("utf-8")).hexdigest()
            if article["news_id"] != news_id or document_digest != article_record["document_sha256"]:
                raise ValueError("source article identity/body differs from build input")
            body = article["content"].strip()
            published = datetime.fromisoformat(article["publish_time"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError("source article publication must be timezone-aware")
            published_text = published.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
            header = (f"# {article['title'].strip()}\n来源地址: {article['url']}\n"
                      f"发布时间: {published_text}\n\n")
            documents[news_id] = {"body": body, "header": header, "covered": [],
                                  "raw_body_chars": len(article["content"]),
                                  "document_sha256": document_digest,
                                  "content_version": article["content_version"]}
            indices[news_id] = []
        document = documents[news_id]
        if type(index) is not int or index < 0:
            raise ValueError("chunk index must be a nonnegative integer")
        start = index * step
        part = document["body"][start:start + chunk_chars]
        if (not part or row["text"] != document["header"] + part
                or row["document_sha256"] != document["document_sha256"]
                or row["content_version"] != document["content_version"]):
            raise ValueError("published chunk does not exactly match the source metadata/body slice")
        indices[news_id].append(index)
        definitions.append((news_id, start, len(part), len(document["header"])))
    for news_id, document in documents.items():
        expected_chunks = 1 + max(0, ceil((len(document["body"]) - chunk_chars) / step))
        if sorted(indices[news_id]) != list(range(expected_chunks)):
            raise ValueError("source article chunk indices are duplicated or incomplete")
    for offset in range(0, len(new_rows), batch_size):
        texts = [row["text"] for row in new_rows[offset:offset + batch_size]]
        common = dict(add_special_tokens=True, return_attention_mask=False,
                      return_token_type_ids=False)
        complete = tokenizer(texts, truncation=False, **common)["input_ids"]
        bounded = tokenizer(texts, truncation=True, max_length=max_sequence_length,
                            return_offsets_mapping=True, **common)
        batch_definitions = definitions[offset:offset + batch_size]
        if not len(complete) == len(bounded["offset_mapping"]) == len(batch_definitions):
            raise ValueError("coverage tokenizer row count differs")
        for tokens, offsets, definition in zip(complete, bounded["offset_mapping"], batch_definitions, strict=True):
            news_id, start, part_chars, header_chars = definition
            # Fully fitting input was not truncated: count its whole original
            # character span, including whitespace normalized by the tokenizer.
            # Otherwise map the last retained non-special token's source offset.
            end_of_input = max((end for begin, end in offsets if end > begin), default=0)
            visible_chars = part_chars if len(tokens) <= max_sequence_length else max(
                0, min(part_chars, end_of_input - header_chars))
            if visible_chars:
                documents[news_id]["covered"].append((start, start + visible_chars))
    article_missing, gap_lengths, top_gaps, affected = [], [], [], 0
    total_chars = total_missing = stripped_chars = 0
    gap_distribution_by_article = []
    for news_id, document in sorted(documents.items()):
        body_length = len(document["body"])
        gaps = uncovered_intervals(body_length, document["covered"])
        missing = sum(end - start for start, end in gaps)
        total_chars += body_length
        stripped_chars += document["raw_body_chars"] - body_length
        total_missing += missing
        article_missing.append(missing)
        if gaps:
            affected += 1
            gap_distribution_by_article.append({"news_id": news_id, "body_characters": body_length,
                "uncovered_characters": missing, "gaps_half_open": gaps})
        for start, end in gaps:
            gap_lengths.append(end - start)
            top_gaps.append({"news_id": news_id, "start": start, "end": end, "characters": end - start})
    return {
        "articles": len(documents), "chunks": len(new_rows),
        "source_article_hash_and_exact_header_body_slices_verified": True,
        "articles_with_uncovered_body_intervals": affected,
        "articles_with_uncovered_body_intervals_fraction": affected / len(documents) if documents else None,
        "canonical_body_characters": total_chars,
        "outer_whitespace_characters_removed_by_split_text_strip": stripped_chars,
        "covered_body_characters": total_chars - total_missing,
        "uncovered_body_characters": total_missing,
        "body_character_coverage_fraction": (total_chars - total_missing) / total_chars if total_chars else None,
        "uncovered_characters_per_article_all": length_distribution(article_missing),
        "uncovered_characters_per_affected_article": length_distribution(value for value in article_missing if value),
        "individual_gap_characters": length_distribution(gap_lengths),
        "largest_gaps": sorted(top_gaps, key=lambda item: (-item["characters"], item["news_id"], item["start"]))[:20],
        "affected_articles": gap_distribution_by_article,
        "definition": [
            "Only newly added rows in this package are audited against their source articles.",
            "Canonical body is original article content.strip(), exactly as split_text; all indices count Python Unicode code points, not bytes or tokens.",
            "Chunk source start is chunk_index*(800-80); the exact published header+slice and every expected chunk index are verified.",
            "For truncated input, source span ends at the last retained token offset after removing metadata-header length; internal whitespace in that prefix is included.",
            "For untruncated input, the whole source slice is included, including whitespace normalized by the tokenizer.",
            "Coverage unions all retained half-open body spans for each article before finding gaps, so 80-character overlaps may restore coverage.",
            "Character coverage measures structural input inclusion, not semantic preservation or retrieval quality.",
        ],
    }


def audit_package(package: Path, *, tokenizer=None, batch_size: int = 256) -> dict:
    package = package.resolve()
    report_path, corpus_path = package / "build_report.json", package / CORPUS_RELATIVE
    build = json.loads(report_path.read_text(encoding="utf-8"))
    if build.get("schema_version") != 1 or build.get("status") != "completed":
        raise ValueError("audit requires a completed schema_version=1 build report")
    model_dir = package / MODEL_RELATIVE
    base_corpus = Path(build["base_package"]) / CORPUS_RELATIVE
    # Verify exact inherited byte-prefix identity without modifying either input.
    prefix_digest = sha256()
    with base_corpus.open("rb") as base, corpus_path.open("rb") as current:
        for block in iter(lambda: base.read(1024 * 1024), b""):
            if current.read(len(block)) != block:
                raise ValueError("current corpus does not preserve the inherited byte prefix")
            prefix_digest.update(block)
    rows = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != build["total_chunks"] or len(rows) - build["base_chunks"] != build["added_chunks"]:
        raise ValueError("corpus row counts differ from the build report")
    if tokenizer is None:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True,
                                                  trust_remote_code=False)
    limit = build["embedding_inference"]["max_sequence_length"]
    scopes = audit_rows(rows, tokenizer=tokenizer, base_chunks=build["base_chunks"],
                        max_sequence_length=limit, batch_size=batch_size)
    coverage = audit_body_coverage(rows[build["base_chunks"]:], build=build, tokenizer=tokenizer,
                                   max_sequence_length=limit, batch_size=batch_size)
    tokenizer_files = {path.name: file_sha256(path) for path in sorted(model_dir.iterdir())
                       if path.is_file() and path.name in {
                           "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"}}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package": str(package),
        "snapshot_version": build["snapshot_version"],
        "package_manifest_sha256": build["package_manifest_sha256"],
        "build_report_sha256": file_sha256(report_path),
        "corpus_sha256": file_sha256(corpus_path),
        "inherited_base_corpus_sha256": prefix_digest.hexdigest(),
        "inherited_byte_prefix_verified": True,
        "tokenizer": {"class": type(tokenizer).__name__, "is_fast": tokenizer.is_fast,
                      "truncation_side": tokenizer.truncation_side,
                      "special_tokens_per_single_sequence": tokenizer.num_special_tokens_to_add(pair=False),
                      "file_sha256": tokenizer_files},
        "max_sequence_length_including_special_tokens": limit,
        "runtime": {"python": platform.python_version(), "transformers": version("transformers"),
                    "tokenizers": version("tokenizers")},
        "scope": scopes,
        "new_article_body_coverage": coverage,
        "method": {
            "network": "local_files_only=True; trust_remote_code=False; no model weights loaded",
            "input": "Exact published JSONL text, including repeated metadata header",
            "counts": "Full local tokenizer IDs including special tokens; checked against actual right-truncated encoding",
            "beyond_window_fraction_denominator": "All untruncated tokens including special tokens within the same scope",
            "percentiles": "Nearest rank, ceil(n*p/100)",
        },
        "limitations": [
            "Token truncation is structural input exclusion, not a semantic-loss percentage.",
            "Inherited rows are measured against the current 512-token policy; original build truncation cannot be proven from this audit.",
            "This does not load an embedding model, measure FP16/FP32 drift, or evaluate retrieval relevance.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()
    package, output = args.package.resolve(), args.output.resolve()
    if output.is_relative_to(package):
        parser.error("audit output must not be written inside the package")
    report = audit_package(package, batch_size=args.batch_size)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"output": str(output), "scope": report["scope"],
        "new_article_body_coverage": {key: value for key, value in report["new_article_body_coverage"].items()
                                      if key not in {"affected_articles", "largest_gaps", "definition"}}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
