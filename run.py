#!/usr/bin/env python3
"""
PrimeTradeML - Deterministic Batch Signal Pipeline
===================================================
A minimal MLOps-style batch job that:
  1. Loads and validates YAML configuration
  2. Reads and validates OHLCV market data
  3. Computes a rolling mean on the close price
  4. Generates binary trading signals (close vs rolling mean)
  5. Writes structured metrics JSON and detailed logs

Usage:
    python run.py --input data.csv --config config.yaml \
                  --output metrics.json --log-file run.log

Mirrors the type of work done in MetaStackerBandit
(trading-signal pipelines).
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


# ──────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    """Parse and return CLI arguments. No hard-coded paths."""
    parser = argparse.ArgumentParser(
        description="PrimeTradeML: deterministic batch signal pipeline",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the OHLCV CSV dataset (e.g. data.csv)",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the YAML configuration file (e.g. config.yaml)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path for the output metrics JSON (e.g. metrics.json)",
    )
    parser.add_argument(
        "--log-file",
        required=True,
        help="Path for the output log file (e.g. run.log)",
    )
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────
def setup_logging(log_file: str) -> logging.Logger:
    """
    Configure structured logging to both file and stdout.
    Returns the configured logger instance.
    """
    logger = logging.getLogger("PrimeTradeML")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — captures everything
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler — INFO and above
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


# ──────────────────────────────────────────────────────────────
# Error metrics helper
# ──────────────────────────────────────────────────────────────
def write_error_metrics(
    output_path: str,
    error_message: str,
    version: str = "unknown",
) -> None:
    """Write a structured error-state metrics file."""
    metrics = {
        "version": version,
        "status": "error",
        "error_message": error_message,
    }
    Path(output_path).write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )


# ──────────────────────────────────────────────────────────────
# Config loader & validator
# ──────────────────────────────────────────────────────────────
REQUIRED_CONFIG_KEYS = {"seed", "window", "version"}


def load_config(config_path: str, logger: logging.Logger) -> dict:
    """
    Load YAML config and validate that all required keys
    (seed, window, version) are present with correct types.
    Raises ValueError on validation failure.
    """
    path = Path(config_path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Config file is empty: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(
            f"Invalid config structure: expected a YAML mapping, "
            f"got {type(config).__name__}"
        )

    # Check required keys
    missing = REQUIRED_CONFIG_KEYS - set(config.keys())
    if missing:
        raise ValueError(f"Missing required config keys: {sorted(missing)}")

    # Type checks
    if not isinstance(config["seed"], int):
        raise ValueError(
            f"'seed' must be an integer, got {type(config['seed']).__name__}"
        )
    if not isinstance(config["window"], int) or config["window"] < 1:
        raise ValueError(
            f"'window' must be a positive integer, got {config['window']}"
        )
    if not isinstance(config["version"], str) or not config["version"].strip():
        raise ValueError("'version' must be a non-empty string")

    logger.info(
        "Config loaded and validated  |  seed=%d  window=%d  version=%s",
        config["seed"],
        config["window"],
        config["version"],
    )
    return config


# ──────────────────────────────────────────────────────────────
# Dataset loader & validator
# ──────────────────────────────────────────────────────────────
def load_dataset(input_path: str, logger: logging.Logger) -> pd.DataFrame:
    """
    Load and validate the OHLCV CSV dataset.
    Handles:
      - Missing file
      - Empty file
      - Invalid CSV format (including quoted-row formats)
      - Missing 'close' column

    Returns a clean DataFrame with a numeric 'close' column.
    """
    path = Path(input_path)

    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Input file is empty: {input_path}")

    # Read raw content to handle the quoted-row CSV format
    # where each entire row is wrapped in double quotes
    raw = path.read_text(encoding="utf-8")

    # Strip surrounding quotes from each line and normalize line endings
    lines = []
    for line in raw.splitlines():
        stripped = line.strip().strip('"')
        if stripped:
            lines.append(stripped)

    if len(lines) < 2:
        raise ValueError(
            f"Dataset has no data rows (found {len(lines)} lines total)"
        )

    clean_csv = "\n".join(lines)

    try:
        df = pd.read_csv(StringIO(clean_csv))
    except Exception as exc:
        raise ValueError(f"Failed to parse CSV: {exc}") from exc

    if df.empty:
        raise ValueError("Dataset is empty after parsing")

    # Validate 'close' column exists
    if "close" not in df.columns:
        available = ", ".join(df.columns.tolist())
        raise ValueError(
            f"Required column 'close' not found. "
            f"Available columns: [{available}]"
        )

    # Coerce close to numeric (use .loc to avoid chained assignment)
    df = df.copy()
    df.loc[:, "close"] = pd.to_numeric(df["close"], errors="coerce")
    nan_count = df["close"].isna().sum()
    if nan_count == len(df):
        raise ValueError("Column 'close' contains no valid numeric values")
    if nan_count > 0:
        logger.warning(
            "Found %d non-numeric values in 'close' column; "
            "these rows will be excluded from signal computation",
            nan_count,
        )

    logger.info(
        "Dataset loaded  |  rows=%d  columns=%d  "
        "close_range=[%.2f, %.2f]",
        len(df),
        len(df.columns),
        df["close"].min(),
        df["close"].max(),
    )
    return df


# ──────────────────────────────────────────────────────────────
# Processing: rolling mean + signal generation
# ──────────────────────────────────────────────────────────────
def compute_rolling_mean(
    df: pd.DataFrame, window: int, logger: logging.Logger
) -> pd.Series:
    """
    Compute rolling mean on the 'close' column.

    Strategy for first (window-1) rows:
        NaN values are left in place. These rows are excluded
        from signal computation to avoid forward-looking bias
        and ensure deterministic results.
    """
    rolling_mean = df["close"].rolling(window=window, min_periods=window).mean()
    valid_count = rolling_mean.notna().sum()

    logger.info(
        "Rolling mean computed  |  window=%d  "
        "valid_rows=%d  nan_rows=%d",
        window,
        valid_count,
        len(df) - valid_count,
    )
    return rolling_mean


def generate_signals(
    df: pd.DataFrame,
    rolling_mean: pd.Series,
    logger: logging.Logger,
) -> pd.Series:
    """
    Generate binary trading signal:
        signal = 1  if close > rolling_mean
        signal = 0  otherwise (including NaN rolling_mean rows)

    Rows where rolling_mean is NaN get signal = NaN
    and are excluded from signal_rate calculation.
    """
    signal = pd.Series(np.nan, index=df.index, dtype="float64")

    valid_mask = rolling_mean.notna()
    signal.loc[valid_mask] = np.where(
        df.loc[valid_mask, "close"] > rolling_mean[valid_mask], 1, 0
    ).astype(float)

    signal_ones = int((signal == 1).sum())
    signal_zeros = int((signal == 0).sum())
    signal_nans = int(signal.isna().sum())

    logger.info(
        "Signals generated  |  bullish=%d  bearish=%d  "
        "excluded(NaN)=%d",
        signal_ones,
        signal_zeros,
        signal_nans,
    )
    return signal


# ──────────────────────────────────────────────────────────────
# Metrics computation & output
# ──────────────────────────────────────────────────────────────
def compute_and_write_metrics(
    signal: pd.Series,
    config: dict,
    latency_ms: int,
    output_path: str,
    logger: logging.Logger,
) -> dict:
    """
    Compute final metrics and write structured JSON.

    signal_rate is computed only over rows where a valid
    signal was generated (i.e., excluding the first window-1
    NaN rows).
    """
    valid_signals = signal.dropna()
    rows_processed = len(valid_signals)
    signal_rate = round(float(valid_signals.mean()), 4)

    metrics = {
        "version": config["version"],
        "rows_processed": rows_processed,
        "metric": "signal_rate",
        "value": signal_rate,
        "latency_ms": latency_ms,
        "seed": config["seed"],
        "status": "success",
    }

    Path(output_path).write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )

    logger.info(
        "Metrics computed  |  rows_processed=%d  "
        "signal_rate=%.4f  latency_ms=%d",
        rows_processed,
        signal_rate,
        latency_ms,
    )
    return metrics


# ──────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────
def main() -> int:
    """
    Execute the full pipeline:
      1. Parse CLI arguments
      2. Setup structured logging
      3. Load + validate config
      4. Set deterministic seed
      5. Load + validate dataset
      6. Compute rolling mean
      7. Generate signals
      8. Compute metrics and write output
      9. Print final metrics to stdout

    Returns 0 on success, 1 on failure.
    """
    args = parse_args()

    # Initialize logging
    logger = setup_logging(args.log_file)

    start_time = time.perf_counter()
    start_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    logger.info("=" * 60)
    logger.info("JOB START  |  %s", start_ts)
    logger.info("=" * 60)
    logger.info(
        "CLI args  |  input=%s  config=%s  output=%s  log_file=%s",
        args.input,
        args.config,
        args.output,
        args.log_file,
    )

    version = "unknown"

    try:
        # ── Step 1: Config ──────────────────────────────────
        logger.info("Step 1/5: Loading configuration...")
        config = load_config(args.config, logger)
        version = config["version"]

        # ── Step 2: Seed ────────────────────────────────────
        logger.info("Step 2/5: Setting deterministic seed...")
        np.random.seed(config["seed"])
        logger.info("NumPy random seed set to %d", config["seed"])

        # ── Step 3: Dataset ─────────────────────────────────
        logger.info("Step 3/5: Loading dataset...")
        df = load_dataset(args.input, logger)

        # ── Step 4: Rolling mean ────────────────────────────
        logger.info("Step 4/5: Computing rolling mean...")
        rolling_mean = compute_rolling_mean(df, config["window"], logger)

        # ── Step 5: Signal generation ───────────────────────
        logger.info("Step 5/5: Generating trading signals...")
        signal = generate_signals(df, rolling_mean, logger)

        # ── Metrics ─────────────────────────────────────────
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        metrics = compute_and_write_metrics(
            signal, config, elapsed_ms, args.output, logger
        )

        logger.info("-" * 60)
        logger.info("METRICS SUMMARY")
        logger.info("-" * 60)
        for key, val in metrics.items():
            logger.info("  %-20s : %s", key, val)
        logger.info("-" * 60)

        logger.info("=" * 60)
        logger.info(
            "JOB END  |  status=SUCCESS  |  duration=%dms", elapsed_ms
        )
        logger.info("=" * 60)

        # Print final metrics JSON to stdout (Docker requirement)
        print(json.dumps(metrics, indent=2))
        return 0

    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        logger.error("Pipeline failed: %s", exc)
        write_error_metrics(args.output, str(exc), version)
        logger.info("=" * 60)
        logger.info(
            "JOB END  |  status=FAILED  |  duration=%dms", elapsed_ms
        )
        logger.info("=" * 60)
        print(
            json.dumps(
                {
                    "version": version,
                    "status": "error",
                    "error_message": str(exc),
                },
                indent=2,
            )
        )
        return 1

    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        logger.exception("Unexpected error: %s", exc)
        write_error_metrics(args.output, f"Unexpected: {exc}", version)
        logger.info("=" * 60)
        logger.info(
            "JOB END  |  status=FAILED  |  duration=%dms", elapsed_ms
        )
        logger.info("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
