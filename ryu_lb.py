from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, ether_types


class SimpleLoadBalancer(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    VIP = '10.0.0.100'
    VIP_MAC = '00:00:00:00:00:99'

    servers = [
        {'ip': '10.0.0.5', 'mac': '00:00:00:00:00:05', 'port': 7},
        {'ip': '10.0.0.6', 'mac': '00:00:00:00:00:06', 'port': 8},
	{'ip': '10.0.0.9', 'mac': '00:00:00:00:00:09', 'port': 9},
    ]

    def __init__(self, *args, **kwargs):
        super(SimpleLoadBalancer, self).__init__(*args, **kwargs)
        self.server_index = 0
        self.mac_to_port = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        inst = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst
        )

        datapath.send_msg(mod)

    def pick_server(self):
        server = self.servers[self.server_index]
        self.server_index = (self.server_index + 1) % len(self.servers)
        return server

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        # Reply to ARP requests for the VIP
        if arp_pkt:
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == self.VIP:
                self.reply_arp(datapath, eth, arp_pkt, in_port)
                return

        # Load-balance IPv4 packets sent to VIP
        if ip_pkt and ip_pkt.dst == self.VIP:
            server = self.pick_server()

            # Forward direction:
            # client -> VIP becomes client -> real server
            forward_actions = [
                parser.OFPActionSetField(ipv4_dst=server['ip']),
                parser.OFPActionSetField(eth_dst=server['mac']),
                parser.OFPActionOutput(server['port'])
            ]

            forward_match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_src=ip_pkt.src,
                ipv4_dst=self.VIP
            )

            self.add_flow(datapath, 10, forward_match, forward_actions)

            # Reverse direction:
            # real server -> client becomes VIP -> client
            reverse_actions = [
                parser.OFPActionSetField(ipv4_src=self.VIP),
                parser.OFPActionSetField(eth_src=self.VIP_MAC),
                parser.OFPActionOutput(in_port)
            ]

            reverse_match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_src=server['ip'],
                ipv4_dst=ip_pkt.src
            )

            self.add_flow(datapath, 10, reverse_match, reverse_actions)

            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port,
                actions=forward_actions,
                data=msg.data
            )

            datapath.send_msg(out)
            return

        # Normal L2 learning switch behavior
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        out_port = self.mac_to_port[dpid].get(
            eth.dst,
            ofproto.OFPP_FLOOD
        )

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_dst=eth.dst,
                eth_src=eth.src
            )
            self.add_flow(datapath, 1, match, actions)

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=msg.data
        )

        datapath.send_msg(out)

    def reply_arp(self, datapath, eth, arp_pkt, in_port):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        e = ethernet.ethernet(
            dst=eth.src,
            src=self.VIP_MAC,
            ethertype=ether_types.ETH_TYPE_ARP
        )

        a = arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=self.VIP_MAC,
            src_ip=self.VIP,
            dst_mac=arp_pkt.src_mac,
            dst_ip=arp_pkt.src_ip
        )

        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()

        actions = [parser.OFPActionOutput(in_port)]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=p.data
        )

        datapath.send_msg(out)


