#!/usr/bin/env python3
# ABOUTME: Scrape Anthropic's pricing page and reconcile pricing.json with it.
# ABOUTME: Exits non-zero when scraped values diverge so cron can surface drift.

"""Refresh `pricing.json` from https://platform.claude.com/docs/en/docs/about-claude/pricing.md

The Anthropic pricing page renders a Markdown-style table that includes one
row per model with columns:

    | Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes |
      Cache Hits & Refreshes | Output Tokens |

This script fetches the page, extracts the rows, normalises the model
display names to the API model IDs we use as dictionary keys in
pricing.json, and rewrites the file when anything has changed.

Exit codes::

    0  Scrape succeeded; pricing.json unchanged.
    1  Scrape succeeded; pricing.json was updated (drift detected).
    2  Scrape failed (network error, parse error, missing models).

A cron / systemd timer can rely on these so the user gets notified only
when there's actual drift to look at.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRICING_FILE = REPO_ROOT / "pricing.json"

# Mintlify-hosted Anthropic docs serve a `.md` source variant alongside the
# JS-rendered HTML. The `.md` URL is plain text and trivial to parse with a
# regex; the bare URL would require a headless browser.
PRICING_URL = "https://platform.claude.com/docs/en/docs/about-claude/pricing.md"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Map the human-readable name in Anthropic's table to the API model IDs we
# use as keys in pricing.json. Multiple aliases per display name are common
# (e.g. Sonnet 4.6 has both `claude-sonnet-4-6-20250929` and the date-less
# alias `claude-sonnet-4-6`); both forms get the same rate so consumers
# keyed on either ID resolve correctly.
DISPLAY_NAME_TO_MODEL_IDS: dict[str, list[str]] = {
    "Claude Opus 4.7": ["claude-opus-4-7"],
    "Claude Opus 4.6": ["claude-opus-4-6"],
    "Claude Opus 4.5": ["claude-opus-4-5-20251101"],
    "Claude Sonnet 4.6": ["claude-sonnet-4-6", "claude-sonnet-4-6-20250929"],
    "Claude Sonnet 4.5": ["claude-sonnet-4-5-20250929"],
    "Claude Haiku 4.5": ["claude-haiku-4-5-20251001"],
}


def fetch_pricing_page(url: str = PRICING_URL, *, timeout: int = 30) -> str:
    """Return the body of Anthropic's pricing page.

    The CDN in front of platform.claude.com 403s default urllib User-Agent
    strings, so we send a regular browser UA. The page itself is public.

    Raises:
        urllib.error.URLError on network failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    return body.decode("utf-8", errors="replace")


_PRICE = r"\$\s*([\d.]+)\s*/\s*MTok"
_ROW_PATTERN = re.compile(
    r"\|\s*(?P<name>Claude [A-Za-z0-9 .]+?)\s*"
    r"(?:\([^)]*\))?"  # Optional "(deprecated)" annotation
    r"\s*\|\s*" + _PRICE +     # Base Input
    r"\s*\|\s*" + _PRICE +     # 5m Cache Writes
    r"\s*\|\s*" + _PRICE +     # 1h Cache Writes — captured but unused
    r"\s*\|\s*" + _PRICE +     # Cache Hits / Refreshes (cache_read)
    r"\s*\|\s*" + _PRICE,      # Output Tokens
    re.IGNORECASE,
)


def parse_pricing_rows(html: str) -> dict[str, dict[str, float]]:
    """Extract per-model rates from the rendered Markdown pricing table.

    Returns:
        Mapping of display name -> {input, cache_create, cache_read, output}.

    Raises:
        ValueError when no recognised rows are found (likely a layout change).
    """
    rows: dict[str, dict[str, float]] = {}
    for match in _ROW_PATTERN.finditer(html):
        name = match.group("name").strip()
        rows[name] = {
            "input": float(match.group(2)),
            "cache_create": float(match.group(3)),
            # group 4 is the 1h cache write rate, ignored
            "cache_read": float(match.group(5)),
            "output": float(match.group(6)),
        }
    if not rows:
        raise ValueError("Pricing table had no recognised rows — page layout may have changed.")
    return rows


def merge_with_existing(
    existing: dict[str, Any],
    scraped_rows: dict[str, dict[str, float]],
) -> tuple[dict[str, Any], list[str]]:
    """Apply scraped rates onto the existing pricing.json structure.

    Returns:
        Tuple of (updated config, list of human-readable change descriptions).
        Empty change list means the file is already up to date.
    """
    updated = json.loads(json.dumps(existing))  # deep copy
    changes: list[str] = []

    for display_name, rates in scraped_rows.items():
        ids = DISPLAY_NAME_TO_MODEL_IDS.get(display_name)
        if not ids:
            continue
        for model_id in ids:
            current = updated["models"].get(model_id)
            if current is None:
                updated["models"][model_id] = {
                    "name": display_name.removeprefix("Claude ").strip(),
                    **rates,
                }
                changes.append(f"added {model_id} ({display_name})")
                continue
            for field, value in rates.items():
                if float(current.get(field, -1)) != value:
                    changes.append(
                        f"{model_id}.{field}: {current.get(field)} -> {value}"
                    )
                    current[field] = value

    if changes:
        updated["last_verified"] = _dt.date.today().isoformat()
    return updated, changes


def write_pricing(file_path: Path, config: dict[str, Any]) -> None:
    """Write pricing.json with two-space indent + trailing newline."""
    file_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_PRICING_FILE,
        help=f"Path to pricing.json (default: {DEFAULT_PRICING_FILE}).",
    )
    parser.add_argument(
        "--url",
        default=PRICING_URL,
        help=f"Source URL to scrape (default: {PRICING_URL}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report drift but do not modify the file.",
    )
    args = parser.parse_args(argv)

    try:
        html = fetch_pricing_page(args.url)
    except urllib.error.URLError as exc:
        print(f"Failed to fetch {args.url}: {exc}", file=sys.stderr)
        return 2

    try:
        scraped = parse_pricing_rows(html)
    except ValueError as exc:
        print(f"Parse failure: {exc}", file=sys.stderr)
        return 2

    try:
        existing = json.loads(args.file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Pricing file not found: {args.file}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Pricing file is not valid JSON: {exc}", file=sys.stderr)
        return 2

    updated, changes = merge_with_existing(existing, scraped)
    if not changes:
        print("pricing.json is up to date.")
        return 0

    print("Drift detected:")
    for change in changes:
        print(f"  - {change}")

    if args.dry_run:
        print("Dry run; not writing.")
        return 1

    write_pricing(args.file, updated)
    print(f"Wrote updates to {args.file}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
