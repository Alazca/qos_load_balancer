from mininet.topo import Topo

class LBTopo(Topo):
	def build(self):
		s1 = self.addSwitch('s1')

		h1 = self.addHost('h1', ip='10.0.0.1/24')
		h2 = self.addHost('h2', ip='10.0.0.2/24')
		h3 = self.addHost('h3', ip='10.0.0.3/24')
		h4 = self.addHost('h4', ip='10.0.0.4/24')
		h5 = self.addHost('h5', ip='10.0.0.7/24')
		h6 = self.addHost('h6', ip='10.0.0.8/24')

		srv1 = self.addHost('srv1', ip='10.0.0.5/24')
		srv2 = self.addHost('srv2', ip='10.0.0.6/24')
		srv3 = self.addHost('srv3', ip='10.0.0.9/24')

		self.addLink(h1, s1, bw=10, delay='2ms')
		self.addLink(h2, s1, bw=10, delay='2ms')
		self.addLink(h3, s1, bw=10, delay='2ms')
		self.addLink(h4, s1, bw=10, delay='2ms')
		self.addLink(h5, s1, bw=10, delay='2ms')
		self.addLink(h6, s1, bw=10, delay='2ms')

		self.addLink(srv1, s1, bw=10, delay='2ms')
		self.addLink(srv2, s1, bw=10, delay='2ms')
		self.addLink(srv3, s1, bw=10, delay='2ms')

topos = {
	'lbtopo': LBTopo
}
