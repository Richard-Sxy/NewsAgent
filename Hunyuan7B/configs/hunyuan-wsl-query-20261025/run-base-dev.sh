#!/usr/bin/env bash
set -euo pipefail
cd /home/shi/project/NewsAgent

query_eval_python="$PWD/small_model/environments/query-eval/venv-20261025/bin/python"
query_eval_package="$PWD/small_model/packages/query-semantics-dev-wsl-20261025-v2"
query_eval_endpoint="$PWD/small_model/configs/hunyuan-wsl-query-20261025/base.http.json"

# This unauthenticated loopback service only needs a dummy token for the HTTP client.
export HUNYUAN_LOCAL_EVAL_TOKEN=local-only-no-auth
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

"$query_eval_python" -B small_model/evaluation/run_query_semantics.py validate --package "$query_eval_package"

# Check service identity before creating an experiment; never start or load a model.
"$query_eval_python" -B - <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:8008/health", timeout=5) as response:
        health = json.load(response)
    expected = {
        "status": "ok", "platform": "linux",
        "model": "hunyuan-7b-instruct-6fd6ecb-nf4",
        "revision": "6fd6ecb05e76589bc43b79f49e3619445c6b4593",
        "model_path": "/home/shi/project/NewsAgent/small_model/models/Hunyuan-7B-Instruct",
        "quantization": "nf4-double-quant",
        "context_tokens": 2048,
    }
    if any(health.get(key) != value for key, value in expected.items()):
        raise ValueError("service_identity_mismatch")
except Exception as error:
    print(json.dumps({"status": "blocked", "stage": "service_health", "error_type": type(error).__name__,
                      "hint": "Expected WSL Hunyuan service is not ready or its identity changed. No evaluation requests were sent."}))
    sys.exit(2)
print(json.dumps({"service_health": "passed", "scope": "development"}))
PY

query_eval_run="$PWD/small_model/runs/hunyuan-base-dev-$(date -u +%Y%m%dT%H%M%SZ)-$$"
"$query_eval_python" -B small_model/evaluation/run_query_semantics.py run \
  --package "$query_eval_package" --endpoint "$query_eval_endpoint" \
  --out "$query_eval_run" --max-calls 24 --max-seconds 3600

"$query_eval_python" -B small_model/evaluation/run_query_semantics.py score \
  --package "$query_eval_package" --run "$query_eval_run" --out "${query_eval_run}-score"

printf 'Predictions: %s\nScores: %s\n' "$query_eval_run" "${query_eval_run}-score"
