#!/usr/bin/env bash

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
RUN_DATE="$(date +%F)"
LOG_FILE="${LOG_DIR}/ingest-${RUN_DATE}.log"

mkdir -p "${LOG_DIR}"
cd "${PROJECT_DIR}" || exit 1

echo "===== start $(date --iso-8601=seconds) =====" \
    >> "${LOG_FILE}"

echo "===== news ingest start =====" \
    >> "${LOG_FILE}"

"${PROJECT_DIR}/.venv/bin/python" \
    -m scripts.ingest_daily \
    >> "${LOG_FILE}" 2>&1

INGEST_EXIT_CODE=$?

echo "===== news ingest end, exit=${INGEST_EXIT_CODE} =====" \
    >> "${LOG_FILE}"

echo "===== qa generate start =====" \
    >> "${LOG_FILE}"

"${PROJECT_DIR}/.venv/bin/python" \
    -m scripts.generate_qa \
    >> "${LOG_FILE}" 2>&1

QA_EXIT_CODE=$?

echo "===== qa generate end, exit=${QA_EXIT_CODE} =====" \
    >> "${LOG_FILE}"

echo "===== daily report start =====" \
    >> "${LOG_FILE}"

"${PROJECT_DIR}/.venv/bin/python" \
    -m scripts.generate_daily_report \
    --date "${RUN_DATE}" \
    --ingest-exit-code "${INGEST_EXIT_CODE}" \
    --qa-exit-code "${QA_EXIT_CODE}" \
    >> "${LOG_FILE}" 2>&1

REPORT_EXIT_CODE=$?

echo "===== daily report end, exit=${REPORT_EXIT_CODE} =====" \
    >> "${LOG_FILE}"

if [ "${INGEST_EXIT_CODE}" -ne 0 ] \
    || [ "${QA_EXIT_CODE}" -ne 0 ] \
    || [ "${REPORT_EXIT_CODE}" -ne 0 ]; then
    EXIT_CODE=1
else
    EXIT_CODE=0
fi

echo "===== end $(date --iso-8601=seconds), exit=${EXIT_CODE} =====" \
    >> "${LOG_FILE}"

exit "${EXIT_CODE}"
