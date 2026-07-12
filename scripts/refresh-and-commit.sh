#!/usr/bin/env bash
# ABOUTME: Daily automation wrapper — refresh pricing.json from upstream docs,
# ABOUTME: and commit+push only when the scrape actually changed something.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

log() { printf '%s [pricing-refresh] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# refresh.py exit codes: 0 up-to-date, 1 drift written, 2 scrape failure,
#                        3 new unmapped model on the page (needs a human).
set +e
uv run --python 3.13 python scripts/refresh.py
rc=$?
set -e

case "${rc}" in
    0)
        log "pricing.json up to date; nothing to commit."
        exit 0
        ;;
    1)
        log "drift detected and written; committing + pushing."
        # Pull first so a sibling host's push doesn't reject ours; rebase keeps
        # history linear. The only tracked change should be pricing.json.
        git pull --rebase --quiet origin main || {
            log "ERROR: rebase pull failed; leaving change uncommitted for manual review."
            exit 2
        }
        git add pricing.json
        git commit -q -m "chore: refresh model pricing from upstream docs (automated)"
        git push --quiet origin HEAD:main
        log "pushed updated pricing.json."
        exit 0
        ;;
    3)
        log "ERROR: pricing page lists a Claude model with no mapping. A human"
        log "must add it to DISPLAY_NAME_TO_MODEL_IDS (see stderr above). Not"
        log "committing — any mapped-model drift written stays uncommitted for"
        log "the same human to review and commit alongside the new mapping."
        exit 3
        ;;
    *)
        log "ERROR: refresh failed (scrape error, exit ${rc}). Not committing."
        exit "${rc}"
        ;;
esac
