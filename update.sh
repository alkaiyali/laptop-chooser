#!/usr/bin/env bash
# Refresh the laptop snapshot, but only if it is older than TTL_HOURS (default 24).
# Every cached page older than the TTL is re-fetched; fresh ones are reused,
# so a re-run right after another one makes no network requests at all.
#
#   ./update.sh            # refresh only if snapshot is stale
#   ./update.sh --force    # always refresh
#   TTL_HOURS=48 ./update.sh
set -euo pipefail
cd "$(dirname "$0")"

TTL_HOURS="${TTL_HOURS:-24}"
FORCE=0
for arg in "$@"; do
  [ "$arg" = "--force" ] && FORCE=1
done

snapshot_mtime() {
  local f="$1"
  [ -f "$f" ] || { echo 0; return; }
  stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0
}

if [ "$FORCE" -eq 0 ]; then
  now=$(date +%s)
  then=$(snapshot_mtime data/unified_laptops.json)
  if [ "$then" -gt 0 ] && [ $((now - then)) -lt "$((TTL_HOURS * 3600))" ]; then
    hours=$(( (now - then) / 3600 ))
    echo "[update] snapshot is fresh (${hours}h old, TTL ${TTL_HOURS}h) — nothing to do"
    exit 0
  fi
fi

echo "[update] refreshing (cache max age ${TTL_HOURS}h)…"
python3 scraper/jarir_scraper.py --max-age "$TTL_HOURS"
python3 scraper/extra_scraper.py --max-age "$TTL_HOURS"
python3 scraper/datablitz_scraper.py --max-age "$TTL_HOURS"
python3 scraper/pcx_scraper.py --max-age "$TTL_HOURS"
python3 scraper/gigahertz_scraper.py --max-age "$TTL_HOURS"
python3 scraper/noon_scraper.py --max-age "$TTL_HOURS"
python3 scraper/normalize.py
echo "[update] done"
