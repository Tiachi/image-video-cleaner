#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo '[iv-sync] FAIL execute, do not source' >&2
  return 2
fi
set -Eeuo pipefail
trap 'echo "[iv-sync] FAIL status=$? line=${LINENO}; existing work preserved" >&2' ERR
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
url=$(git remote get-url origin)
case "$url" in
  https://github.com/Tiachi/image-video-cleaner|https://github.com/Tiachi/image-video-cleaner.git|git@github.com:Tiachi/image-video-cleaner|git@github.com:Tiachi/image-video-cleaner.git|ssh://git@ssh.github.com:443/Tiachi/image-video-cleaner|ssh://git@ssh.github.com:443/Tiachi/image-video-cleaner.git) ;;
  *) echo '[iv-sync] FAIL unexpected origin' >&2; exit 2 ;;
esac
[[ -z "$(git status --porcelain)" ]] || { echo '[iv-sync] FAIL dirty checkout; no stash/reset/clean' >&2; exit 2; }
[[ "$(git branch --show-current)" == main ]] || { echo '[iv-sync] FAIL expected main' >&2; exit 2; }
# Inspect key names only; do not print proxy credentials or change persistent settings.
mapfile -t proxy_keys < <(git config --name-only --get-regexp '(^http\.proxy$|^http\..*\.proxy$)' || true)
git_args=(-c http.proxy= -c credential.interactive=false)
for key in "${proxy_keys[@]}"; do git_args+=(-c "${key}="); done
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git "${git_args[@]}" fetch origin main
git merge --ff-only origin/main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo '[iv-sync] FAIL local main differs; no push/reset' >&2; exit 2; }
echo "[iv-sync] PASS main=$(git rev-parse HEAD); proxy settings preserved"
