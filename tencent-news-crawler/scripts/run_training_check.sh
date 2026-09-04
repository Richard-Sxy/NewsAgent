#!/usr/bin/env bash

set -u

PROJECT_DIR="/home/shi/project/NewsAgent/tencent-news-crawler"
LOG_DIR="${PROJECT_DIR}/logs"
RUN_DATE="$(date +%F)"
LOG_FILE="${LOG_DIR}/training-${RUN_DATE}.log"

mkdir -p "${LOG_DIR}"
cd "${PROJECT_DIR}" || exit 1

echo "===== training check start $(date --iso-8601=seconds) =====" \
    >> "${LOG_FILE}"

"${PROJECT_DIR}/.venv/bin/python" \
    -m scripts.check_training_status \
    >> "${LOG_FILE}" 2>&1

EXIT_CODE=$?

echo "===== training check end, exit=${EXIT_CODE} =====" \
    >> "${LOG_FILE}"

exit "${EXIT_CODE}"
