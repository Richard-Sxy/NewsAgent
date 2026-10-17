#!/usr/bin/env bash
# Read-only local baseline for the later tiered-recall experiment.
# Usage: bash /absolute/path/run_baseline.sh [smoke|full] [new-report.json]
# Upstream: pinned immutable v2 BGE package; no network or .env loading.
# Core: existing pytest modules, policy demo, evaluate_bge_ann.py.
# Output: a new ANN JSON report outside the immutable package.
# Dependencies: Python 3.11, pytest, NumPy, hnswlib, threadpoolctl and agent deps.
# Config: NEWSAGENT_TEST_PYTHON, mode, report path; hashes are pinned below.
# TODO: this is a single-layer baseline, not a dynamic three-tier benchmark.
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
task_python="${NEWSAGENT_TEST_PYTHON:-python3.11}"
task_mode="${1:-smoke}"
if [[ "$task_mode" != smoke && "$task_mode" != full ]]; then
  printf '%s\n' 'usage: run_baseline.sh [smoke|full] [new-report.json]' >&2
  exit 2
fi
task_report="${2:-$task_root/output/tiered-recall-test-guide-2026-10-15/baseline-$task_mode-$(date +%Y%m%dT%H%M%S)-$$.json}"
if [[ -e "$task_report" ]]; then
  printf 'Report already exists; choose a new path: %s\n' "$task_report" >&2
  exit 2
fi

"$task_python" -c 'import pytest, numpy, hnswlib, threadpoolctl; print("Local test dependencies ready")'
(
  cd "$task_root/agent"
  "$task_python" -m pytest -o addopts='' -q \
    tests/test_tiered_vector_retrieval.py \
    tests/test_native_knowledge_indexing.py \
    tests/test_milvus_vector_index.py \
    tests/test_bge_snapshot_index.py
)
"$task_python" "$task_root/news/test/vector_tier_policy.py"

if [[ "$task_mode" == smoke ]]; then
  task_eval_args=(--sample-size 24 --ks 10 --efs 256 --warmup-queries 4 --bootstrap-resamples 200)
else
  task_eval_args=(--sample-size 96 --ks 3 10 20 --efs 256 512 --warmup-queries 8 --bootstrap-resamples 2000)
fi
"$task_python" "$task_root/agent/tools/evaluate_bge_ann.py" \
  --package "$task_root/agent/data/news-bge-incremental-20261014-v2" \
  --manifest-sha256 c59a3095657711fb0213b66bf7e7e775c63840961cbad4ba96fdbb9de6bd9138 \
  --base-manifest-sha256 edf9d5f6cd9b05f8d1c5eeece8c47424bcfdeaf6d569b02f4d5619a042b27733 \
  --snapshot-version news-bge-incremental-20261014-v2 \
  "${task_eval_args[@]}" --output "$task_report"
printf 'Baseline complete. New report: %s\n' "$task_report"
printf '%s\n' 'Scope: single-layer, source-vector ANN approximation and warm-cache timing; not semantic quality, HTTP or tiered freshness.'
