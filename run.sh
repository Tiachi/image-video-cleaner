#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo '[image-video-cleaner] FAIL execute, do not source' >&2
  return 2
fi
set -Eeuo pipefail
trap 'echo "[image-video-cleaner] FAIL status=$? line=${LINENO}; originals unchanged" >&2' ERR
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command=${1:-help}
if [[ $# -gt 0 ]]; then shift; fi
case "$command" in
  stage01|stage02|stage03) bash "$root/scripts/run_${command}.sh" "$@" ;;
  vllm) PYTHONDONTWRITEBYTECODE=1 "${OPD_PYTHON:-python3}" -B -u "$root/scripts/vllm_service.py" "$@" ;;
  test) PYTHONDONTWRITEBYTECODE=1 "${OPD_PYTHON:-python3}" -B -m unittest discover -s "$root/tests" -v ;;
  help) echo 'bash run.sh stage01|stage02|stage03 init-config|pilot|run ... ; stage03 cache ; vllm print|serve --config ... ; test' ;;
  *) echo '[image-video-cleaner] FAIL unknown command' >&2; exit 2 ;;
esac
echo '[image-video-cleaner] PASS requested command'
