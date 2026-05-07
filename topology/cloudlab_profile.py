"""
cloudlab_profile.py
===================
CloudLab geni-lib RSpec for Project Prometheus -- "hardware emulation"
deployment as called for in the project test plan.

How CloudLab uses this file
---------------------------
CloudLab profiles are Python scripts that emit an XML RSpec when CloudLab
imports them. You upload this file via the web UI:

    Experiments  ->  Create Experiment Profile  ->  source: this file

CloudLab itself runs the script in its sandboxed `geni-lib` environment;
we only need to make sure the emitted RSpec describes:

    * One controller node    (runs Ryu + the controller package)
    * One Mininet  node      (runs the Mininet topology)
    * Optionally, separate emulator nodes for backends (commented out
      below; flip `USE_PHYSICAL_BACKENDS = True` to enable that mode).

A single LAN connects them so the OpenFlow control channel and the
in-band data plane can both reach across nodes if needed.

Local-test note
---------------
You cannot run this script directly with python3 -- it relies on the
CloudLab `geni` library that's only present in CloudLab's portal. To
syntax-check locally without that dependency, use:

    python3 -m py_compile topology/cloudlab_profile.py

(Local Mininet runs work fine without ever touching this file.)
"""

# CloudLab geni-lib (only available inside the portal -- linters will
# complain locally; that's expected).
import geni.portal           as portal     # type: ignore
import geni.rspec.pg         as pg         # type: ignore
import geni.rspec.igext      as IG         # type: ignore


# ---------------------------------------------------------------------------
#  Profile-level knobs (exposed in the CloudLab UI as parameters)
# ---------------------------------------------------------------------------
pc = portal.Context()

pc.defineParameter(
    "node_count_clients",
    "Number of client hosts (default 6, matches the design diagram)",
    portal.ParameterType.INTEGER, 6,
)
pc.defineParameter(
    "node_count_servers",
    "Number of backend servers (default 3, matches the design diagram)",
    portal.ParameterType.INTEGER, 3,
)
pc.defineParameter(
    "physical_backends",
    "Use separate physical nodes for backends instead of Mininet hosts? "
    "Slower to allocate but gives true hardware isolation for QoS measurement.",
    portal.ParameterType.BOOLEAN, False,
)
pc.defineParameter(
    "hw_type",
    "CloudLab hardware type for all nodes (e.g. d430, c220g2, m400). "
    "Pick a class your project allocation supports.",
    portal.ParameterType.STRING, "d430",
)

params = pc.bindParameters()
request = pc.makeRequestRSpec()


# ---------------------------------------------------------------------------
#  Helper: build a stock Ubuntu node with our setup script installed
# ---------------------------------------------------------------------------
# CloudLab images change names from time to time; this URN points at a
# recent Ubuntu LTS image with kernel modules suitable for OVS+Mininet.
DEFAULT_IMAGE = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"


def _make_node(name: str, role: str) -> pg.RawPC:
    """
    Allocate one bare-metal node, install the role-specific bootstrap
    script, and return the node object so the caller can attach it to a
    LAN.

    `role` ∈ {"controller", "mininet", "backend"} -- selects which
    package set the node installs at boot.
    """
    node = request.RawPC(name)
    node.disk_image = DEFAULT_IMAGE
    node.hardware_type = params.hw_type

    # Bootstrap script per role. Kept inline (not a separate file) so the
    # profile is self-contained when uploaded to CloudLab.
    if role == "controller":
        cmd = (
            "set -eux; "
            "sudo apt-get update; "
            "sudo apt-get install -y python3-pip git tcpdump iperf3; "
            "sudo pip3 install ryu eventlet==0.30.2 ; "  # eventlet pin: Ryu compat
            "git clone https://example.invalid/your/prometheus.git "
            "    /opt/prometheus || true; "
        )
    elif role == "mininet":
        cmd = (
            "set -eux; "
            "sudo apt-get update; "
            "sudo apt-get install -y mininet openvswitch-switch "
            "    python3-pip tcpdump iperf3 git; "
            "sudo pip3 install matplotlib pandas; "
            "git clone https://example.invalid/your/prometheus.git "
            "    /opt/prometheus || true; "
        )
    else:  # backend
        cmd = (
            "set -eux; "
            "sudo apt-get update; "
            "sudo apt-get install -y python3 iperf3 tcpdump; "
        )

    node.addService(pg.Execute(shell="bash", command=cmd))
    return node


# ---------------------------------------------------------------------------
#  Build the topology
# ---------------------------------------------------------------------------
# A single LAN connects everything, mirroring the star topology of s1.
lan = request.LAN("prom_lan")

controller_node = _make_node("controller", "controller")
lan.addInterface(controller_node.addInterface("if0"))

mininet_node    = _make_node("mininet",    "mininet")
lan.addInterface(mininet_node.addInterface("if0"))

if params.physical_backends:
    # "Hardware emulation" mode: backends are real CloudLab nodes, not
    # Mininet hosts. More realistic but consumes the project allocation
    # faster, so it's off by default.
    for j in range(1, params.node_count_servers + 1):
        srv = _make_node(f"srv{j}", "backend")
        lan.addInterface(srv.addInterface("if0"))


# ---------------------------------------------------------------------------
#  Emit the RSpec
# ---------------------------------------------------------------------------
pc.printRequestRSpec(request)
