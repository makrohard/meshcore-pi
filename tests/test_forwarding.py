"""Repeater path rewriting, against current MeshCore semantics.

Reference: mesh::Mesh (meshcore-dev/MeshCore @ 0679dbeffc504d562d2f09eb072fdc223f8ffc2a)

  flood forward   append our hash at path[n*sz], count -> n+1,
                  allowed while (n+1)*sz <= MAX_PATH_SIZE
  direct forward  match self against path[0:sz]; then decrement the count and
                  shuffle the path down by ONE ENTRY (the first hop is consumed)
"""

import unittest
from collections import defaultdict

from basicmesh import BasicMesh
from packet import MC_Incoming, MC_Packet

from tests.test_packet_wireformat import encoded_pathlen, hdr, wire

FLOOD = MC_Packet.ROUTE_FLOOD
DIRECT = MC_Packet.ROUTE_DIRECT
T_DIRECT = MC_Packet.ROUTE_TRANSPORT_DIRECT


class _Me:
    """Just enough identity: the path hash is the public-key prefix."""

    def __init__(self, pubkey=b'\xa1\xb2\xc3\xd4'):
        self.pubkey = pubkey

    @property
    def hash(self):
        return self.pubkey[0]

    def path_hash(self, size=1):
        return bytes(self.pubkey[:size])


class _Mesh(BasicMesh):
    def __init__(self, me):
        self.me = me
        self.stats = defaultdict(int)


def _mesh(pubkey=b'\xa1\xb2\xc3\xd4'):
    return _Mesh(_Me(pubkey))


class FloodForwardTests(unittest.TestCase):
    def test_our_hash_is_appended(self):
        m = _mesh()
        p = MC_Incoming(wire(FLOOD, path=b'\x11\x22', payload=b'\x01'))
        self.assertTrue(m.prepare_forward(p))
        self.assertEqual(bytes(p.path), b'\x11\x22\xa1')
        self.assertEqual(p.pathlen, 3)

    def test_append_uses_the_packets_own_hash_size(self):
        """A 2-byte-hash path must gain a 2-byte entry. Appending one byte would misalign
        every hop after it."""
        m = _mesh()
        p = MC_Incoming(wire(FLOOD, path=b'\x11\x22', payload=b'\x01', hash_size=2))
        self.assertTrue(m.prepare_forward(p))
        self.assertEqual(bytes(p.path), b'\x11\x22\xa1\xb2')
        self.assertEqual(p.pathlen, 2)
        self.assertEqual(p.path_hash_size, 2)
        # and it still serialises to a valid packet
        self.assertEqual(MC_Incoming(bytes(p.packet)).pathlen, 2)

    def test_full_path_is_not_extended(self):
        m = _mesh()
        raw = bytes([hdr(FLOOD), encoded_pathlen(1, 63)]) + b'\x07' * 63 + b'\x01'
        p = MC_Incoming(raw)
        self.assertFalse(m.prepare_forward(p))
        self.assertEqual(len(p.path), 63)

    def test_two_byte_path_is_bounded_by_bytes_not_entries(self):
        # 32 entries * 2 bytes = 64 == MAX_PATH_SIZE: adding one more would exceed it,
        # even though the 6-bit count could still hold 63.
        m = _mesh()
        raw = bytes([hdr(FLOOD), encoded_pathlen(2, 32)]) + b'\x07' * 64 + b'\x01'
        p = MC_Incoming(raw)
        self.assertFalse(m.prepare_forward(p))
        self.assertEqual(len(p.path), 64)


class DirectForwardTests(unittest.TestCase):
    def test_the_first_hop_is_consumed_not_the_last(self):
        """The bug: `path.pop()` removed the LAST entry after matching the FIRST, so a
        multi-hop packet was sent back towards its origin."""
        m = _mesh()
        p = MC_Incoming(wire(DIRECT, path=b'\xa1\x22\x33', payload=b'\x01'))
        self.assertTrue(m.prepare_forward(p))
        self.assertEqual(bytes(p.path), b'\x22\x33')      # NOT b'\xa1\x22'
        self.assertEqual(p.pathlen, 2)

    def test_single_hop_leaves_an_empty_path(self):
        m = _mesh()
        p = MC_Incoming(wire(DIRECT, path=b'\xa1', payload=b'\x01'))
        self.assertTrue(m.prepare_forward(p))
        self.assertEqual(bytes(p.path), b'')
        self.assertEqual(p.pathlen, 0)

    def test_a_packet_not_addressed_to_us_is_dropped(self):
        m = _mesh()
        p = MC_Incoming(wire(DIRECT, path=b'\x99\xa1', payload=b'\x01'))
        self.assertFalse(m.prepare_forward(p))
        self.assertEqual(bytes(p.path), b'\x99\xa1')      # untouched
        self.assertEqual(m.stats["repeat.Direct.notme"], 1)

    def test_zero_hop_is_dropped(self):
        m = _mesh()
        p = MC_Incoming(wire(DIRECT, payload=b'\x01'))
        self.assertFalse(m.prepare_forward(p))
        self.assertEqual(m.stats["repeat.Direct.zerohop"], 1)

    def test_multi_byte_hash_match_uses_the_full_entry(self):
        """With 2-byte hashes, a node whose FIRST byte matches but second does not must
        not claim the hop."""
        m = _mesh()
        p = MC_Incoming(wire(DIRECT, path=b'\xa1\x99\x33\x44', payload=b'\x01',
                             hash_size=2))
        self.assertFalse(m.prepare_forward(p))
        p2 = MC_Incoming(wire(DIRECT, path=b'\xa1\xb2\x33\x44', payload=b'\x01',
                              hash_size=2))
        self.assertTrue(m.prepare_forward(p2))
        self.assertEqual(bytes(p2.path), b'\x33\x44')


class TransportForwardTests(unittest.TestCase):
    def test_transport_codes_survive_a_direct_forward(self):
        m = _mesh()
        raw = wire(T_DIRECT, path=b'\xa1\x22', payload=b'\x42',
                   transport=(0xdead, 0xbeef))
        p = MC_Incoming(raw)
        self.assertTrue(m.prepare_forward(p))
        out = MC_Incoming(bytes(p.packet))
        self.assertEqual(out.transport_codes, (0xdead, 0xbeef))
        self.assertEqual(bytes(out.path), b'\x22')
        self.assertTrue(out.is_direct())
        self.assertEqual(out.payload, b'\x42')


if __name__ == '__main__':
    unittest.main()
