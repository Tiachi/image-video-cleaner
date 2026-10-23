#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo '[media-qa-clean] FAIL execute; do not source' >&2
  return 2
fi
set -Eeuo pipefail
trap 'echo "[media-qa-clean] FAIL status=$? line=${LINENO}; originals unchanged; frame cache and completed parts retained" >&2' ERR
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
echo '[media-qa-clean] READ Stage02 accepted QA + verified ancestry + exact referenced images/video clips; native sampled visual frame cache.'
echo '[media-qa-clean] MODEL already-started local vision vLLM only; no automatic retries, startup, installation or external API.'
echo '[media-qa-clean] WRITE separate Stage03 results and bounded PNG frame cache; four integers per QA, originals unchanged.'
"${OPD_PYTHON:-python3}" -B -u "${here}/media_qa_clean.py" "$@"
echo '[media-qa-clean] PASS command; inspect review/rejection counts; sparse visual evidence does not certify whole-video truth.'
