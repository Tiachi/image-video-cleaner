#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo '[text-qa-clean] FAIL execute; do not source' >&2
  return 2
fi
set -Eeuo pipefail
trap 'echo "[text-qa-clean] FAIL status=$? line=${LINENO}; upstream/data/media unchanged; completed parts retained" >&2' ERR
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
echo '[text-qa-clean] READ Stage01 accepted text + verified audit/receipts; call configured vLLM API directly, no proxy; no media reads or CoT generation.'
echo '[text-qa-clean] WRITE separate Stage02 parts/audit/statistics only; no automatic retries or original modifications.'
"${OPD_PYTHON:-python3}" -B -u "${here}/text_qa_clean.py" "$@"
echo '[text-qa-clean] PASS command; inspect candidate/rejected/review counts; text labels do not certify media truth.'
