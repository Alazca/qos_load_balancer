"""
qos_load_balancer.py
====================
Project Prometheus -- main Ryu application.

Responsibilities
    * Discover the topology (single switch s1 in the reference design).
    * Advertise a Virtual IP (VIP). Clients connect to the VIP; the
      controller decides which backend serves each new flow.
    * On PacketIn for a *new* client->VIP TCP/UDP flow:
        1. Ask the strategy module for a backend.
        2. Install symmetric flow-mod rules:
             client -> VIP    becomes  client -> backend  (DNAT-style)
             backend -> client becomes backend->client    (SNAT spoofs VIP)
        3. Forward the buffered packet on the chosen path.
    * Periodically refresh per-backend QoS metrics (latency probes here,
      utilisation/loss in metrics_collector.py).

Design notes
    * The VIP has no host behind it; ARP for the VIP is answered by the
      controller itself (proxy-ARP).
    * Flows are installed with an idle_timeout so dead client sessions
      are reaped automatically -- avoids unbounded flow-table growth
      during long stress tests.
    * Strategy is loaded by name from `balancing_strategies`; change the
      `STRATEGY` constant (or pass --user-args) to A/B different policies.

Run:
    ryu-manager controller.qos_load_balancer
"""

# Standard library
import logging
import time
from typing import Dict, List, Optional

# Ryu
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
)
from ryu.lib import hub
from ryu.lib.packet import (
    arp, ethernet, icmp, ipv4, packet, tcp, udp
)
from ryu.ofproto import ofproto_v1_3

# Project
from controller import balancing_strategies as bs
from controller.metrics_collector import MetricsCollector


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Configuration constants
# ---------------------------------------------------------------------------
# Virtual IP advertised to clients. Must be in the same /24 as h1..h6.
VIP_IP  = "10.0.0.100"
VIP_MAC = "02:00:00:00:00:64"   # locally-administered, mirrors VIP last octet

# Backend pool. The Ryu app learns each backend's switch port from the
# first packet it sees, so all we hard-code is the IP/MAC pairing.
BACKEND_POOL = [
    # (name,  ip,           mac)
    ("srv1",  "10.0.0.201", "00:00:00:00:00:07"),  # autoSetMacs assigns 7..9
    ("srv2",  "10.0.0.202", "00:00:00:00:00:08"),
    ("srv3",  "10.0.0.203", "00:00:00:00:00:09"),
]

# Which strategy to use; swap freely for evaluation runs.
STRATEGY = "qos_weighted"     # one of bs.available()
STRATEGY_KWARGS = dict(w_lat=0.5, w_loss=0.3, w_util=0.2)

# Flow-mod timeouts. Idle = 30 s catches normal session ends; hard = 5 min
# guarantees periodic re-balancing even for very long-lived flows.
FLOW_IDLE_TIMEOUT_S = 30
FLOW_HARD_TIMEOUT_S = 300

# RTT probe cadence (ICMP echo from controller to each backend).
RTT_PROBE_INTERVAL_S = 5.0


