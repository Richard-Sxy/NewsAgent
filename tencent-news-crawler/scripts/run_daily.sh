#!/usr/bin/env bash

set -u

PROJECT_DIR="/home/shi/project/NewsAgent/tencent-news-crawler"
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

EVALUATION_ENABLED="$(
    "${PROJECT_DIR}/.venv/bin/python" -c \
    'from config import settings; print(str(settings.daily_evaluation_enabled).lower())'
)"

if [ "${EVALUATION_ENABLED}" = "true" ]; then
    echo "===== evaluation start =====" >> "${LOG_FILE}"
    "${PROJECT_DIR}/.venv/bin/python" \
        -m scripts.evaluate_qa \
        --questions "$(
            "${PROJECT_DIR}/.venv/bin/python" -c \
            'from config import settings; print(settings.evaluation_questions_path)'
        )" \
        --output "reports/evaluation-${RUN_DATE}.json" \
        --limit "$(
            "${PROJECT_DIR}/.venv/bin/python" -c \
            'from config import settings; print(settings.daily_evaluation_limit)'
        )" \
        >> "${LOG_FILE}" 2>&1
    EVALUATION_EXIT_CODE=$?
    echo "===== evaluation end, exit=${EVALUATION_EXIT_CODE} =====" \
        >> "${LOG_FILE}"
else
    EVALUATION_EXIT_CODE=0
    echo "===== evaluation skipped (disabled) =====" >> "${LOG_FILE}"
fi

echo "===== daily report start =====" \
    >> "${LOG_FILE}"

"${PROJECT_DIR}/.venv/bin/python" \
    -m scripts.generate_daily_report \
    --date "${RUN_DATE}" \
    --ingest-exit-code "${INGEST_EXIT_CODE}" \
    --qa-exit-code "${QA_EXIT_CODE}" \
    --evaluation-exit-code "${EVALUATION_EXIT_CODE}" \
    >> "${LOG_FILE}" 2>&1

REPORT_EXIT_CODE=$?

echo "===== daily report end, exit=${REPORT_EXIT_CODE} =====" \
    >> "${LOG_FILE}"

if [ "${INGEST_EXIT_CODE}" -ne 0 ] \
    || [ "${QA_EXIT_CODE}" -ne 0 ] \
    || [ "${EVALUATION_EXIT_CODE}" -ne 0 ] \
    || [ "${REPORT_EXIT_CODE}" -ne 0 ]; then
    EXIT_CODE=1
else
    EXIT_CODE=0
fi

echo "===== end $(date --iso-8601=seconds), exit=${EXIT_CODE} =====" \
    >> "${LOG_FILE}"

exit "${EXIT_CODE}"
