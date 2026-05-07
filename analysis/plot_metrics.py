#!/usr/bin/env python3
"""
plot_metrics.py
===============
Quick-look visualisation for traffic_generator CSV logs. Produces three
panels in a single figure:

    1. Latency over time, scattered by backend       (per-flow QoS view)
    2. Backend hit-distribution histogram            (fairness audit)
    3. Rolling p50/p95 latency                       (tail behaviour)

These are the three plots you'll want side-by-side in your final report.

Run:
    python3 -m analysis.plot_metrics /tmp/h1.csv /tmp/h2.csv /tmp/h3.csv \
        --out results.png --title "QoS-weighted strategy, burst workload"

Tip: combine multiple client logs in one call -- pandas concatenates them.
"""

# Standard library
import argparse
import sys
from pathlib import Path

# Third-party
import matplotlib.pyplot as plt
import pandas as pd


# ---------------------------------------------------------------------------
#  Loader
# ---------------------------------------------------------------------------
def load_logs(paths) -> pd.DataFrame:
    """Read N CSVs (skipping rows with errors) into one DataFrame."""
    frames = []
    for pth in paths:
        df = pd.read_csv(pth, parse_dates=["timestamp"])
        df["source_file"] = Path(pth).stem
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    # Drop error rows from latency/distribution stats; they'd skew everything.
    # We keep them around as a separate count so the report can mention them.
    return df


# ---------------------------------------------------------------------------
#  Plotters
# ---------------------------------------------------------------------------
def _scatter_by_backend(ax, df: pd.DataFrame) -> None:
    """Latency vs time, colour-coded by which backend served the request."""
    ok = df[df["backend"] != "error"].copy()
    # Convert to elapsed seconds so the x-axis isn't a wall of timestamps.
    t0 = ok["timestamp"].min()
    ok["elapsed_s"] = (ok["timestamp"] - t0).dt.total_seconds()

    for name, group in ok.groupby("backend"):
        ax.scatter(group["elapsed_s"], group["latency_ms"],
                   s=12, alpha=0.6, label=name)

    ax.set_xlabel("elapsed time (s)")
    ax.set_ylabel("latency (ms)")
    ax.set_title("Per-request latency, coloured by backend")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)


def _backend_distribution(ax, df: pd.DataFrame) -> None:
    """Bar chart: how many requests each backend served."""
    counts = (df[df["backend"] != "error"]["backend"]
              .value_counts()
              .sort_index())
    ax.bar(counts.index, counts.values)
    ax.set_xlabel("backend")
    ax.set_ylabel("requests served")
    ax.set_title("Distribution across backends")
    for i, v in enumerate(counts.values):
        ax.text(i, v, str(v), ha="center", va="bottom", fontsize=9)


def _rolling_percentiles(ax, df: pd.DataFrame, window: str = "5s") -> None:
    """Rolling p50 / p95 latency -- the tail is what hurts users."""
    ok = df[df["backend"] != "error"].copy()
    ok = ok.set_index("timestamp").sort_index()

    p50 = ok["latency_ms"].rolling(window).median()
    p95 = ok["latency_ms"].rolling(window).quantile(0.95)

    ax.plot(p50.index, p50.values, label="p50", linewidth=1.5)
    ax.plot(p95.index, p95.values, label="p95", linewidth=1.5)
    ax.set_xlabel("time")
    ax.set_ylabel("latency (ms)")
    ax.set_title(f"Rolling latency percentiles ({window} window)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    # Slim down the date formatter -- defaults are needlessly verbose.
    ax.tick_params(axis="x", rotation=15)


# ---------------------------------------------------------------------------
#  Top-level orchestration
# ---------------------------------------------------------------------------
def make_figure(df: pd.DataFrame, out_path: str, title: str) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    _scatter_by_backend  (axes[0], df)
    _backend_distribution(axes[1], df)
    _rolling_percentiles (axes[2], df)
    if title:
        fig.suptitle(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"wrote {out_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Plot Prometheus workload CSVs")
    p.add_argument("inputs",  nargs="+", help="one or more CSV files")
    p.add_argument("--out",   default="prometheus_results.png",
                   help="output image path")
    p.add_argument("--title", default="",
                   help="figure suptitle")
    args = p.parse_args()

    df = load_logs(args.inputs)
    if df.empty:
        sys.exit("no rows loaded; nothing to plot")

    # Sanity report to stderr -- handy when the figure looks odd.
    n_total  = len(df)
    n_errors = (df["backend"] == "error").sum()
    print(f"loaded {n_total} rows ({n_errors} errors) "
          f"from {df['source_file'].nunique()} file(s)", file=sys.stderr)

    make_figure(df, args.out, args.title)


if __name__ == "__main__":
    main()
