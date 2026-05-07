#!/usr/bin/env python3
"""
prometheus_topo.py
==================
Mininet topology for Project Prometheus -- a single-switch star topology
that mirrors the design diagram exactly:

    h1..h6  -->  s1  <-->  c0  (remote OpenFlow controller)
                 |
              srv1..srv3

The switch s1 is the only data-plane element; all client and server hosts
attach directly to it. The controller c0 runs as a *remote* Ryu process,
which is the natural fit for CMPE 210 / Ryu-based development workflows.

Run standalone:
    sudo python3 -m topology.prometheus_topo            # default CLI mode
    sudo python3 -m topology.prometheus_topo --pingall  # quick sanity check

Run with the QoS controller (recommended -- two terminals):
    Terminal A:  ryu-manager controller.qos_load_balancer
    Terminal B:  sudo python3 -m topology.prometheus_topo
"""

# Standard library
import argparse
import logging

# Mininet primitives
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.topo import Topo


# ---------------------------------------------------------------------------
#  Topology definition
# ---------------------------------------------------------------------------
class PrometheusTopo(Topo):
    """
    Star topology: one OF switch, N client hosts, M backend servers.

    The class is intentionally parametric so that the same topology file
    can drive small (default) experiments and larger stress tests without
    edits -- only constructor arguments change.
    """

    # The 10.0.0.0/24 block is split into three logical zones so that
    # tcpdump traces and Ryu logs are immediately readable:
    #   10.0.0.1   - 10.0.0.99   : clients   (h1..hN)
    #   10.0.0.100              : Virtual IP (VIP, advertised by controller)
    #   10.0.0.201 - 10.0.0.2xx : backends   (srv1..srvM)
    CLIENT_SUBNET_BASE   = "10.0.0."
    CLIENT_HOST_OFFSET   = 1       # h1 -> 10.0.0.1
    SERVER_HOST_OFFSET   = 201     # srv1 -> 10.0.0.201
    DEFAULT_LINK_BW_MBPS = 10      # cap host-switch links so QoS is observable

    def build(self, n_clients: int = 6, n_servers: int = 3,
              link_bw: int = DEFAULT_LINK_BW_MBPS) -> None:
        """
        Construct the topology graph. `Topo.build()` is called automatically
        by Mininet during `Topo.__init__`; do not call it directly.
        """
        # ---- Single OpenFlow switch (matches s1 in the diagram) ----------
        switch = self.addSwitch(
            "s1",
            cls=OVSSwitch,
            protocols="OpenFlow13",  # match the controller; 1.3 is required
                                     # for meter-based QoS enforcement later
        )

        # ---- Client hosts h1..hN -----------------------------------------
        # Use a small helper to keep the loop body declarative.
        for i in range(1, n_clients + 1):
            host = self.addHost(
                f"h{i}",
                ip=f"{self.CLIENT_SUBNET_BASE}{self.CLIENT_HOST_OFFSET + i - 1}/24",
            )
            # TCLink lets us shape bandwidth and inject delay/loss later
            # (essential for the "Stress Test" phase in your work plan).
            self.addLink(host, switch, bw=link_bw)

        # ---- Backend servers srv1..srvM ----------------------------------
        for j in range(1, n_servers + 1):
            srv = self.addHost(
                f"srv{j}",
                ip=f"{self.CLIENT_SUBNET_BASE}{self.SERVER_HOST_OFFSET + j - 1}/24",
            )
            self.addLink(srv, switch, bw=link_bw)


# ---------------------------------------------------------------------------
#  Network bring-up helper
# ---------------------------------------------------------------------------
def launch(n_clients: int = 6, n_servers: int = 3,
           controller_ip: str = "127.0.0.1", controller_port: int = 6653,
           pingall: bool = False, drop_to_cli: bool = True) -> Mininet:
    """
    Instantiate the topology, attach a remote controller, and either drop
    into the Mininet CLI or run an automated sanity check.

    Returns the Mininet instance so callers (e.g. integration tests) can
    drive it programmatically.
    """
    topo = PrometheusTopo(n_clients=n_clients, n_servers=n_servers)

    # `RemoteController` tells the OVS switches to dial out to Ryu.
    # When running on CloudLab, point controller_ip at the controller node.
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        controller=lambda name: RemoteController(
            name, ip=controller_ip, port=controller_port
        ),
        link=TCLink,
        autoSetMacs=True,   # deterministic MACs make Ryu logs grep-friendly
        autoStaticArp=True, # avoid ARP storms muddying QoS measurements
    )

    net.start()

    # Force OF1.3 on every switch -- belt-and-braces, since some OVS
    # builds default to 1.0 even when the topology asks for 1.3.
    for sw in net.switches:
        sw.cmd(f"ovs-vsctl set bridge {sw.name} protocols=OpenFlow13")

    if pingall:
        # Useful as a smoke test before launching real workloads.
        # Note: with the QoS controller running, the VIP (10.0.0.100) will
        # *not* answer pings unless the controller treats ICMP as a flow --
        # the diagnostic here is host<->host reachability only.
        net.pingAll()

    if drop_to_cli:
        CLI(net)
        net.stop()

    return net


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Project Prometheus Mininet topology")
    p.add_argument("--clients",        type=int, default=6,
                   help="number of client hosts (default: 6, matching the diagram)")
    p.add_argument("--servers",        type=int, default=3,
                   help="number of backend servers (default: 3, matching the diagram)")
    p.add_argument("--controller-ip",  default="127.0.0.1",
                   help="IP address of the remote Ryu controller")
    p.add_argument("--controller-port", type=int, default=6653,
                   help="TCP port of the remote Ryu controller (default: 6653)")
    p.add_argument("--pingall",        action="store_true",
                   help="run pingAll after bring-up as a smoke test")
    p.add_argument("--no-cli",         action="store_true",
                   help="exit immediately instead of dropping into the Mininet CLI")
    return p.parse_args()


if __name__ == "__main__":
    setLogLevel("info")
    logging.basicConfig(level=logging.INFO,
                        format="[topo] %(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    launch(
        n_clients       = args.clients,
        n_servers       = args.servers,
        controller_ip   = args.controller_ip,
        controller_port = args.controller_port,
        pingall         = args.pingall,
        drop_to_cli     = not args.no_cli,
    )
