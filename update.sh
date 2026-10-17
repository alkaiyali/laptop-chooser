#!/usr/bin/env bash
# Refresh the laptop snapshot, but only if it is older than TTL_HOURS (default 24).
# Every cached page older than the TTL is re-fetched; fresh ones are reused,
# so a re-run right after another one makes no network requests at all.
#
#   ./update.sh            # refresh only if snapshot is stale
#   ./update.sh --force    # always refresh
#   TTL_HOURS=48 ./update.sh
#   AUTO_DEPLOY=1 ./update.sh   # ...then rebuild + wrangler deploy (logs/deploy.log)
set -euo pipefail
cd "$(dirname "$0")"

# Single-flight: cron (every 4h) and manual runs (pm2-manager "SCRAPE NOW")
# must never overlap — scrapers share jsonl/db outputs that a concurrent
# normalize would read half-written. Second runner exits quietly (code 0 so
# cron doesn't mail).
exec 9> .update.lock
if ! flock -n 9; then
  echo "[update] another refresh is already running — skipping"
  exit 0
fi

TTL_HOURS="${TTL_HOURS:-24}"
# TTL comes from the environment (and from app.py as a float like "24.0"):
# accept only digits with an optional decimal part, then use the integer part
# for bash arithmetic and the scrapers. Anything else exits instead of running
# `$((...))` on untrusted input.
case "$TTL_HOURS" in
  ''|*[!0-9.]*|*.*.*)
    echo "[update] invalid TTL_HOURS: $TTL_HOURS (must be a number)" >&2
    exit 1
    ;;
esac
TTL_INT="${TTL_HOURS%%.*}"
case "$TTL_INT" in
  '') TTL_INT=0 ;;
esac
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
  then_phones=$(snapshot_mtime data/unified_phones.json)
  # refresh when either catalog is stale (phones piggyback the laptop cron)
  if [ "$then" -gt 0 ] && [ "$then_phones" -gt 0 ] \
     && [ $((now - then)) -lt "$((TTL_INT * 3600))" ] \
     && [ $((now - then_phones)) -lt "$((TTL_INT * 3600))" ]; then
    hours=$(( (now - then) / 3600 ))
    echo "[update] snapshot is fresh (${hours}h old, TTL ${TTL_INT}h) — nothing to do"
    exit 0
  fi
fi

echo "[update] refreshing (cache max age ${TTL_INT}h)…"
python3 scraper/jarir_scraper.py --max-age "$TTL_INT"
python3 scraper/extra_scraper.py --max-age "$TTL_INT"
python3 scraper/datablitz_scraper.py --max-age "$TTL_INT"
python3 scraper/pcx_scraper.py --max-age "$TTL_INT"
python3 scraper/gigahertz_scraper.py --max-age "$TTL_INT"
python3 scraper/noon_scraper.py --max-age "$TTL_INT"
python3 scraper/normalize.py
python3 scraper/jarir_phones_scraper.py --max-age "$TTL_INT"
python3 scraper/extra_phones_scraper.py --max-age "$TTL_INT"
python3 scraper/greentelcom_phones_scraper.py --max-age "$TTL_INT"
python3 scraper/ichiban_phones_scraper.py --max-age "$TTL_INT"
python3 scraper/normalize_phones.py
echo "[update] done"

# Auto-push: rebuild the single-file chooser and publish it to the
# Cloudflare Worker, so a fresh scrape reaches the public page without a
# manual deploy. Opt-in via AUTO_DEPLOY=1 (cron sets it); needs wrangler
# auth once (`wrangler login` or CLOUDFLARE_API_TOKEN in the environment).
# Never fails the run — a failed push is a log line, not a failed scrape.
if [ "${AUTO_DEPLOY:-0}" = "1" ]; then
  {
    echo "--- $(date -Is) auto-push"
    if python3 build_worker.py && (cd deploy && ./node_modules/.bin/wrangler deploy); then
      echo "[deploy] published ok"
    else
      echo "[deploy] FAILED (scrape data is fine — see above)"
    fi
  } >> logs/deploy.log 2>&1
fi
