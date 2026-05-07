#!/usr/bin/env python3
"""
SDN Load-Balancing Live Demo Runner
====================================

Drives the CMPE 210 load-balancing demo without juggling four terminals.
Brings up the topology, walks through the no-LB baseline and the LB
scenario, and tears everything down cleanly when you quit.

Prerequisites
-------------
* `ryu-manager ryu_lb.py` is already running in another terminal/pane.
* `lb_topo.py` lives in the same directory as this script and exposes
  `topos = {'lbtopo': LBTopo}` (the same hook `mn --custom` uses).
* Run as root: `sudo python3 lb_demo.py`.

Design notes
------------
* `LoadBalanceDemo` is a context manager so an exception during a scenario
  still triggers a full Mininet teardown -- no orphaned OVS bridges.
* xterm is only used if $DISPLAY is set; otherwise tcpdump output goes to
  log files under /tmp/sdn_demo. Same script works on a bare SSH session.
* Server MAC addresses, the virtual IP, and the direct srv1 IP are pulled
  from a single config block at the top so they stay in sync with
  ryu_lb.py without hunting through the script.
"""

import importlib.util
import os
import re
import sys
import time
from pathlib import Path

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController


# ---------- Configuration ----------------------------------------------------
# Keep these mirrored with whatever ryu_lb.py expects.

CONTROLLER_IP   = "127.0.0.1"
CONTROLLER_PORT = 6653

# Pinned server MACs -- the controller's flow rules key on these.
SERVER_MACS = {
    "srv1": "00:00:00:00:00:05",
    "srv2": "00:00:00:00:00:06",
    "srv3": "00:00:00:00:00:09",
}

VIRTUAL_IP     = "10.0.0.100"  # LB VIP rewritten by the controller
SRV1_DIRECT_IP = "10.0.0.5"    # baseline target (no LB in the path)

LOG_DIR  = Path("/tmp/sdn_demo")
TOPO_PY  = Path(__file__).parent / "lb_topo.py"
TOPO_KEY = "lbtopo"  # matches `mn --topo lbtopo`


# ---------- Small helpers ----------------------------------------------------

def banner(title):
    """Loud section break -- nice for live demos."""
    bar = "=" * 70
    print(f"\n{bar}\n  {title}\n{bar}\n", flush=True)


def have_display():
    """True when xterm windows will actually render (X-forwarded SSH or local)."""
    return bool(os.environ.get("DISPLAY"))


