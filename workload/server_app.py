#!/usr/bin/env python3
"""
server_app.py
=============
Minimal TCP echo server with optional artificial latency. Runs on each
backend host (srv1..srv3) so we have something to load-balance traffic to.

Why a custom server instead of, say, `python3 -m http.server`?
    * We want *deterministic* response times so QoS metrics under load
      reflect the controller's behaviour, not Python's HTTP machinery.
    * Adding a `--latency` knob lets us simulate a "slow" backend on
      command -- crucial for showing that the QoS-weighted strategy
      actually shifts traffic away from degraded servers.

Usage (run *inside* a Mininet host shell):
    srv1 python3 -m workload.server_app --port 8080 --name srv1 &
    srv2 python3 -m workload.server_app --port 8080 --name srv2 --latency 50 &
    srv3 python3 -m workload.server_app --port 8080 --name srv3 &
"""

# Standard library
import argparse
import socket
import threading
import time


# ---------------------------------------------------------------------------
#  Per-connection handler
# ---------------------------------------------------------------------------
def _handle_connection(conn: socket.socket, addr: tuple,
                       name: str, latency_ms: float) -> None:
    """
    Echo loop. Sleeps `latency_ms` before each reply -- this is what gives
    the load balancer something interesting to measure during stress tests.
    """
    try:
        with conn:
            conn.sendall(f"hello from {name}\n".encode())
            while True:
                data = conn.recv(4096)
                if not data:
                    return  # client closed cleanly
                if latency_ms > 0:
                    time.sleep(latency_ms / 1000.0)
                # Tag the response so the client can verify which backend
                # actually served the request (useful for distribution audits).
                conn.sendall(f"[{name}] {data.decode(errors='replace')}".encode())
    except (ConnectionResetError, BrokenPipeError):
        # Normal under stress -- clients yank connections all the time.
        pass


# ---------------------------------------------------------------------------
#  Accept loop
# ---------------------------------------------------------------------------
def serve(host: str, port: int, name: str, latency_ms: float) -> None:
    """Accept forever. One thread per connection -- fine for our scale."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(64)
    print(f"[{name}] listening on {host}:{port} (latency={latency_ms}ms)",
          flush=True)

    while True:
        conn, addr = sock.accept()
        # daemon=True -> threads die with the process; no graceful shutdown
        # needed for a Mininet experiment harness.
        threading.Thread(
            target=_handle_connection,
            args=(conn, addr, name, latency_ms),
            daemon=True,
        ).start()


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Prometheus backend echo server")
    p.add_argument("--host",    default="0.0.0.0",
                   help="bind address (default: all interfaces)")
    p.add_argument("--port",    type=int, default=8080,
                   help="TCP port (default: 8080)")
    p.add_argument("--name",    default="srv?",
                   help="server identifier embedded in responses")
    p.add_argument("--latency", type=float, default=0.0,
                   help="artificial response delay in ms (default: 0)")
    args = p.parse_args()
    serve(args.host, args.port, args.name, args.latency)
