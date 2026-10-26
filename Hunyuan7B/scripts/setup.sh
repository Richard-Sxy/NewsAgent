#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
TASK_STATE="$TASK_ROOT/small_model/runtime/hunyuan"
TASK_UV="$TASK_STATE/bootstrap/uv"
export UV_CACHE_DIR="$TASK_STATE/uv-cache"
export UV_PYTHON_INSTALL_DIR="$TASK_STATE/python"
export UV_HTTP_TIMEOUT=120
export UV_NO_PROGRESS=1

python3 "$TASK_ROOT/small_model/scripts/bootstrap_uv.py"
"$TASK_UV" --no-config python install 3.11.14 --no-bin --install-dir "$UV_PYTHON_INSTALL_DIR"
if [[ ! -e "$TASK_ROOT/small_model/environments/hunyuan" ]]; then
  "$TASK_UV" --no-config venv --python 3.11.14 "$TASK_ROOT/small_model/environments/hunyuan"
else
  "$TASK_ROOT/small_model/environments/hunyuan/bin/python" -c \
    'import sys; assert sys.version_info[:3] == (3, 11, 14), "Unexpected existing Python environment"'
fi
"$TASK_UV" --no-config pip install --python "$TASK_ROOT/small_model/environments/hunyuan/bin/python" \
  --index-url https://pypi.org/simple -r "$TASK_ROOT/small_model/scripts/torch-cu128-linux.txt"
"$TASK_UV" --no-config pip install --python "$TASK_ROOT/small_model/environments/hunyuan/bin/python" \
  --index-url https://pypi.org/simple -r "$TASK_ROOT/small_model/scripts/requirements.txt"
"$TASK_UV" --no-config pip check --python "$TASK_ROOT/small_model/environments/hunyuan/bin/python"
"$TASK_UV" --no-config pip freeze --python "$TASK_ROOT/small_model/environments/hunyuan/bin/python" \
  > "$TASK_STATE/installed-packages.txt"
printf 'Environment ready: %s\n' "$TASK_ROOT/small_model/environments/hunyuan/bin/python"
printf 'Import weights separately with small_model/scripts/assets.py --import-from PATH\n'
