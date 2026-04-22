"""
PrimeTradeML - Pipeline Validation Tests
==========================================
Tests cover config validation, dataset validation, signal logic,
determinism, and error-state metrics output.

Run:  python -m pytest tests/ -v
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import (
    compute_rolling_mean,
    generate_signals,
    load_config,
    load_dataset,
    write_error_metrics,
)


# ── Helpers ──────────────────────────────────────────────────

def _make_logger():
    """Create a silent logger for testing."""
    import logging
    logger = logging.getLogger("test")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _write_yaml(path, data):
    """Write a dict as YAML to the given path."""
    Path(path).write_text(yaml.dump(data), encoding="utf-8")


def _write_csv(path, content):
    """Write raw CSV string to the given path."""
    Path(path).write_text(content, encoding="utf-8")


# ── Config Validation Tests ──────────────────────────────────

class TestConfigValidation:
    """Verify config loader catches all invalid states."""

    def test_valid_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, {"seed": 42, "window": 5, "version": "v1"})
        result = load_config(str(cfg), _make_logger())
        assert result["seed"] == 42
        assert result["window"] == 5
        assert result["version"] == "v1"

    def test_missing_config_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml", _make_logger())

    def test_empty_config_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_config(str(cfg), _make_logger())

    def test_missing_required_keys(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, {"seed": 42})  # missing window + version
        with pytest.raises(ValueError, match="Missing required"):
            load_config(str(cfg), _make_logger())

    def test_invalid_seed_type(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, {"seed": "abc", "window": 5, "version": "v1"})
        with pytest.raises(ValueError, match="seed"):
            load_config(str(cfg), _make_logger())

    def test_invalid_window_value(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        _write_yaml(cfg, {"seed": 42, "window": 0, "version": "v1"})
        with pytest.raises(ValueError, match="window"):
            load_config(str(cfg), _make_logger())


# ── Dataset Validation Tests ─────────────────────────────────

class TestDatasetValidation:
    """Verify dataset loader catches all invalid states."""

    def test_missing_input_file(self):
        with pytest.raises(FileNotFoundError):
            load_dataset("/nonexistent/data.csv", _make_logger())

    def test_empty_input_file(self, tmp_path):
        csv = tmp_path / "empty.csv"
        csv.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_dataset(str(csv), _make_logger())

    def test_missing_close_column(self, tmp_path):
        csv = tmp_path / "bad.csv"
        _write_csv(csv, "open,high,low\n1,2,3\n4,5,6\n")
        with pytest.raises(ValueError, match="close"):
            load_dataset(str(csv), _make_logger())

    def test_valid_dataset(self, tmp_path):
        csv = tmp_path / "good.csv"
        _write_csv(csv, "timestamp,close\n2024-01-01,100.0\n2024-01-02,200.0\n")
        df = load_dataset(str(csv), _make_logger())
        assert len(df) == 2
        assert "close" in df.columns

    def test_quoted_row_format(self, tmp_path):
        """Handles the actual data.csv format where rows are quoted."""
        csv = tmp_path / "quoted.csv"
        content = (
            '"timestamp,open,high,low,close,volume"\n'
            '"2024-01-01,100,110,90,105,1000"\n'
            '"2024-01-02,105,115,95,110,2000"\n'
        )
        _write_csv(csv, content)
        df = load_dataset(str(csv), _make_logger())
        assert len(df) == 2
        assert df["close"].iloc[0] == 105.0


# ── Signal Logic Tests ───────────────────────────────────────

class TestSignalLogic:
    """Verify rolling mean + signal generation correctness."""

    def test_rolling_mean_nan_handling(self):
        """First window-1 rows must be NaN."""
        df = pd.DataFrame({"close": [10, 20, 30, 40, 50]})
        rm = compute_rolling_mean(df, window=3, logger=_make_logger())
        assert rm.isna().sum() == 2  # first 2 rows are NaN
        assert rm.iloc[2] == pytest.approx(20.0)

    def test_signal_values(self):
        """Signal = 1 when close > rolling_mean, else 0."""
        df = pd.DataFrame({"close": [10, 20, 30, 25, 50]})
        rm = compute_rolling_mean(df, window=3, logger=_make_logger())
        sig = generate_signals(df, rm, _make_logger())
        # Row 2: close=30, rm=20.0 -> 1
        assert sig.iloc[2] == 1
        # Row 3: close=25, rm=25.0 (mean of 20,30,25=25) -> 0 (not >)
        assert sig.iloc[3] == 0
        # Row 4: close=50, rm=35.0 -> 1
        assert sig.iloc[4] == 1

    def test_signal_nan_for_incomplete_window(self):
        """Rows without a valid rolling mean get NaN signal."""
        df = pd.DataFrame({"close": [10, 20, 30]})
        rm = compute_rolling_mean(df, window=3, logger=_make_logger())
        sig = generate_signals(df, rm, _make_logger())
        assert sig.isna().sum() == 2
        assert sig.notna().sum() == 1


# ── Determinism Test ─────────────────────────────────────────

class TestDeterminism:
    """Pipeline must produce identical output on repeated runs."""

    def test_identical_metrics_across_runs(self):
        """Run pipeline twice, compare metrics JSON."""
        project_root = Path(__file__).resolve().parent.parent
        cmd = [
            sys.executable, str(project_root / "run.py"),
            "--input", str(project_root / "data.csv"),
            "--config", str(project_root / "config.yaml"),
        ]

        results = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "m.json")
                log = os.path.join(td, "r.log")
                r = subprocess.run(
                    cmd + ["--output", out, "--log-file", log],
                    capture_output=True, text=True,
                )
                assert r.returncode == 0, f"Pipeline failed: {r.stderr}"
                with open(out) as f:
                    metrics = json.load(f)
                # Remove latency (non-deterministic timing)
                metrics.pop("latency_ms", None)
                results.append(metrics)

        assert results[0] == results[1], "Outputs differ between runs"


class TestCliContract:
    """Verify evaluator-facing CLI behavior and metrics schema."""

    def test_success_cli_outputs_clean_json(self):
        project_root = Path(__file__).resolve().parent.parent

        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "metrics.json")
            log = os.path.join(td, "run.log")

            result = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "run.py"),
                    "--input",
                    str(project_root / "data.csv"),
                    "--config",
                    str(project_root / "config.yaml"),
                    "--output",
                    out,
                    "--log-file",
                    log,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            assert result.returncode == 0, result.stderr
            stdout_json = json.loads(result.stdout)
            file_json = json.loads(Path(out).read_text())

            assert stdout_json == file_json
            assert stdout_json["rows_processed"] == 10000
            assert stdout_json["metric"] == "signal_rate"
            assert stdout_json["value"] == pytest.approx(0.4991)
            assert stdout_json["seed"] == 42
            assert stdout_json["status"] == "success"
            assert result.stderr == ""

    def test_error_metrics_default_to_v1_when_config_is_missing(self):
        project_root = Path(__file__).resolve().parent.parent

        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "metrics.json")
            log = os.path.join(td, "run.log")

            result = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "run.py"),
                    "--input",
                    str(project_root / "data.csv"),
                    "--config",
                    str(project_root / "missing-config.yaml"),
                    "--output",
                    out,
                    "--log-file",
                    log,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            assert result.returncode == 1
            stdout_json = json.loads(result.stdout)
            file_json = json.loads(Path(out).read_text())

            assert stdout_json == file_json
            assert stdout_json["version"] == "v1"
            assert stdout_json["status"] == "error"
            assert "Config file not found" in stdout_json["error_message"]


# ── Error Metrics Test ───────────────────────────────────────

class TestErrorMetrics:
    """Error state must still produce valid metrics JSON."""

    def test_error_metrics_structure(self, tmp_path):
        out = tmp_path / "err.json"
        write_error_metrics(str(out), "test failure", version="v1")
        data = json.loads(out.read_text())
        assert data["status"] == "error"
        assert data["version"] == "v1"
        assert "test failure" in data["error_message"]
