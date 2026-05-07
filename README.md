# Project Prometheus — QoS-Aware Load-Balancing VNF

Mininet + CloudLab simulator for *Intelligent QoS-Aware Load Balancing for
Virtualized Network Functions*.

The system implements a single-switch SDN load balancer. Six client hosts
connect to a Virtual IP (VIP); a Ryu controller intercepts each new flow,
consults a pluggable balancing strategy, and installs symmetric flow rules
that map the flow onto one of three backend servers. QoS metrics
(per-backend RTT, link utilisation, error-derived loss) are collected
continuously and fed back into the strategy.

## Layout

```
prometheus/
├── topology/
│   ├── prometheus_topo.py       # Mininet topology (matches the diagram)
│   └── cloudlab_profile.py      # CloudLab geni-lib RSpec
├── controller/
│   ├── qos_load_balancer.py     # Ryu app: PacketIn handling, flow-mod, ARP-VIP
│   ├── balancing_strategies.py  # round_robin / least_connections / random / qos_weighted
│   └── metrics_collector.py     # OFPPortStats poller, EWMA smoothing
├── workload/
│   ├── server_app.py            # Backend echo server (artificial-latency knob)
│   └── traffic_generator.py     # Client driver: steady / burst / closed
├── analysis/
│   └── plot_metrics.py          # 3-panel summary figure
└── scripts/
    ├── run_experiment.sh        # End-to-end runner (calls everything below)
    └── drive_topology.py        # Programmatic Mininet harness
```

## Quick start (local Mininet)

Prereqs: Mininet, Open vSwitch (1.3 capable), Ryu, Python 3.8+, matplotlib,
pandas.

```bash
# One-shot: spins up controller, topology, servers, clients; saves CSVs+plot.
sudo ./scripts/run_experiment.sh burst 30
```

Or step by step in two terminals:

```bash
# Terminal A — controller
ryu-manager controller.qos_load_balancer

# Terminal B — topology (drops into Mininet CLI)
sudo python3 -m topology.prometheus_topo
```

From the Mininet CLI you can sanity-check connectivity:

```
mininet> h1 ping -c 2 10.0.0.100        # ping the VIP -- handled by proxy-ARP + LB
mininet> srv1 python3 -m workload.server_app --name srv1 &
mininet> h1   curl 10.0.0.100:8080
```

## Switching strategies

Edit the constants at the top of `controller/qos_load_balancer.py`:

```python
STRATEGY = "qos_weighted"   # or: "round_robin", "least_connections", "random_choice"
STRATEGY_KWARGS = dict(w_lat=0.5, w_loss=0.3, w_util=0.2)
```

For your evaluation phase, re-run `run_experiment.sh` once per strategy
and compare the `summary.png` outputs side-by-side.

## CloudLab deployment

Upload `topology/cloudlab_profile.py` as a CloudLab profile. Defaults
allocate two `d430` nodes (controller + Mininet); flip the
`physical_backends` parameter to spin srv1/2/3 onto separate nodes for
true hardware-level QoS measurement.

After instantiation, ssh into the Mininet node and run the same
`scripts/run_experiment.sh` you'd use locally — the controller IP
defaults to localhost, override with `--controller-ip <ctrl-node-ip>`
in `prometheus_topo.py` if you put Ryu on the dedicated controller node.

## Mapping to the project plan

| Phase from PDF                         | Where it lives                                              |
|----------------------------------------|-------------------------------------------------------------|
| W1 Literature review                   | (out of scope here)                                         |
| W2 Design                              | This README + `prometheus_topo.py`, `cloudlab_profile.py`   |
| W3 Implementation in simulated env     | `controller/`, `workload/`                                  |
| W4 Testing & evaluation                | `scripts/run_experiment.sh`, `analysis/plot_metrics.py`     |
| W5 Validation & documentation          | CSV logs in `results/`, comparison plots                    |
| W6 Final demo                          | Live `run_experiment.sh burst 60` with strategy A/B         |

## Things you'll want to extend

- **Meter-based bandwidth caps**: OF1.3 meters can enforce per-tenant
  rate limits at the switch; hook them into `_install_forward_flow`.
- **Active probes for loss**: the current loss proxy is OFPortStats
  errors, which underestimates real loss. Adding a UDP-echo probe
  (similar to the existing ICMP RTT loop) is one extra method.
- **Horizontal scaling trigger**: detect "all backends > threshold
  utilisation" in `metrics_collector` and emit a callback the Ryu
  app can use to spin up an additional `srv4` host.

## Footnote on assumptions

The MAC addresses in `BACKEND_POOL` assume Mininet's `autoSetMacs=True`
allocation order: `h1=00:00:...:01`, ..., `h6=06`, `srv1=07`, `srv2=08`,
`srv3=09`. If you change the host count or order, update the pool.
