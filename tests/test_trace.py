"""TRACE with current MeshCore hash widths.

The low two bits of `flags` are a SHIFT, not a count: each entry of the supplied route is
`1 << (flags & 3)` bytes — 1, 2, 4 or 8. The packet's own path field is unrelated; it
collects ONE SNR BYTE per hop ("append SNR (Not hash!)" in mesh::Mesh::onRecvPacket).

Comparing those two by BYTE length — as this did — declares a 2/4/8-byte-hash trace
finished while it still has hops to walk, and drops it as incomplete at the originator.

Upstream reference: meshcore-dev/MeshCore @ 0679dbeffc504d562d2f09eb072fdc223f8ffc2a
    uint8_t path_sz = flags & 0x03;
    uint16_t offset = (uint16_t)pkt->path_len << path_sz;
    if (offset >= len) { ...trace complete... }
"""

import struct
import unittest
from collections import defaultdict

import packet
from exceptions import InvalidMeshcorePacket

from tests.test_packet_wireformat import encoded_pathlen, hdr

DIRECT = packet.MC_Packet.ROUTE_DIRECT


def trace_packet(route, flags, snrs=b''):
    """A TRACE on the wire: header + path(SNRs) + payload(tag, auth, flags, route)."""
    payload = b'\x01\x02\x03\x04' + b'\x05\x06\x07\x08' + bytes([flags]) + route
    return (bytes([hdr(DIRECT, packet.MC_Packet.TYPE_TRACE),
                   encoded_pathlen(1, len(snrs))]) + snrs + payload)


class HashWidthTests(unittest.TestCase):
    def test_flags_low_bits_are_a_shift(self):
        for path_sz, width in ((0, 1), (1, 2), (2, 4), (3, 8)):
            p = packet.MC_Trace(trace_packet(b'\x00' * (width * 2), path_sz))
            with self.subTest(path_sz=path_sz):
                self.assertEqual(p.trace_hash_size, width)
                self.assertEqual(p.trace_hops, 2)

    def test_upper_flag_bits_do_not_affect_the_width(self):
        p = packet.MC_Trace(trace_packet(b'\xaa\xbb', 0xFC | 0x01))
        self.assertEqual(p.trace_hash_size, 2)
        self.assertEqual(p.trace_hops, 1)


class CompletionTests(unittest.TestCase):
    def test_one_byte_hashes_complete_when_every_hop_has_an_snr(self):
        route = b'\x11\x22\x33'
        self.assertFalse(packet.MC_Trace(trace_packet(route, 0, b'\x01\x02')).trace_completed)
        self.assertTrue(packet.MC_Trace(trace_packet(route, 0, b'\x01\x02\x03')).trace_completed)

    def test_a_two_byte_hash_trace_is_not_finished_at_byte_parity(self):
        """The regression: 3 hops of 2-byte hashes is 6 route bytes. With 6 SNR bytes the
        old byte comparison called it complete; it is complete at THREE."""
        route = b'\xaa\xbb' b'\xcc\xdd' b'\xee\xff'
        self.assertFalse(packet.MC_Trace(trace_packet(route, 1, b'\x01\x02')).trace_completed)
        self.assertTrue(packet.MC_Trace(trace_packet(route, 1, b'\x01\x02\x03')).trace_completed)

    def test_four_and_eight_byte_hashes(self):
        for path_sz, width in ((2, 4), (3, 8)):
            route = b'\x5a' * (width * 2)
            with self.subTest(width=width):
                self.assertFalse(
                    packet.MC_Trace(trace_packet(route, path_sz, b'\x01')).trace_completed)
                self.assertTrue(
                    packet.MC_Trace(trace_packet(route, path_sz, b'\x01\x02')).trace_completed)


class HopExtractionTests(unittest.TestCase):
    def test_hops_are_whole_entries(self):
        p = packet.MC_Trace(trace_packet(b'\xaa\xbb' b'\xcc\xdd', 1))
        self.assertEqual(p.trace_hop(0), b'\xaa\xbb')
        self.assertEqual(p.trace_hop(1), b'\xcc\xdd')
        self.assertIsNone(p.trace_hop(2))

    def test_one_byte_hops_are_still_single_bytes(self):
        p = packet.MC_Trace(trace_packet(b'\x11\x22', 0))
        self.assertEqual(p.trace_hop(0), b'\x11')
        self.assertEqual(p.trace_hop(1), b'\x22')


class _Me:
    def __init__(self, pubkey=b'\xa1\xb2\xc3\xd4\xe5\xf6\x07\x08'):
        self.pubkey = pubkey

    @property
    def hash(self):
        return self.pubkey[0]

    def path_hash(self, size=1):
        return bytes(self.pubkey[:size])


class RepeaterForwardTests(unittest.IsolatedAsyncioTestCase):
    """The repeater must match its hash at the packet's own entry width."""

    def _repeater(self):
        from repeater import Repeater
        r = Repeater.__new__(Repeater)
        r.me = _Me()
        r.stats = defaultdict(int)
        r.transmitted = []

        async def fake_tx(p, **kw):
            r.transmitted.append(p)
        r.transmit_packet = fake_tx
        return r

    async def test_a_two_byte_hop_addressed_to_us_is_matched(self):
        import aiotools
        r = self._repeater()
        route = b'\xa1\xb2' b'\x99\x99'                 # first hop is us, at 2 bytes
        p = packet.MC_Trace(trace_packet(route, 1))
        async with aiotools.TaskGroup() as tg:          # rx_trace spawns its resend
            await r.rx_trace(p)
        self.assertEqual(len(p.path), 1, "our SNR should have been appended")

    async def test_a_two_byte_hop_not_addressed_to_us_is_ignored(self):
        import aiotools
        r = self._repeater()
        route = b'\xa1\x99' b'\x11\x22'                 # first byte matches, second does not
        p = packet.MC_Trace(trace_packet(route, 1))
        async with aiotools.TaskGroup() as tg:
            await r.rx_trace(p)
        self.assertEqual(len(p.path), 0, "must not claim a hop that is not ours")


class TraceOutTests(unittest.TestCase):
    def test_a_route_must_be_whole_entries(self):
        with self.assertRaises(ValueError):
            packet.MC_Trace_Out(b'\xaa\xbb\xcc', tag=1, flags=1)   # 3 bytes, 2-byte entries

    def test_whole_entry_routes_are_accepted(self):
        for flags, route in ((0, b'\x11\x22'), (1, b'\xaa\xbb\xcc\xdd'),
                             (2, b'\x00' * 8), (3, b'\x00' * 16)):
            with self.subTest(flags=flags):
                p = packet.MC_Trace_Out(route, tag=1, flags=flags)
                self.assertEqual(p.tracepath, route)

    def test_the_route_is_bounded_by_max_path_size(self):
        with self.assertRaises(ValueError):
            packet.MC_Trace_Out(b'\x00' * (packet.MC_Packet.MAX_PATH_SIZE + 1),
                                tag=1, flags=0)

    def test_a_full_length_multi_byte_route_is_allowed(self):
        """32 entries of 2 bytes is exactly MAX_PATH_SIZE and must not be refused."""
        p = packet.MC_Trace_Out(b'\x5a' * packet.MC_Packet.MAX_PATH_SIZE, tag=1, flags=1)
        self.assertEqual(len(p.tracepath), packet.MC_Packet.MAX_PATH_SIZE)


if __name__ == '__main__':
    unittest.main()
