# ABOUTME: Tests for scripts/refresh.py — pricing-page scraper and reconciler.
# ABOUTME: Covers row parsing, merge-with-existing logic, and CLI exit codes.

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "refresh.py"


@pytest.fixture(scope="module")
def module() -> Any:
    """Import scripts/refresh.py by file path so tests can call its helpers."""
    spec = importlib.util.spec_from_file_location("refresh", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["refresh"] = mod
    spec.loader.exec_module(mod)
    return mod


# Mirrors the literal pipe-table layout of the real Anthropic pricing page.
_TABLE_ROWS = [
    "| Model | Base Input Tokens | 5m Cache Writes | 1h Cache Writes |"
    " Cache Hits & Refreshes | Output Tokens |",
    "|-------|-------------------|-----------------|-----------------|"
    "------------------------|---------------|",
    "| Claude Opus 4.7   | $5 / MTok | $6.25 / MTok | $10 / MTok |"
    " $0.50 / MTok | $25 / MTok |",
    "| Claude Opus 4.6   | $5 / MTok | $6.25 / MTok | $10 / MTok |"
    " $0.50 / MTok | $25 / MTok |",
    "| Claude Sonnet 4.6 | $3 / MTok | $3.75 / MTok | $6 / MTok |"
    " $0.30 / MTok | $15 / MTok |",
    "| Claude Haiku 4.5  | $1 / MTok | $1.25 / MTok | $2 / MTok |"
    " $0.10 / MTok | $5 / MTok |",
    "| Claude Opus 3 (deprecated) | $15 / MTok | $18.75 / MTok | $30 / MTok |"
    " $1.50 / MTok | $75 / MTok |",
]
SAMPLE_HTML = "\n".join(_TABLE_ROWS)


class TestParsePricingRows:
    def test_extracts_all_known_models(self, module: Any) -> None:
        rows = module.parse_pricing_rows(SAMPLE_HTML)
        assert "Claude Opus 4.7" in rows
        assert "Claude Sonnet 4.6" in rows
        assert "Claude Haiku 4.5" in rows

    def test_extracts_correct_rates_for_opus_47(self, module: Any) -> None:
        rows = module.parse_pricing_rows(SAMPLE_HTML)
        assert rows["Claude Opus 4.7"] == {
            "input": 5.0,
            "cache_create": 6.25,
            "cache_read": 0.50,
            "output": 25.0,
        }

    def test_extracts_haiku_45_with_decimal_one(self, module: Any) -> None:
        """Bare integer dollar amounts ('$1', '$5') must parse as floats."""
        rows = module.parse_pricing_rows(SAMPLE_HTML)
        assert rows["Claude Haiku 4.5"]["input"] == 1.0
        assert rows["Claude Haiku 4.5"]["output"] == 5.0

    def test_skips_deprecated_annotation(self, module: Any) -> None:
        """`Claude Opus 3 (deprecated)` parses as `Claude Opus 3` — the
        annotation in parens should not pollute the model name key."""
        rows = module.parse_pricing_rows(SAMPLE_HTML)
        assert "Claude Opus 3" in rows

    def test_empty_html_raises(self, module: Any) -> None:
        with pytest.raises(ValueError, match="recognised rows"):
            module.parse_pricing_rows("nothing here")


class TestMergeWithExisting:
    def _baseline(self) -> dict[str, Any]:
        return {
            "last_verified": "2026-01-01",
            "models": {
                "claude-opus-4-7": {
                    "name": "Opus 4.7",
                    "input": 5.0,
                    "cache_create": 6.25,
                    "cache_read": 0.50,
                    "output": 25.0,
                },
                "<synthetic>": {
                    "name": "Synthetic marker",
                    "input": 0.0,
                    "cache_create": 0.0,
                    "cache_read": 0.0,
                    "output": 0.0,
                },
            },
            "defaults": {
                "input": 15.0,
                "cache_create": 18.75,
                "cache_read": 1.50,
                "output": 75.0,
            },
        }

    def test_no_changes_when_scrape_matches(self, module: Any) -> None:
        scraped = {
            "Claude Opus 4.7": {
                "input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0,
            },
        }
        updated, changes = module.merge_with_existing(self._baseline(), scraped)
        assert changes == []
        assert updated["last_verified"] == "2026-01-01"

    def test_changes_recorded_when_rate_drifts(self, module: Any) -> None:
        scraped = {
            "Claude Opus 4.7": {
                "input": 6.0,  # Drift
                "cache_create": 6.25,
                "cache_read": 0.50,
                "output": 25.0,
            },
        }
        updated, changes = module.merge_with_existing(self._baseline(), scraped)
        assert any("claude-opus-4-7.input" in c for c in changes)
        assert updated["models"]["claude-opus-4-7"]["input"] == 6.0
        # last_verified bumped
        assert updated["last_verified"] != "2026-01-01"

    def test_synthetic_marker_preserved(self, module: Any) -> None:
        scraped = {
            "Claude Opus 4.7": {
                "input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0,
            },
        }
        updated, _ = module.merge_with_existing(self._baseline(), scraped)
        assert "<synthetic>" in updated["models"]
        assert updated["models"]["<synthetic>"]["input"] == 0.0

    def test_sonnet_46_updates_both_aliases(self, module: Any) -> None:
        """When the table lists Claude Sonnet 4.6 once, both the dateless
        alias and the date-suffixed alias should pick up the new rate."""
        baseline = self._baseline()
        baseline["models"]["claude-sonnet-4-6"] = {
            "name": "Sonnet 4.6",
            "input": 2.0,  # Stale
            "cache_create": 3.75,
            "cache_read": 0.30,
            "output": 15.0,
        }
        baseline["models"]["claude-sonnet-4-6-20250929"] = {
            "name": "Sonnet 4.6",
            "input": 2.0,  # Stale
            "cache_create": 3.75,
            "cache_read": 0.30,
            "output": 15.0,
        }
        scraped = {
            "Claude Sonnet 4.6": {
                "input": 3.0,
                "cache_create": 3.75,
                "cache_read": 0.30,
                "output": 15.0,
            },
        }
        updated, changes = module.merge_with_existing(baseline, scraped)
        assert updated["models"]["claude-sonnet-4-6"]["input"] == 3.0
        assert updated["models"]["claude-sonnet-4-6-20250929"]["input"] == 3.0
        assert any("claude-sonnet-4-6.input" in c for c in changes)
        assert any("claude-sonnet-4-6-20250929.input" in c for c in changes)

    def test_unknown_display_name_ignored(self, module: Any) -> None:
        """A row for some future model we haven't mapped to an API ID
        should not get added with a guessed key."""
        scraped = {
            "Claude Future 5.0": {
                "input": 99.0, "cache_create": 99.0, "cache_read": 99.0, "output": 99.0,
            },
        }
        updated, changes = module.merge_with_existing(self._baseline(), scraped)
        assert changes == []
        assert "claude-future-5-0" not in updated["models"]


class TestEndToEnd:
    """Drive `main()` against a fake pricing page response."""

    def test_dry_run_reports_drift_without_writing(
        self, tmp_path: Path, module: Any, capsys: pytest.CaptureFixture[str],
    ) -> None:
        pricing_file = tmp_path / "pricing.json"
        pricing_file.write_text(json.dumps({
            "last_verified": "2026-01-01",
            "models": {
                "claude-opus-4-7": {
                    "name": "Opus 4.7",
                    "input": 4.0,  # Drift from sample's $5
                    "cache_create": 6.25,
                    "cache_read": 0.50,
                    "output": 25.0,
                },
            },
            "defaults": {"input": 15.0, "cache_create": 18.75, "cache_read": 1.50, "output": 75.0},
        }))
        before = pricing_file.read_text()

        with patch.object(module, "fetch_pricing_page", return_value=SAMPLE_HTML):
            exit_code = module.main(["--file", str(pricing_file), "--dry-run"])

        assert exit_code == 1
        assert pricing_file.read_text() == before
        out = capsys.readouterr().out
        assert "Drift detected" in out

    def test_writes_when_drift_and_not_dry_run(
        self, tmp_path: Path, module: Any,
    ) -> None:
        pricing_file = tmp_path / "pricing.json"
        pricing_file.write_text(json.dumps({
            "last_verified": "2026-01-01",
            "models": {
                "claude-opus-4-7": {
                    "name": "Opus 4.7",
                    "input": 4.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0,
                },
            },
            "defaults": {"input": 15.0, "cache_create": 18.75, "cache_read": 1.50, "output": 75.0},
        }))

        with patch.object(module, "fetch_pricing_page", return_value=SAMPLE_HTML):
            exit_code = module.main(["--file", str(pricing_file)])

        assert exit_code == 1
        new_config = json.loads(pricing_file.read_text())
        assert new_config["models"]["claude-opus-4-7"]["input"] == 5.0
        # last_verified bumped to today (any real date)
        assert new_config["last_verified"] != "2026-01-01"

    def test_no_drift_returns_zero(
        self, tmp_path: Path, module: Any,
    ) -> None:
        """Baseline must match every row SAMPLE_HTML emits, otherwise the merge
        will (correctly) report missing models as added."""
        pricing_file = tmp_path / "pricing.json"
        pricing_file.write_text(json.dumps({
            "last_verified": "2026-04-01",
            "models": {
                "claude-opus-4-7": {
                    "name": "Opus 4.7",
                    "input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0,
                },
                "claude-opus-4-6": {
                    "name": "Opus 4.6",
                    "input": 5.0, "cache_create": 6.25, "cache_read": 0.50, "output": 25.0,
                },
                "claude-sonnet-4-6": {
                    "name": "Sonnet 4.6",
                    "input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0,
                },
                "claude-sonnet-4-6-20250929": {
                    "name": "Sonnet 4.6",
                    "input": 3.0, "cache_create": 3.75, "cache_read": 0.30, "output": 15.0,
                },
                "claude-haiku-4-5-20251001": {
                    "name": "Haiku 4.5",
                    "input": 1.0, "cache_create": 1.25, "cache_read": 0.10, "output": 5.0,
                },
            },
            "defaults": {"input": 15.0, "cache_create": 18.75, "cache_read": 1.50, "output": 75.0},
        }))

        with patch.object(module, "fetch_pricing_page", return_value=SAMPLE_HTML):
            exit_code = module.main(["--file", str(pricing_file)])

        assert exit_code == 0

    def test_returns_2_on_fetch_failure(
        self, tmp_path: Path, module: Any, capsys: pytest.CaptureFixture[str],
    ) -> None:
        import urllib.error as urlerr
        pricing_file = tmp_path / "pricing.json"
        pricing_file.write_text("{}")

        with patch.object(
            module, "fetch_pricing_page",
            side_effect=urlerr.URLError("boom"),
        ):
            exit_code = module.main(["--file", str(pricing_file)])

        assert exit_code == 2
        assert "Failed to fetch" in capsys.readouterr().err


class TestPricingFileShape:
    """Schema invariants the consumer repos rely on."""

    def test_canonical_pricing_loads(self) -> None:
        canonical = SCRIPT_PATH.parent.parent / "pricing.json"
        config = json.loads(canonical.read_text())
        assert "models" in config
        assert "defaults" in config
        assert "last_verified" in config

    def test_required_models_present(self) -> None:
        canonical = SCRIPT_PATH.parent.parent / "pricing.json"
        config = json.loads(canonical.read_text())
        for model in (
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6-20250929",
            "claude-haiku-4-5-20251001",
            "<synthetic>",
        ):
            assert model in config["models"], f"{model} missing from canonical pricing.json"

    def test_every_model_has_required_fields(self) -> None:
        canonical = SCRIPT_PATH.parent.parent / "pricing.json"
        config = json.loads(canonical.read_text())
        for model_id, rates in config["models"].items():
            for field in ("input", "output", "cache_create", "cache_read", "name"):
                assert field in rates, f"{model_id} missing {field}"

    def test_sonnet_46_alias_and_dated_form_agree(self) -> None:
        """Both ID forms must carry identical rates so consumers keyed on
        either resolve to the same cost."""
        canonical = SCRIPT_PATH.parent.parent / "pricing.json"
        config = json.loads(canonical.read_text())
        a = config["models"]["claude-sonnet-4-6"]
        b = config["models"]["claude-sonnet-4-6-20250929"]
        for field in ("input", "output", "cache_create", "cache_read"):
            assert a[field] == b[field], f"{field} differs between sonnet-4-6 aliases"