# ---------------------------------------------------------------------------
#  Ryu application
# ---------------------------------------------------------------------------
class QoSLoadBalancer(app_manager.RyuApp):
    """
    Single-instance Ryu app. Mininet's single switch makes the topology
    trivial; the interesting logic is the per-flow scheduling decision in
    `_handle_new_client_flow`.
    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # ----- lifecycle -------------------------------------------------------
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Pluggable strategy -- pulls from the registry.
        self.strategy = bs.build(STRATEGY, **STRATEGY_KWARGS)
        _LOG.info("controller: strategy=%s kwargs=%s",
                  self.strategy.strategy_name, STRATEGY_KWARGS)

        # Live backend list. Switch ports are filled in via PacketIn.
        self.backends: List[bs.Backend] = [
            bs.Backend(name=n, ip=ip, mac=mac, port=0)
            for (n, ip, mac) in BACKEND_POOL
        ]
        self._by_ip:  Dict[str, bs.Backend]  = {b.ip:  b for b in self.backends}
        self._by_mac: Dict[str, bs.Backend]  = {b.mac: b for b in self.backends}

        # Flow affinity: (client_ip, client_port, vip_port) -> Backend
        # Maintains five-tuple stickiness so a single TCP session always
        # lands on the same backend until the flow ages out.
        self._affinity: Dict[tuple, bs.Backend] = {}

        # Stats / metrics
        self.metrics = MetricsCollector(poll_interval_s=2.0,
                                        link_capacity_bps=10e6)
        self.metrics.start()

        # RTT probe bookkeeping: backend.ip -> last_send_ts
        self._probe_send_ts: Dict[str, float] = {}
        hub.spawn(self._rtt_probe_loop)

        # The single datapath we expect (s1). Stored when it connects.
        self._datapath: Optional["ryu.controller.controller.Datapath"] = None

    # ----- handler: switch features ----------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _on_switch_features(self, ev):
        """
        Called once per switch at handshake time. Install the table-miss
        flow so unmatched packets bubble up to the controller as PacketIn.
        """
        datapath = ev.msg.datapath
        ofp        = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        self._datapath = datapath
        self.metrics.register_datapath(datapath)

        # Table-miss: match-everything, send to controller (no buffering).
        match   = ofp_parser.OFPMatch()
        actions = [ofp_parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                              ofp.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, priority=0, match=match, actions=actions)

        _LOG.info("controller: switch %016x connected, table-miss installed",
                  datapath.id)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _on_state_change(self, ev):
        """Track datapath lifecycle so the metrics poller stays consistent."""
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER:
            self.metrics.unregister_datapath(dp)

    # ----- handler: port stats reply ---------------------------------------
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _on_port_stats_reply(self, ev):
        """Delegate to the metrics module, then sync into Backend objects."""
        self.metrics.handle_port_stats_reply(ev)

        # Push freshly-computed metrics into the strategy's view of the world.
        for b in self.backends:
            if b.port == 0:
                continue
            snap = self.metrics.snapshot(self._datapath.id, b.port)
            b.util_ratio = snap["util_ratio"]
            b.loss_ratio = snap["loss_ratio"]

    # ----- handler: PacketIn (the workhorse) -------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _on_packet_in(self, ev):
        """
        Single PacketIn dispatcher. Order of operations:
            1. Learn the in_port for whichever host originated the packet
               (clients map IP->port; backends update Backend.port).
            2. ARP for the VIP            -> proxy-respond.
            3. ICMP echo reply from backend -> RTT measurement.
            4. IPv4+TCP/UDP destined VIP  -> load-balance.
            5. Anything else              -> ignored.
        """
        msg     = ev.msg
        dp      = msg.datapath
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # --- 2. ARP for VIP -------------------------------------------------
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt is not None:
            # Learn from the ARP source too; ARP is often a host's first frame.
            if arp_pkt.src_ip and arp_pkt.src_ip not in self._by_ip:
                self._client_ports[arp_pkt.src_ip] = in_port
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == VIP_IP:
                self._proxy_arp_for_vip(dp, in_port, eth, arp_pkt)
            return

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is None:
            return

        # --- 1. Port learning ----------------------------------------------
        if ip_pkt.src in self._by_ip:
            # Backend speaking -> remember its switch port
            self._by_ip[ip_pkt.src].port = in_port
        else:
            # Anything else is a client (or the VIP probe loopback, but
            # that originates from the controller and never re-enters here)
            self._client_ports[ip_pkt.src] = in_port

        # --- 3. ICMP echo reply -> RTT sample -------------------------------
        icmp_pkt = pkt.get_protocol(icmp.icmp)
        if icmp_pkt is not None and icmp_pkt.type == icmp.ICMP_ECHO_REPLY:
            self._record_rtt(ip_pkt.src)
            return

        # --- 4. Client -> VIP TCP/UDP: load balance ------------------------
        if ip_pkt.dst == VIP_IP:
            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)
            if tcp_pkt is not None or udp_pkt is not None:
                self._handle_new_client_flow(
                    dp, in_port, msg, eth, ip_pkt, tcp_pkt, udp_pkt
                )
            return

    # ----- core: load-balancing decision -----------------------------------
    def _handle_new_client_flow(self, dp, in_port, msg, eth, ip_pkt,
                                tcp_pkt, udp_pkt) -> None:
        """
        Pick a backend, install bidirectional flows, forward the buffered pkt.
        """
        # Five-tuple key for affinity. Keeps a single TCP session pinned.
        l4         = tcp_pkt or udp_pkt
        l4_proto   = "tcp" if tcp_pkt is not None else "udp"
        client_key = (ip_pkt.src, l4.src_port, l4.dst_port, l4_proto)

        backend = self._affinity.get(client_key)
        if backend is None or backend.port == 0:
            backend = self.strategy.select(self.backends)
            self._affinity[client_key] = backend
            backend.active_flows += 1
            _LOG.info("LB: new flow client=%s:%d -> VIP -> %s (%s) [strategy=%s]",
                      ip_pkt.src, l4.src_port, backend.name, backend.ip,
                      self.strategy.strategy_name)

        # ---- forward path: client -> backend ------------------------------
        self._install_forward_flow(dp, in_port, ip_pkt, l4, l4_proto, backend)

        # ---- reverse path: backend -> client (rewrite src to VIP) ---------
        self._install_reverse_flow(dp, ip_pkt, l4, l4_proto, backend, eth)

        # ---- send the buffered first packet on its way --------------------
        self._send_first_packet(dp, in_port, msg, ip_pkt, l4, l4_proto, backend)

    # ----- flow-mod helpers ------------------------------------------------
    def _install_forward_flow(self, dp, in_port, ip_pkt, l4, l4_proto, backend):
        """client -> VIP   ===  rewrite to ===>  client -> backend"""
        ofp        = dp.ofproto
        ofp_parser = dp.ofproto_parser

        match_kwargs = dict(
            eth_type   = 0x0800,
            ipv4_src   = ip_pkt.src,
            ipv4_dst   = VIP_IP,
            ip_proto   = ip_pkt.proto,
        )
        # Include L4 ports so different sessions from the same client can
        # be balanced independently.
        if l4_proto == "tcp":
            match_kwargs.update(tcp_src=l4.src_port, tcp_dst=l4.dst_port)
        else:
            match_kwargs.update(udp_src=l4.src_port, udp_dst=l4.dst_port)

        actions = [
            ofp_parser.OFPActionSetField(eth_dst=backend.mac),
            ofp_parser.OFPActionSetField(ipv4_dst=backend.ip),
            ofp_parser.OFPActionOutput(backend.port),
        ]
        self._add_flow(
            dp, priority=100,
            match=ofp_parser.OFPMatch(**match_kwargs),
            actions=actions,
            idle_timeout=FLOW_IDLE_TIMEOUT_S,
            hard_timeout=FLOW_HARD_TIMEOUT_S,
        )

    def _install_reverse_flow(self, dp, ip_pkt, l4, l4_proto, backend, eth):
        """backend -> client  ===  rewrite src to VIP ===>  client sees VIP"""
        ofp_parser = dp.ofproto_parser

        match_kwargs = dict(
            eth_type   = 0x0800,
            ipv4_src   = backend.ip,
            ipv4_dst   = ip_pkt.src,
            ip_proto   = ip_pkt.proto,
        )
        # NB: in the reverse direction, src/dst L4 ports are swapped.
        if l4_proto == "tcp":
            match_kwargs.update(tcp_src=l4.dst_port, tcp_dst=l4.src_port)
        else:
            match_kwargs.update(udp_src=l4.dst_port, udp_dst=l4.src_port)

        # Output port for the client side -- learned from PacketIns. If we
        # haven't seen the client yet (port == 0), fall through to FLOOD so
        # the very first reply still reaches its destination; the next
        # packet will hit a properly-installed flow.
        client_port = self._client_port_for(ip_pkt.src)
        out_action = (ofp_parser.OFPActionOutput(client_port)
                      if client_port != 0
                      else ofp_parser.OFPActionOutput(dp.ofproto.OFPP_FLOOD))

        actions = [
            ofp_parser.OFPActionSetField(eth_src=VIP_MAC),
            ofp_parser.OFPActionSetField(ipv4_src=VIP_IP),
            out_action,
        ]
        self._add_flow(
            dp, priority=100,
            match=ofp_parser.OFPMatch(**match_kwargs),
            actions=actions,
            idle_timeout=FLOW_IDLE_TIMEOUT_S,
            hard_timeout=FLOW_HARD_TIMEOUT_S,
        )

    def _send_first_packet(self, dp, in_port, msg, ip_pkt, l4, l4_proto, backend):
        """
        Send the buffered packet that triggered the PacketIn out of the
        chosen backend port, applying the same rewrites the flow rule will.
        Without this, the first packet of every flow is silently dropped.
        """
        ofp        = dp.ofproto
        ofp_parser = dp.ofproto_parser

        actions = [
            ofp_parser.OFPActionSetField(eth_dst=backend.mac),
            ofp_parser.OFPActionSetField(ipv4_dst=backend.ip),
            ofp_parser.OFPActionOutput(backend.port),
        ]
        out = ofp_parser.OFPPacketOut(
            datapath   = dp,
            buffer_id  = msg.buffer_id,
            in_port    = in_port,
            actions    = actions,
            data       = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None,
        )
        dp.send_msg(out)

    # ----- client-port tracker --------------------------------------------
    # client IP -> switch port. Populated lazily inside `_on_packet_in`.
    _client_ports: Dict[str, int] = {}

    def _client_port_for(self, client_ip: str) -> int:
        """Look up the switch port a client lives behind. 0 if unknown."""
        return self._client_ports.get(client_ip, 0)

    # ----- proxy-ARP for VIP ----------------------------------------------
    def _proxy_arp_for_vip(self, dp, in_port, eth, arp_req) -> None:
        """
        Reply to ARP-who-has VIP with our synthetic VIP MAC. Without this,
        clients can't even build the first frame; they ARP into the void.
        """
        ofp        = dp.ofproto
        ofp_parser = dp.ofproto_parser

        # Build the reply by hand -- it's just an Ethernet + ARP frame.
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(
            ethertype = eth.ethertype,
            dst       = eth.src,
            src       = VIP_MAC,
        ))
        reply.add_protocol(arp.arp(
            opcode    = arp.ARP_REPLY,
            src_mac   = VIP_MAC,
            src_ip    = VIP_IP,
            dst_mac   = arp_req.src_mac,
            dst_ip    = arp_req.src_ip,
        ))
        reply.serialize()

        actions = [ofp_parser.OFPActionOutput(in_port)]
        out = ofp_parser.OFPPacketOut(
            datapath   = dp,
            buffer_id  = ofp.OFP_NO_BUFFER,
            in_port    = ofp.OFPP_CONTROLLER,
            actions    = actions,
            data       = reply.data,
        )
        dp.send_msg(out)

    # ----- RTT probing -----------------------------------------------------
    def _rtt_probe_loop(self) -> None:
        """
        Periodically send ICMP echo requests *from the controller* to each
        backend. The reply comes back as a PacketIn (because no flow rule
        matches it), where `_record_rtt` computes the round-trip time.
        """
        while True:
            if self._datapath is not None:
                for b in self.backends:
                    if b.port != 0:
                        self._send_icmp_probe(b)
                        self._probe_send_ts[b.ip] = time.time()
            hub.sleep(RTT_PROBE_INTERVAL_S)

    def _send_icmp_probe(self, backend: bs.Backend) -> None:
        """Synthesise an ICMP echo from VIP to the given backend."""
        dp         = self._datapath
        ofp        = dp.ofproto
        ofp_parser = dp.ofproto_parser

        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype = 0x0800, dst=backend.mac, src=VIP_MAC,
        ))
        pkt.add_protocol(ipv4.ipv4(
            dst=backend.ip, src=VIP_IP, proto=1,  # 1 = ICMP
        ))
        pkt.add_protocol(icmp.icmp(
            type_=icmp.ICMP_ECHO_REQUEST, code=0,
            data=icmp.echo(id_=0xCAFE, seq=int(time.time()) & 0xFFFF,
                           data=b"prometheus"),
        ))
        pkt.serialize()

        actions = [ofp_parser.OFPActionOutput(backend.port)]
        out = ofp_parser.OFPPacketOut(
            datapath  = dp,
            buffer_id = ofp.OFP_NO_BUFFER,
            in_port   = ofp.OFPP_CONTROLLER,
            actions   = actions,
            data      = pkt.data,
        )
        dp.send_msg(out)

    def _record_rtt(self, backend_ip: str) -> None:
        """Update the smoothed RTT for the given backend (EWMA, alpha=0.3)."""
        sent_at = self._probe_send_ts.get(backend_ip)
        if sent_at is None:
            return
        sample_ms = (time.time() - sent_at) * 1000.0
        b = self._by_ip.get(backend_ip)
        if b is None:
            return
        # Exponentially-weighted moving average smooths out single-shot noise.
        b.rtt_ms = 0.7 * b.rtt_ms + 0.3 * sample_ms
        _LOG.debug("rtt %s = %.2f ms (smoothed %.2f)",
                   b.name, sample_ms, b.rtt_ms)

    # ----- thin wrapper around OFPFlowMod ----------------------------------
    @staticmethod
    def _add_flow(datapath, priority, match, actions,
                  idle_timeout: int = 0, hard_timeout: int = 0) -> None:
        """One-stop flow-installation helper used everywhere above."""
        ofp        = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        inst = [ofp_parser.OFPInstructionActions(
            ofp.OFPIT_APPLY_ACTIONS, actions
        )]
        mod = ofp_parser.OFPFlowMod(
            datapath     = datapath,
            priority     = priority,
            match        = match,
            instructions = inst,
            idle_timeout = idle_timeout,
            hard_timeout = hard_timeout,
        )
        datapath.send_msg(mod)
