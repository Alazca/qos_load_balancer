#!/usr/bin/env python3
"""
traffic_generator.py
====================
Workload driver. Issues TCP requests from a client host (h1..h6) to the
Virtual IP. Every reply is parsed for its `[srvN]` tag, giving us the
distribution audit needed to evaluate the load balancer.

Three operating modes:

  * `steady`  -- N requests/sec for a fixed duration.
                 Good for the "Performance Test -- Baseline" phase.

  * `burst`   -- alternates between low-load and high-load windows.
                 Good for the "Stress Test -- Sudden traffic jumps" phase.

  * `closed`  -- closed-loop: K concurrent virtual users, each issuing
                 back-to-back requests. Closer to a real client mix.

Outputs CSV to stdout:
    timestamp_iso,client,latency_ms,backend,bytes,error

Pipe into `analysis/metric_logger.py` (or just `tee` into a file) and
plot with `analysis/plot_metrics.py`.

Usage examples (run *inside* a Mininet host shell):
    h1 python3 -m workload.traffic_generator --vip 10.0.0.100 \
       --mode steady --rate 20 --duration 30 > /tmp/h1.csv
    h2 python3 -m workload.traffic_generator --vip 10.0.0.100 \
       --mode burst  --duration 60 > /tmp/h2.csv
"""

# Standard library
import argparse
import csv
import random
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
#  Single-request primitive
# ---------------------------------------------------------------------------
_BACKEND_TAG_RE = re.compile(rb"\[(srv\d+)\]")


def _one_request(vip: str, port: int, payload: bytes,
                 timeout_s: float) -> tuple:
    """
    Open a TCP connection, send `payload`, read until EOF or timeout.
    Returns (latency_ms, backend_name, bytes_received, error_or_None).
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((vip, port), timeout=timeout_s) as s:
            s.sendall(payload)
            # Half-close to signal "done sending" -- lets the server drop
            # its loop cleanly on EOF rather than waiting for our timeout.
            s.shutdown(socket.SHUT_WR)

            chunks = bytearray()
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks.extend(data)
        latency_ms = (time.perf_counter() - start) * 1000.0

        # Extract backend tag (e.g. b"[srv2]") from the response.
        match   = _BACKEND_TAG_RE.search(chunks)
        backend = match.group(1).decode() if match else "unknown"

        return latency_ms, backend, len(chunks), None

    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return latency_ms, "error", 0, repr(e)


# ---------------------------------------------------------------------------
#  Mode: steady
# ---------------------------------------------------------------------------
def _steady_mode(args, writer) -> None:
    """Open-loop: fire `rate` requests per second for `duration` seconds."""
    interval = 1.0 / args.rate
    deadline = time.time() + args.duration
    seq      = 0

    while time.time() < deadline:
        seq += 1
        payload = f"req {args.client_id} #{seq}\n".encode()
        ts      = datetime.now(timezone.utc).isoformat()

        # Run each request in its own thread so a slow backend doesn't
        # throttle the offered load -- otherwise we'd be measuring our
        # own client, not the system under test.
        threading.Thread(
            target=_emit_row,
            args=(writer, ts, args.client_id, args.vip, args.port,
                  payload, args.timeout),
            daemon=True,
        ).start()

        time.sleep(interval)


# ---------------------------------------------------------------------------
#  Mode: burst
# ---------------------------------------------------------------------------
def _burst_mode(args, writer) -> None:
    """
    Alternate between LOW (5 req/s) and HIGH (50 req/s) phases of 10 s each.
    Designed to surface adaptation latency in the controller's response.
    """
    LOW, HIGH      = 5.0, 50.0
    PHASE_LENGTH_S = 10.0

    deadline = time.time() + args.duration
    seq      = 0
    high     = False
    phase_ends_at = time.time() + PHASE_LENGTH_S

    while time.time() < deadline:
        if time.time() >= phase_ends_at:
            high           = not high
            phase_ends_at  = time.time() + PHASE_LENGTH_S

        rate     = HIGH if high else LOW
        interval = 1.0 / rate
        seq     += 1
        payload  = f"burst {args.client_id} #{seq}\n".encode()
        ts       = datetime.now(timezone.utc).isoformat()

        threading.Thread(
            target=_emit_row,
            args=(writer, ts, args.client_id, args.vip, args.port,
                  payload, args.timeout),
            daemon=True,
        ).start()
        time.sleep(interval)


# ---------------------------------------------------------------------------
#  Mode: closed
# ---------------------------------------------------------------------------
def _closed_mode(args, writer) -> None:
    """
    Closed-loop: `concurrency` virtual users, each issuing requests as
    fast as it can. Models a fixed user population.
    """
    deadline = time.time() + args.duration
    barrier  = threading.Barrier(args.concurrency)

    def _vuser(uid: int) -> None:
        # Stagger startups so all VUs don't hammer at t=0
        time.sleep(random.uniform(0.0, 0.5))
        barrier.wait()

        seq = 0
        while time.time() < deadline:
            seq    += 1
            payload = f"vu{uid} #{seq}\n".encode()
            ts      = datetime.now(timezone.utc).isoformat()
            _emit_row(writer, ts, f"{args.client_id}/vu{uid}",
                      args.vip, args.port, payload, args.timeout)

    threads = [threading.Thread(target=_vuser, args=(i,), daemon=True)
               for i in range(args.concurrency)]
    for t in threads: t.start()
    for t in threads: t.join()


# ---------------------------------------------------------------------------
#  Shared row emitter (thread-safe via the underlying CSV writer)
# ---------------------------------------------------------------------------
_WRITER_LOCK = threading.Lock()


def _emit_row(writer, ts, client, vip, port, payload, timeout) -> None:
    """One request -> one CSV row. Used by all three modes."""
    latency, backend, n_bytes, err = _one_request(vip, port, payload, timeout)
    with _WRITER_LOCK:
        writer.writerow([ts, client, f"{latency:.3f}", backend, n_bytes,
                         err or ""])
        sys.stdout.flush()


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Prometheus traffic generator")
    p.add_argument("--vip",        default="10.0.0.100",
                   help="virtual IP advertised by the load balancer")
    p.add_argument("--port",       type=int, default=8080,
                   help="TCP port on the VIP (default: 8080)")
    p.add_argument("--mode",       choices=("steady", "burst", "closed"),
                   default="steady",
                   help="workload pattern (default: steady)")
    p.add_argument("--rate",       type=float, default=10.0,
                   help="[steady] requests per second")
    p.add_argument("--concurrency", type=int, default=8,
                   help="[closed]  concurrent virtual users")
    p.add_argument("--duration",   type=float, default=30.0,
                   help="total runtime in seconds")
    p.add_argument("--timeout",    type=float, default=2.0,
                   help="per-request timeout in seconds")
    p.add_argument("--client-id",  default=socket.gethostname(),
                   help="label to embed in CSV rows (default: hostname)")
    args = p.parse_args()

    writer = csv.writer(sys.stdout)
    writer.writerow(["timestamp", "client", "latency_ms", "backend",
                     "bytes", "error"])

    dispatch = {
        "steady": _steady_mode,
        "burst":  _burst_mode,
        "closed": _closed_mode,
    }
    dispatch[args.mode](args, writer)


if __name__ == "__main__":
    main()