def load_topo_class():
    """
    Mirror what `mn --custom lb_topo.py --topo lbtopo` does internally:
    load the file, then look up `topos[TOPO_KEY]`.
    """
    spec = importlib.util.spec_from_file_location("lb_topo", TOPO_PY)
    if spec is None or spec.loader is None:
        sys.exit(f"ERROR: cannot import {TOPO_PY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.topos[TOPO_KEY]()
    except (AttributeError, KeyError):
        sys.exit(f"ERROR: {TOPO_PY} must define topos = {{'{TOPO_KEY}': <Topo>}}")


# Linux ping summary line, e.g.:
#   rtt min/avg/max/mdev = 0.083/0.456/2.123/0.567 ms
_RTT_RE = re.compile(
    r"rtt\s+min/avg/max(?:/mdev)?\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)"
)


def parse_ping_rtt(text):
    """Return (min_ms, avg_ms, max_ms) from a ping log, or None if not found."""
    m = _RTT_RE.search(text)
    if not m:
        return None
    return tuple(float(x) for x in m.groups())


# ---------- Demo orchestrator ------------------------------------------------

class LoadBalanceDemo:
    """
    Lifecycle:
        with LoadBalanceDemo() as demo:
            demo.scenario_no_lb()
            demo.scenario_with_lb()
    """

    def __init__(self):
        self.net = None
        self.rtt_results = {}  # scenario label -> (min_ms, avg_ms, max_ms)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    # --- context manager ------------------------------------------------------

    def __enter__(self):
        self._build_network()
        return self

    def __exit__(self, *_exc):
        self._teardown()

    # --- setup / teardown -----------------------------------------------------

    def _build_network(self):
        """Spin up the topology and let the controller handshake settle."""
        banner("Building topology")

        self.net = Mininet(
            topo=load_topo_class(),
            controller=lambda name: RemoteController(
                name, ip=CONTROLLER_IP, port=CONTROLLER_PORT
            ),
            switch=OVSKernelSwitch,
            link=TCLink,
            autoSetMacs=False,  # we set server MACs explicitly per-scenario
        )
        self.net.start()

        # Force OF1.3 on every switch -- same as `--switch ovsk,protocols=OpenFlow13`.
        for sw in self.net.switches:
            sw.cmd(f"ovs-vsctl set bridge {sw.name} protocols=OpenFlow13")

        info("*** Waiting 2s for controller handshake...\n")
        time.sleep(2)

    def _teardown(self):
        """Kill any background iperf/tcpdump first, then stop Mininet."""
        banner("Tearing down")
        if self.net is not None:
            for h in self.net.hosts:
                h.cmd("pkill -9 iperf  2>/dev/null")
                h.cmd("pkill -9 tcpdump 2>/dev/null")
            self.net.stop()

    # --- shared helpers -------------------------------------------------------

    def _set_server_macs(self, server_names):
        """Pin server MACs so the controller's match fields line up."""
        for name in server_names:
            mac = SERVER_MACS[name]
            self.net.get(name).cmd(f"ifconfig {name}-eth0 hw ether {mac}")
            info(f"*** {name} MAC -> {mac}\n")

    def _start_iperf_server(self, host_name):
        """Background iperf -s on a host, log to /tmp/sdn_demo."""
        log = LOG_DIR / f"iperf_server_{host_name}.log"
        self.net.get(host_name).cmd(f"iperf -s > {log} 2>&1 &")
        info(f"*** iperf -s on {host_name} (log: {log})\n")

    def _start_tcpdump(self, host_name):
        """
        Visualize traffic per server. xterm if we have a DISPLAY,
        else stream to a log file so SSH-only sessions still work.
        """
        h     = self.net.get(host_name)
        iface = f"{host_name}-eth0"
        log   = LOG_DIR / f"tcpdump_{host_name}.log"

        if have_display():
            h.cmd(f"xterm -T 'tcpdump {host_name}' -e tcpdump -i {iface} -n &")
            info(f"*** tcpdump xterm: {host_name}\n")
        else:
            h.cmd(f"tcpdump -i {iface} -n -l > {log} 2>&1 &")
            info(f"*** tcpdump -> {log}\n")

    def _run_clients(self, clients, target_ip, duration):
        """
        Launch every iperf client in parallel (each backgrounded), then block
        for the test duration plus a small grace window.
        """
        info(f"*** {len(clients)} clients -> {target_ip} for {duration}s\n")
        for c in clients:
            log = LOG_DIR / f"iperf_client_{c}.log"
            self.net.get(c).cmd(
                f"iperf -c {target_ip} -t {duration} > {log} 2>&1 &"
            )
        time.sleep(duration + 2)
        info("*** clients done -- check /tmp/sdn_demo/iperf_client_*.log\n")

    def _summarize_ping(self, label, log_path, wait_timeout=30):
        """
        Wait for the rtt summary line to appear in `log_path`, parse it,
        store it under `label`, and print the min/avg/max in ms.
        Falls through gracefully if the summary never shows up.
        """
        info(f"*** Waiting for ping summary ({label})...\n")
        deadline = time.time() + wait_timeout
        rtt = None
        while time.time() < deadline:
            if log_path.exists():
                rtt = parse_ping_rtt(log_path.read_text())
                if rtt is not None:
                    break
            time.sleep(0.5)

        if rtt is None:
            print(f"\n[{label}] no ping summary found -- check {log_path}\n")
            return

        mn, avg, mx = rtt
        self.rtt_results[label] = rtt
        print(
            f"\n[{label}]  RTT  min={mn:.3f} ms   "
            f"avg={avg:.3f} ms   max={mx:.3f} ms\n"
        )

    # --- scenarios ------------------------------------------------------------

    def scenario_no_lb(self, duration=30):
        """Baseline: every flow lands on srv1 directly. No balancing involved."""
        banner("SCENARIO 1 - No load balancing (all traffic to srv1)")

        self._set_server_macs(["srv1"])
        self._start_tcpdump("srv1")
        self._start_iperf_server("srv1")

        # h4 ping in the background -- audience-friendly liveness signal.
        self.net.get("h4").cmd(
            f"ping -c 20 {SRV1_DIRECT_IP} > {LOG_DIR}/ping_h4.log 2>&1 &"
        )

        self._run_clients(
            clients=["h1", "h2", "h3"],
            target_ip=SRV1_DIRECT_IP,
            duration=duration,
        )
        self._summarize_ping("no-LB  (h4 -> srv1)", LOG_DIR / "ping_h4.log")

    def scenario_with_lb(self, duration=20):
        """Load balanced: clients hit the VIP, controller spreads them across srv1/2/3."""
        banner("SCENARIO 2 - Load balancing across srv1 / srv2 / srv3")

        servers = ["srv1", "srv2", "srv3"]
        self._set_server_macs(servers)
        for s in servers:
            self._start_tcpdump(s)
            self._start_iperf_server(s)

        self.net.get("h6").cmd(
            f"ping -c 20 {VIRTUAL_IP} > {LOG_DIR}/ping_h6.log 2>&1 &"
        )

        self._run_clients(
            clients=["h1", "h2", "h3", "h4", "h5"],
            target_ip=VIRTUAL_IP,
            duration=duration,
        )
        self._summarize_ping("with-LB (h6 -> VIP)", LOG_DIR / "ping_h6.log")

    def print_comparison(self):
        """Side-by-side RTT for whichever scenarios have run so far."""
        banner("RTT comparison  (lower is better)")
        if not self.rtt_results:
            print("No scenarios have completed yet -- run 1 and/or 2 first.\n")
            return

        cols = (("scenario", 22), ("min (ms)", 12),
                ("avg (ms)", 12), ("max (ms)", 12))
        header = "".join(f"{name:<{w}}" if i == 0 else f"{name:>{w}}"
                         for i, (name, w) in enumerate(cols))
        print(header)
        print("-" * len(header))
        for label, (mn, avg, mx) in self.rtt_results.items():
            print(f"{label:<22}{mn:>12.3f}{avg:>12.3f}{mx:>12.3f}")
        print()

    def drop_to_cli(self):
        """Hand control to the user for ad-hoc poking during the demo."""
        banner("Mininet CLI (Ctrl-D to return to menu)")
        CLI(self.net)


# ---------- Interactive menu -------------------------------------------------

MENU = """
Choose a scenario:
  1) No load balance    (baseline -- srv1 only, h1-h3 + h4 ping)
  2) With load balance  (srv1+srv2+srv3 via VIP, h1-h5 + h6 ping)
  3) Compare RTT        (side-by-side min/avg/max from prior runs)
  4) Mininet CLI        (manual exploration)
  q) Quit
"""


def main():
    if os.geteuid() != 0:
        sys.exit("ERROR: Mininet needs root. Re-run with sudo.")

    setLogLevel("info")

    with LoadBalanceDemo() as demo:
        while True:
            print(MENU)
            choice = input("> ").strip().lower()
            if   choice == "1":           demo.scenario_no_lb()
            elif choice == "2":           demo.scenario_with_lb()
            elif choice == "3":           demo.print_comparison()
            elif choice == "4":           demo.drop_to_cli()
            elif choice in ("q", "quit"): break
            else:                         print(f"Unknown choice: {choice!r}")


if __name__ == "__main__":
    main() 
