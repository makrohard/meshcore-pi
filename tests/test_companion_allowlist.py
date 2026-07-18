import ipaddress
import unittest

from configuration import get_config
from companionwifi import (
    CompanionInterface,
    derive_listen_host,
    parse_allow_network,
    peer_allowed,
)


class ParseAllowNetworkTests(unittest.TestCase):
    def test_bare_address_becomes_slash_32(self):
        net = parse_allow_network("192.168.0.5")
        self.assertEqual(net, ipaddress.ip_network("192.168.0.5/32"))

    def test_cidr_is_preserved(self):
        net = parse_allow_network("192.168.0.0/24")
        self.assertEqual(net, ipaddress.ip_network("192.168.0.0/24"))

    def test_any_network(self):
        net = parse_allow_network("0.0.0.0/0")
        self.assertEqual(net, ipaddress.ip_network("0.0.0.0/0"))

    def test_invalid_value_fails_closed_to_loopback(self):
        net = parse_allow_network("garbage")
        self.assertEqual(net, ipaddress.ip_network("127.0.0.1/32"))

    def test_host_bits_set_are_tolerated(self):
        # strict=False: 192.168.0.5/24 is accepted as the 192.168.0.0/24 network.
        net = parse_allow_network("192.168.0.5/24")
        self.assertEqual(net, ipaddress.ip_network("192.168.0.0/24"))

    def test_ipv6_fails_closed_to_loopback(self):
        # IPv4 only: an IPv6 allow-list would leave derive_listen_host binding IPv4
        # 0.0.0.0 (family mismatch), so it fails closed to loopback instead.
        for v6 in ("::1", "::1/128", "2001:db8::/32", "::/0"):
            self.assertEqual(parse_allow_network(v6),
                             ipaddress.ip_network("127.0.0.1/32"), v6)


class DeriveListenHostTests(unittest.TestCase):
    def test_loopback_host_stays_loopback(self):
        net = parse_allow_network("127.0.0.1")
        self.assertEqual(derive_listen_host(net), "127.0.0.1")

    def test_loopback_range_stays_loopback(self):
        net = parse_allow_network("127.0.0.0/8")
        self.assertEqual(derive_listen_host(net), "127.0.0.1")

    def test_lan_derives_all_interfaces(self):
        net = parse_allow_network("192.168.0.0/24")
        self.assertEqual(derive_listen_host(net), "0.0.0.0")

    def test_any_derives_all_interfaces(self):
        net = parse_allow_network("0.0.0.0/0")
        self.assertEqual(derive_listen_host(net), "0.0.0.0")


class PeerAllowedTests(unittest.TestCase):
    def test_peer_inside_range_allowed(self):
        net = parse_allow_network("192.168.0.0/24")
        self.assertTrue(peer_allowed(net, "192.168.0.42"))

    def test_peer_outside_range_rejected(self):
        net = parse_allow_network("192.168.0.0/24")
        self.assertFalse(peer_allowed(net, "10.0.0.5"))

    def test_loopback_only_rejects_lan_peer(self):
        net = parse_allow_network("127.0.0.1")
        self.assertTrue(peer_allowed(net, "127.0.0.1"))
        self.assertFalse(peer_allowed(net, "192.168.0.42"))

    def test_malformed_peer_fails_closed(self):
        net = parse_allow_network("0.0.0.0/0")
        self.assertFalse(peer_allowed(net, "not-an-ip"))

    def test_family_mismatch_fails_closed(self):
        # An IPv6 peer against an IPv4 allow-net must not raise, just reject.
        net = parse_allow_network("192.168.0.0/24")
        self.assertFalse(peer_allowed(net, "::1"))


class CompanionInterfaceConfigTests(unittest.TestCase):
    """The interface receives a 'wifi' sub-view; verify config wiring end to end."""

    def make(self, wifi):
        config = get_config({"wifi": wifi})
        return CompanionInterface(config.get("wifi", view=True))

    def test_default_is_loopback_only(self):
        iface = self.make({})
        self.assertEqual(iface.listen, "127.0.0.1")
        self.assertEqual(iface.port, 5000)
        self.assertTrue(peer_allowed(iface._allow_net, "127.0.0.1"))
        self.assertFalse(peer_allowed(iface._allow_net, "192.168.0.1"))

    def test_lan_allow_binds_all_interfaces(self):
        iface = self.make({"allow": "192.168.0.0/24"})
        self.assertEqual(iface.listen, "0.0.0.0")
        self.assertTrue(peer_allowed(iface._allow_net, "192.168.0.99"))
        self.assertFalse(peer_allowed(iface._allow_net, "10.1.2.3"))

    def test_explicit_listen_overrides_derived_address(self):
        iface = self.make({"allow": "192.168.0.0/24", "listen": "192.168.0.10"})
        self.assertEqual(iface.listen, "192.168.0.10")
        # The allow-list still governs who may connect.
        self.assertTrue(peer_allowed(iface._allow_net, "192.168.0.99"))

    def test_invalid_allow_fails_closed_to_loopback(self):
        iface = self.make({"allow": "garbage"})
        self.assertEqual(iface.listen, "127.0.0.1")
        self.assertFalse(peer_allowed(iface._allow_net, "192.168.0.1"))

    def test_custom_port(self):
        iface = self.make({"port": 5555})
        self.assertEqual(iface.port, 5555)


if __name__ == "__main__":
    unittest.main()
