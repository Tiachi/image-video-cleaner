#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo '[mechanical-clean] FAIL execute this script; do not source' >&2
  return 2
fi
set -Eeuo pipefail
trap 'echo "[mechanical-clean] FAIL status=$? line=${LINENO}; inputs unchanged; completed parts retained" >&2' ERR
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
echo '[mechanical-clean] READ exact configured JSONL and referenced media only; no directory/media-tree traversal.'
echo '[mechanical-clean] Decode still images and8 frames/video clip at native resolution; no model/NPU.'
echo '[mechanical-clean] WRITE external output root only; original rows/media unchanged; resumable part receipts.'
export PYTHONDONTWRITEBYTECODE=1
"${OPD_PYTHON:-python3}" -B -u "${here}/mechanical_clean.py" "$@"
echo '[mechanical-clean] PASS command; accepted means mechanical candidate only; review is excluded from accepted.'
