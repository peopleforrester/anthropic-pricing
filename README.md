# anthropic-pricing

Single source of truth for per-model Anthropic API pricing across consumer
repos (`llm-coding-workflow`, `Engineering_Journal`).

Pricing data lives as a flat JSON file. A daily refresh script scrapes the
official Anthropic pricing page and rewrites the file when rates change.

## Files

- `pricing.json` — canonical rates. Edit by hand only when the refresh
  script disagrees with what you see on Anthropic's pricing page.
- `scripts/refresh.py` — fetch Anthropic's pricing docs and reconcile
  `pricing.json` against them. Exits non-zero on drift so cron can
  surface it.
- `tests/` — pytest suite for refresh logic and schema invariants.

## Schema

```json
{
  "last_verified": "YYYY-MM-DD",
  "models": {
    "<model-id>": {
      "name": "Display name",
      "input": <USD per MTok>,
      "output": <USD per MTok>,
      "cache_create": <USD per MTok, 5-minute>,
      "cache_read": <USD per MTok>
    }
  },
  "defaults": {
    "input": <fallback>,
    "output": <fallback>,
    "cache_create": <fallback>,
    "cache_read": <fallback>
  }
}
```

`<synthetic>` is reserved for Claude Code's post-compact assistant marker.
Those messages always have zero token usage, so the entry exists to silence
the unknown-model warning, not to charge anything.

`defaults` exist for unknown models. Set to the legacy Opus 4.0/4.1 tier
($15/$75) as a deliberate over-estimate — never silently apply zero, never
make an unknown model look free.

## Bootstrap on a new host

```bash
git clone https://github.com/peopleforrester/anthropic-pricing.git \
    ~/repos/workflow/anthropic-pricing
```

That's it. The default path consumers look for is
`~/repos/workflow/anthropic-pricing/pricing.json`. Override with
`ANTHROPIC_PRICING_FILE=/some/other/path/pricing.json` for tests or
non-standard layouts.

## Consumers

| Repo | Path | Reader |
|------|------|--------|
| `llm-coding-workflow` | `src/workflow/parsers/sessions.py` | `_load_pricing()` reads `$ANTHROPIC_PRICING_FILE` or the default path |
| `Engineering_Journal` | `scripts/harvest-sessions.sh` | Reads via `jq` from the same path (PRD #6 M3 in progress) |

## Running the refresh

```bash
cd ~/repos/workflow/anthropic-pricing
uv run --python 3.13 python scripts/refresh.py            # update if drift
uv run --python 3.13 python scripts/refresh.py --dry-run  # report only
```

Exit codes:

- `0` — `pricing.json` is up to date.
- `1` — drift detected (with `--dry-run`, no write; without, the file was
  rewritten).
- `2` — scrape failure (network error, parse error, missing file).
- `3` — the page lists a Claude model with no mapping. Add it to
  `DISPLAY_NAME_TO_MODEL_IDS` (or, if it's a model consumers don't need priced,
  to `IGNORED_DISPLAY_NAMES`). Any mapped-model drift is still written first;
  only the mapping needs a human. This is what keeps a net-new model from
  slipping past unnoticed.

A daily systemd-user timer fires the dry-run on Netcup (PRD #6 M4); when
drift appears it opens a PR on the GitHub repo instead of auto-committing.

## License

MIT.
