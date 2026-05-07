"""
metrics_collector.py
====================
Periodic OpenFlow stats poller. Refreshes the dynamic fields on each
`Backend` (RTT, loss ratio, utilisation, active flow count) so the
strategy module can make informed decisions.

Responsibilities are intentionally narrow:
    * Send OFPPortStatsRequest at a fixed cadence
    * Compute deltas across polls -> bytes/sec -> utilisation ratio
    * Track per-port flow counts via the controller's flow table
    * Expose a thread-safe snapshot dict for the strategy to consume

Latency (RTT) is *not* derivable from port stats; the controller injects
periodic probe packets and times the corresponding PacketIn. That logic
lives in `qos_load_balancer.py` because it depends on flow-table state.
"""

# Standard library
import logging
import time
from threading import Lock
from typing import Dict, Optional

# Ryu
from ryu.lib import hub  # green-thread primitives (eventlet underneath)


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Per-port rolling sample
# ---------------------------------------------------------------------------
class _PortSample:
    """
    Tracks the previous OFPPortStats reading so we can compute deltas.
    Keeping this as a tiny class (vs. a dict) makes the arithmetic in
    `update()` self-documenting.
    """
    __slots__ = ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "ts")

    def __init__(self) -> None:
        self.rx_bytes  = 0
        self.tx_bytes  = 0
        self.rx_errors = 0
        self.tx_errors = 0
        self.ts        = 0.0


# ---------------------------------------------------------------------------
#  Collector
# ---------------------------------------------------------------------------
class MetricsCollector:
    """
    Owns a green-thread that polls every `poll_interval_s` seconds.

    The Ryu app constructs one of these, registers `handle_port_stats_reply`
    as the OFPPortStatsReply event handler, and reads `snapshot()` whenever
    it needs to make a routing decision.
    """

    def __init__(self, poll_interval_s: float = 2.0,
                 link_capacity_bps: float = 10e6):
        # Polling cadence -- 2 s is a comfortable middle ground:
        # short enough to react, long enough to amortise OF overhead.
        self.poll_interval_s   = poll_interval_s

        # Used to convert bytes/sec into a [0, 1] utilisation ratio.
        # Must match the bandwidth set in `prometheus_topo.py`.
        self.link_capacity_bps = link_capacity_bps

        # datapath_id -> { port_no -> _PortSample }
        self._previous: Dict[int, Dict[int, _PortSample]] = {}

        # Thread-safe snapshot consumed by strategies.
        # Key format: (dpid, port_no) -> dict(util_ratio, loss_ratio, ts)
        self._snapshot: Dict[tuple, dict] = {}
        self._lock = Lock()

        # Set externally by the Ryu app once it knows the datapath
        self._datapaths: Dict[int, "ryu.controller.controller.Datapath"] = {}

        self._poller_thread: Optional[hub.greenthread.GreenThread] = None

    # ----- lifecycle -------------------------------------------------------
    def register_datapath(self, datapath) -> None:
        """Called from the EventOFPStateChange handler in the Ryu app."""
        self._datapaths[datapath.id] = datapath
        _LOG.info("metrics: registered datapath dpid=%016x", datapath.id)

    def unregister_datapath(self, datapath) -> None:
        self._datapaths.pop(datapath.id, None)
        self._previous.pop(datapath.id, None)

    def start(self) -> None:
        """Spawn the periodic poller. Idempotent."""
        if self._poller_thread is None:
            self._poller_thread = hub.spawn(self._poll_loop)
            _LOG.info("metrics: poller started (interval=%.1fs)",
                      self.poll_interval_s)

    # ----- snapshot consumed by strategies ---------------------------------
    def snapshot(self, dpid: int, port_no: int) -> dict:
        """
        Return the latest metrics for one switch-port.
        Defaults are conservative so the strategy never sees NaN.
        """
        with self._lock:
            return self._snapshot.get((dpid, port_no), {
                "util_ratio": 0.0,
                "loss_ratio": 0.0,
                "ts":         0.0,
            })

    # ----- internal: polling loop -----------------------------------------
    def _poll_loop(self) -> None:
        """Runs until the controller shuts down."""
        while True:
            for dp in list(self._datapaths.values()):
                self._request_port_stats(dp)
            hub.sleep(self.poll_interval_s)

    @staticmethod
    def _request_port_stats(datapath) -> None:
        """Send OFPPortStatsRequest for *all* ports on this datapath."""
        ofp        = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        req = ofp_parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
        datapath.send_msg(req)

    # ----- callback: stats reply ------------------------------------------
    def handle_port_stats_reply(self, ev) -> None:
        """
        Wire this up in the Ryu app:

            @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
            def _on_stats(self, ev):
                self.metrics.handle_port_stats_reply(ev)
        """
        body  = ev.msg.body
        dpid  = ev.msg.datapath.id
        now   = time.time()

        prev_per_port = self._previous.setdefault(dpid, {})

        for stat in body:
            port = stat.port_no

            # OFPP_LOCAL and other reserved ports show up here; skip them
            # by filtering on a sane upper bound.
            if port > 0xFF00:  # OFPP_MAX-ish
                continue

            prev = prev_per_port.get(port)
            curr = _PortSample()
            curr.rx_bytes  = stat.rx_bytes
            curr.tx_bytes  = stat.tx_bytes
            curr.rx_errors = stat.rx_errors
            curr.tx_errors = stat.tx_errors
            curr.ts        = now

            # First sample -> store and skip; we need two points for a delta.
            if prev is None or prev.ts == 0.0:
                prev_per_port[port] = curr
                continue

            dt = max(curr.ts - prev.ts, 1e-3)  # guard against zero division

            # ---- Throughput / utilisation -------------------------------
            tx_bps = (curr.tx_bytes - prev.tx_bytes) * 8.0 / dt
            rx_bps = (curr.rx_bytes - prev.rx_bytes) * 8.0 / dt
            util   = max(tx_bps, rx_bps) / self.link_capacity_bps
            util   = min(max(util, 0.0), 1.0)  # clamp -- TC bursts can exceed

            # ---- Loss proxy --------------------------------------------
            # OF doesn't give us "packets lost upstream" directly; we use
            # the port error counters as a coarse proxy. For a finer
            # measurement, the controller injects probe packets (see RTT
            # logic in qos_load_balancer.py).
            err_delta = ((curr.rx_errors - prev.rx_errors) +
                         (curr.tx_errors - prev.tx_errors))
            # Normalise against "packets that went through" -- we can't
            # know that exactly without rx_packets/tx_packets, so we
            # approximate via bytes-at-MTU.
            approx_pkts = max(
                (curr.rx_bytes - prev.rx_bytes +
                 curr.tx_bytes - prev.tx_bytes) / 1500.0,
                1.0,
            )
            loss = min(err_delta / approx_pkts, 1.0)

            # ---- Publish under lock -------------------------------------
            with self._lock:
                self._snapshot[(dpid, port)] = {
                    "util_ratio": util,
                    "loss_ratio": loss,
                    "ts":         now,
                }

            prev_per_port[port] = curr
