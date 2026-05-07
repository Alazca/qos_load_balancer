#!/usr/bin/env python3
"""
drive_topology.py
=================
Starts the Mininet topology, launches a backend echo server on each
srv host, drives the workload generator on each client host, waits for
results, and shuts down cleanly.

This is what `run_experiment.sh` calls -- keeping the orchestration in
Python (not bash) means we can talk to Mininet's API directly rather
than poking the CLI through stdin pipes.
"""

# Standard library
import argparse
import os
import sys
import time
from pathlib import Path

# Allow running as a top-level script from `scripts/`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Project
from topology.prometheus_topo import launch  # noqa: E402

# Mininet (only needed for the CLI fall-through if --debug is set)
from mininet.cli import CLI       # noqa: E402
from mininet.log import setLogLevel  # noqa: E402


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------
def _start_backends(net, project_root: Path, results_dir: Path) -> None:
    """Launch one echo server per srv host, with srv2 deliberately slowed
    down so QoS-aware balancing has something interesting to react to."""
    for srv in [h for h in net.hosts if h.name.startswith("srv")]:
        # srv2 gets +50ms artificial latency -- this is the "degraded
        # backend" scenario in the test plan. Tweak freely for other
        # experiments.
        latency = 50 if srv.name == "srv2" else 0

        log = results_dir / f"{srv.name}.log"
        cmd = (
            f"cd {project_root} && "
            f"python3 -m workload.server_app "
            f"  --port 8080 --name {srv.name} --latency {latency} "
            f"  > {log} 2>&1 &"
        )
        srv.cmd(cmd)
        print(f"[drive] {srv.name} server up (latency={latency}ms)")

    # Tiny pause so accept() is ready before the first client knocks.
    time.sleep(1.0)


def _drive_clients(net, project_root: Path, results_dir: Path,
                   mode: str, duration: float) -> None:
    """Spawn a traffic generator on every h* host, all writing CSV in parallel."""
    for h in [h for h in net.hosts if h.name.startswith("h")]:
        csv = results_dir / f"{h.name}.csv"
        cmd = (
            f"cd {project_root} && "
            f"python3 -m workload.traffic_generator "
            f"  --vip 10.0.0.100 --port 8080 "
            f"  --mode {mode} --duration {duration} "
            f"  --client-id {h.name} "
            f"  > {csv} 2>&1 &"
        )
        h.cmd(cmd)
        print(f"[drive] {h.name} traffic-gen up (mode={mode})")


def _wait_for_clients(net, duration: float) -> None:
    """Block until the workload window has elapsed plus a small grace."""
    grace = 5.0
    print(f"[drive] running for {duration}s (+{grace}s grace)...")
    time.sleep(duration + grace)


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Drive a Prometheus experiment")
    p.add_argument("--mode",     default="burst",
                   choices=("steady", "burst", "closed"))
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--results",  required=True,
                   help="output directory (already created by the shell wrapper)")
    p.add_argument("--debug",    action="store_true",
                   help="drop into Mininet CLI after the workload completes")
    args = p.parse_args()

    setLogLevel("info")
    project_root = Path(__file__).resolve().parent.parent
    results_dir  = Path(args.results)

    # Bring up the network *without* dropping to the CLI -- we drive it
    # programmatically below.
    net = launch(drop_to_cli=False)

    try:
        _start_backends (net, project_root, results_dir)
        _drive_clients  (net, project_root, results_dir, args.mode, args.duration)
        _wait_for_clients(net, args.duration)

        if args.debug:
            CLI(net)

    finally:
        # Best-effort kill of leftover background jobs inside the namespaces;
        # net.stop() handles the namespaces themselves.
        for h in net.hosts:
            h.cmd("pkill -f traffic_generator || true")
            h.cmd("pkill -f server_app        || true")
        net.stop()


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("drive_topology.py: must be run as root (Mininet requirement)")
    main()
