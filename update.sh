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

# Resilience: one store outage (DNS blip, connection reset, changed API) must
# not sink the whole refresh. Every scraper is best-effort; normalize still runs
# on whatever stores succeeded and the deploy still ships the merged snapshot.
# PARTIAL_MARKER makes the next cron pass retry a partial run instead of
# trusting the freshly-written snapshot mtime as "fresh" for a whole TTL.
PARTIAL_MARKER=data/.partial-refresh
failures=()
laptop_failed=0
phone_failed=0
laptop_norm_ok=0
phone_norm_ok=0

run_step() {
  local label="$1"; shift
  if "$@"; then
    return 0
  fi
  echo "[update] WARN   ${label} failed — continuing"
  failures+=("$label")
  return 1
}

run_scraper() {
  local kind="$1" label="$2"; shift 2
  if ! run_step "$label" "$@"; then
    if [ "$kind" = "laptop" ]; then
      laptop_failed=$((laptop_failed + 1))
    else
      phone_failed=$((phone_failed + 1))
    fi
  fi
  return 0
}

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
  # A partial run leaves PARTIAL_MARKER, forcing a retry even though the
  # snapshot mtime was refreshed by normalize.
  if [ "$then" -gt 0 ] && [ "$then_phones" -gt 0 ] \
     && [ ! -e "$PARTIAL_MARKER" ] \
     && [ $((now - then)) -lt "$((TTL_INT * 3600))" ] \
     && [ $((now - then_phones)) -lt "$((TTL_INT * 3600))" ]; then
    hours=$(( (now - then) / 3600 ))
    echo "[update] snapshot is fresh (${hours}h old, TTL ${TTL_INT}h) — nothing to do"
    exit 0
  fi
fi

echo "[update] refreshing (cache max age ${TTL_INT}h)…"
mkdir -p data
# Mark the run in progress: a crash or a partial refresh must be retried by the
# next cron pass, not treated as a fresh snapshot.
: > "$PARTIAL_MARKER"

run_scraper laptop jarir      python3 scraper/jarir_scraper.py --max-age "$TTL_INT"
run_scraper laptop extra      python3 scraper/extra_scraper.py --max-age "$TTL_INT"
run_scraper laptop datablitz  python3 scraper/datablitz_scraper.py --max-age "$TTL_INT"
run_scraper laptop pcx        python3 scraper/pcx_scraper.py --max-age "$TTL_INT"
run_scraper laptop gigahertz  python3 scraper/gigahertz_scraper.py --max-age "$TTL_INT"
run_scraper laptop noon       python3 scraper/noon_scraper.py --max-age "$TTL_INT"

if [ "$laptop_failed" -lt 6 ]; then
  run_step "normalize laptops" python3 scraper/normalize.py && laptop_norm_ok=1
else
  echo "[update] WARN   all 6 laptop stores failed — keeping the previous laptop snapshot"
fi

run_scraper phone jarir-phones  python3 scraper/jarir_phones_scraper.py --max-age "$TTL_INT"
run_scraper phone extra-phones  python3 scraper/extra_phones_scraper.py --max-age "$TTL_INT"
run_scraper phone greentelcom   python3 scraper/greentelcom_phones_scraper.py --max-age "$TTL_INT"
run_scraper phone ichiban       python3 scraper/ichiban_phones_scraper.py --max-age "$TTL_INT"

if [ "$phone_failed" -lt 4 ]; then
  run_step "normalize phones" python3 scraper/normalize_phones.py && phone_norm_ok=1
else
  echo "[update] WARN   all 4 phone stores failed — keeping the previous phone snapshot"
fi

failed_count=${#failures[@]}
if [ "$failed_count" -eq 0 ]; then
  echo "[update] done"
else
  echo "[update] done with ${failed_count} failed step(s) — next run will retry"
  printf '%s partial refresh — failed: %s; laptops normalized: %s; phones normalized: %s\n' \
    "$(date -Is)" "${failures[*]:-none}" "$laptop_norm_ok" "$phone_norm_ok" > "$PARTIAL_MARKER"
fi

# Auto-push: rebuild the single-file chooser and publish it to the
# Cloudflare Worker, so a fresh scrape reaches the public page without a
# manual deploy. Opt-in via AUTO_DEPLOY=1 (cron sets it); needs wrangler
# auth once (`wrangler login` or CLOUDFLARE_API_TOKEN in the environment).
# Never fails the run — a failed push is a log line, not a failed scrape.
mkdir -p logs
if [ "${AUTO_DEPLOY:-0}" = "1" ]; then
  {
    echo "--- $(date -Is) auto-push"
    if [ "$laptop_norm_ok" -eq 0 ] && [ "$phone_norm_ok" -eq 0 ]; then
      echo "[deploy] skipped: no catalog was normalized this run"
    elif python3 build_worker.py && (cd deploy && ./node_modules/.bin/wrangler deploy); then
      echo "[deploy] published ok"
    else
      echo "[deploy] FAILED (scrape data is fine — see above)"
    fi
  } >> logs/deploy.log 2>&1
fi

# A clean run clears the retry marker. A deploy failure is logged but does not
# trigger another scrape — the data is already fine.
if [ "$failed_count" -eq 0 ]; then
  rm -f "$PARTIAL_MARKER"
fi

# Partial success still refreshed at least one catalog, so report success to
# cron/app.py; only a run that normalized nothing is a real failure.
if [ "$laptop_norm_ok" -eq 0 ] && [ "$phone_norm_ok" -eq 0 ]; then
  exit 1
fi
