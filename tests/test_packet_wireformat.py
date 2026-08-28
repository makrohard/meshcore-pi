"""Canonical MeshCore v1 wire-format tests.

Fixtures are built BYTE BY BYTE from the current upstream C++ definition, not from the
Python encoder under test, so a matching bug on both sides cannot hide:

  mesh::Packet::writeTo   (src/Packet.cpp)
      header
      [transport_codes[0], transport_codes[1]]   only when hasTransportCodes()
      path_len                                   encoded: (size-1)<<6 | count
      path                                       count * size bytes
      payload

  mesh::Packet::isValidPathLen
      hash_size == 4 is reserved -> invalid
      hash_count * hash_size <= MAX_PATH_SIZE (64)

Upstream reference: meshcore-dev/MeshCore @ 0679dbeffc504d562d2f09eb072fdc223f8ffc2a
"""

import struct
import unittest

from exceptions import InvalidMeshcorePacket
from packet import MC_Incoming, MC_Packet

FLOOD = MC_Packet.ROUTE_FLOOD
DIRECT = MC_Packet.ROUTE_DIRECT
T_FLOOD = MC_Packet.ROUTE_TRANSPORT_FLOOD
T_DIRECT = MC_Packet.ROUTE_TRANSPORT_DIRECT


def hdr(route, ptype=MC_Packet.TYPE_RAW_CUSTOM, ver=0):
    """header = ver<<6 | type<<2 | route"""
    return (ver << 6) | (ptype << 2) | route


def encoded_pathlen(size, count):
    """mesh::Packet::setPathHashSizeAndCount"""
    return ((size - 1) << 6) | (count & 63)


def wire(route, path=b'', payload=b'\x01', hash_size=1, transport=None,
         ptype=MC_Packet.TYPE_RAW_CUSTOM):
    """A packet exactly as upstream writeTo() would lay it out."""
    out = bytearray([hdr(route, ptype)])
    if transport is not None:
        out += struct.pack("<HH", *transport)
    count = len(path) // hash_size
    out += bytes([encoded_pathlen(hash_size, count)])
    out += path
    out += payload
    return bytes(out)


class OrdinaryRoutesTests(unittest.TestCase):
    def test_flood_no_path(self):
        p = MC_Incoming(wire(FLOOD, payload=b'\xaa\xbb'))
        self.assertTrue(p.is_flood())
        self.assertFalse(p.is_direct())
        self.assertFalse(p.has_transport_codes())
        self.assertEqual(p.pathlen, 0)
        self.assertEqual(bytes(p.path), b'')
        self.assertEqual(p.payload, b'\xaa\xbb')

    def test_flood_with_one_byte_hashes(self):
        p = MC_Incoming(wire(FLOOD, path=b'\x11\x22\x33', payload=b'\x99'))
        self.assertEqual(p.path_hash_size, 1)
        self.assertEqual(p.pathlen, 3)
        self.assertEqual(bytes(p.path), b'\x11\x22\x33')

    def test_direct_with_path(self):
        p = MC_Incoming(wire(DIRECT, path=b'\x0a\x0b', payload=b'\x01'))
        self.assertTrue(p.is_direct())
        self.assertFalse(p.is_flood())
        self.assertEqual(p.pathlen, 2)


class TransportRouteTests(unittest.TestCase):
    def test_transport_flood_codes_are_parsed_not_eaten_as_path_len(self):
        """The corruption this fixes: transport codes sit BEFORE path_len, so reading
        path_len from offset 1 consumed a transport-code byte and desynchronised the rest
        of the packet."""
        raw = wire(T_FLOOD, path=b'\x77', payload=b'\xde\xad',
                   transport=(0x1234, 0xabcd))
        p = MC_Incoming(raw)
        self.assertTrue(p.has_transport_codes())
        self.assertTrue(p.is_flood())
        self.assertEqual(p.transport_codes, (0x1234, 0xabcd))
        self.assertEqual(bytes(p.path), b'\x77')
        self.assertEqual(p.payload, b'\xde\xad')

    def test_transport_direct(self):
        raw = wire(T_DIRECT, path=b'\x01\x02', payload=b'\x05',
                   transport=(0x0001, 0xffff))
        p = MC_Incoming(raw)
        self.assertTrue(p.has_transport_codes())
        self.assertTrue(p.is_direct())
        self.assertEqual(p.transport_codes, (0x0001, 0xffff))
        self.assertEqual(p.payload, b'\x05')

    def test_transport_codes_are_little_endian(self):
        raw = wire(T_FLOOD, payload=b'\x01', transport=(0x0201, 0x0403))
        self.assertEqual(raw[1:5], b'\x01\x02\x03\x04')
        self.assertEqual(MC_Incoming(raw).transport_codes, (0x0201, 0x0403))

    def test_truncated_transport_codes_are_rejected(self):
        raw = wire(T_FLOOD, payload=b'\x01', transport=(1, 2))
        for n in range(2, 5):
            with self.assertRaises(InvalidMeshcorePacket):
                MC_Incoming(raw[:n])


class PathHashSizeTests(unittest.TestCase):
    def test_two_byte_hashes(self):
        path = b'\xaa\xbb' b'\xcc\xdd' b'\xee\xff'
        p = MC_Incoming(wire(FLOOD, path=path, payload=b'\x01', hash_size=2))
        self.assertEqual(p.path_hash_size, 2)
        self.assertEqual(p.pathlen, 3)                 # three HASHES, six bytes
        self.assertEqual(len(p.path), 6)
        self.assertEqual(bytes(p.path), path)

    def test_three_byte_hashes(self):
        path = bytes(range(9))
        p = MC_Incoming(wire(DIRECT, path=path, payload=b'\x01', hash_size=3))
        self.assertEqual(p.path_hash_size, 3)
        self.assertEqual(p.pathlen, 3)
        self.assertEqual(bytes(p.path), path)

    def test_hash_size_four_is_reserved_and_rejected(self):
        # isValidPathLen: `if (hash_size == 4) return false;`
        raw = bytes([hdr(FLOOD), (3 << 6) | 1]) + b'\x00' * 4 + b'\x01'
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Incoming(raw)

    def test_path_longer_than_max_path_size_is_rejected(self):
        # 33 hashes * 2 bytes = 66 > MAX_PATH_SIZE(64)
        raw = bytes([hdr(FLOOD), encoded_pathlen(2, 33)]) + b'\x00' * 66 + b'\x01'
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Incoming(raw)

    def test_maximum_legal_path_is_accepted(self):
        # 32 hashes * 2 bytes = 64 == MAX_PATH_SIZE
        raw = bytes([hdr(FLOOD), encoded_pathlen(2, 32)]) + b'\x5a' * 64 + b'\x01'
        p = MC_Incoming(raw)
        self.assertEqual(p.pathlen, 32)
        self.assertEqual(len(p.path), 64)


class MalformedTests(unittest.TestCase):
    def test_packet_shorter_than_two_bytes(self):
        for raw in (b'', b'\x01'):
            with self.assertRaises(InvalidMeshcorePacket):
                MC_Incoming(raw)

    def test_path_running_past_the_end_is_rejected(self):
        raw = bytes([hdr(FLOOD), encoded_pathlen(1, 10)]) + b'\x00' * 3
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Incoming(raw)

    def test_packet_with_no_payload_is_rejected(self):
        # Upstream readFrom: `if (i >= len) return false;`
        raw = bytes([hdr(FLOOD), encoded_pathlen(1, 2)]) + b'\x01\x02'
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Incoming(raw)

    def test_truncation_at_every_offset_never_crashes(self):
        raw = wire(T_DIRECT, path=b'\x01\x02\x03\x04', payload=b'\x09\x08',
                   hash_size=2, transport=(0x1111, 0x2222))
        for n in range(len(raw)):
            try:
                MC_Incoming(raw[:n])
            except InvalidMeshcorePacket:
                pass                                   # the only acceptable failure

    def test_oversized_payload_is_rejected(self):
        raw = bytes([hdr(FLOOD), 0]) + b'\x00' * (MC_Packet.MAX_PACKET_PAYLOAD + 1)
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Incoming(raw)


class RoundTripTests(unittest.TestCase):
    """decode -> encode must reproduce the original bytes exactly, including transport
    codes and the encoded path-length byte."""

    CASES = [
        ("flood, no path", wire(FLOOD, payload=b'\x01\x02\x03')),
        ("flood, 1-byte path", wire(FLOOD, path=b'\x11\x22', payload=b'\xaa')),
        ("direct, 1-byte path", wire(DIRECT, path=b'\x33', payload=b'\xbb')),
        ("flood, 2-byte hashes", wire(FLOOD, path=b'\x01\x02\x03\x04',
                                      payload=b'\xcc', hash_size=2)),
        ("direct, 3-byte hashes", wire(DIRECT, path=bytes(range(6)),
                                       payload=b'\xdd', hash_size=3)),
        ("transport flood", wire(T_FLOOD, path=b'\x09', payload=b'\xee',
                                 transport=(0x1234, 0x5678))),
        ("transport direct", wire(T_DIRECT, path=b'\x0a\x0b', payload=b'\xff',
                                  transport=(0xffff, 0x0000))),
        ("transport direct, 2-byte hashes",
         wire(T_DIRECT, path=b'\x01\x02\x03\x04', payload=b'\x77',
              hash_size=2, transport=(0x0102, 0x0304))),
    ]

    def test_round_trips(self):
        for name, raw in self.CASES:
            with self.subTest(name):
                p = MC_Incoming(raw)
                self.assertEqual(bytes(p.packet), raw, name)

    def test_transport_codes_survive_a_forward(self):
        """A repeated transport packet must keep its codes — losing them would strip the
        transport route's meaning while still advertising the route type."""
        raw = wire(T_FLOOD, path=b'\x01', payload=b'\x42', transport=(0xdead, 0xbeef))
        p = MC_Incoming(raw)
        p.path += b'\x99'                              # as a flood repeat would
        out = MC_Incoming(bytes(p.packet))
        self.assertEqual(out.transport_codes, (0xdead, 0xbeef))
        self.assertEqual(bytes(out.path), b'\x01\x99')
        self.assertEqual(out.payload, b'\x42')


class EncodingHelperTests(unittest.TestCase):
    def test_encoded_pathlen_matches_upstream_formula(self):
        for size in (1, 2, 3):
            for count in (0, 1, 5, 21, 63):
                p = MC_Packet()
                p.path_hash_size = size
                p.path = bytearray(size * count)
                self.assertEqual(p.encoded_pathlen, encoded_pathlen(size, count),
                                 f"size={size} count={count}")

    def test_decode_pathlen_matches_upstream_formula(self):
        for size in (1, 2, 3):
            for count in (0, 1, 63):
                if count * size > MC_Packet.MAX_PATH_SIZE:
                    continue
                got = MC_Packet.decode_pathlen(encoded_pathlen(size, count))
                self.assertEqual(got, (size, count, count * size))

    def test_a_one_byte_hash_encodes_as_a_bare_count(self):
        """Backward compatibility: legacy packets are byte-identical to before."""
        for count in range(64):
            self.assertEqual(encoded_pathlen(1, count), count)


class NewPayloadTypeTests(unittest.TestCase):
    """MULTIPART/CONTROL exist upstream. This node does not implement their semantics,
    but the parser must not destructively misread them — they decode as ordinary
    unknown-type packets and round-trip intact."""

    def test_multipart_and_control_parse_without_corruption(self):
        for ptype in (MC_Packet.TYPE_MULTIPART, MC_Packet.TYPE_CONTROL):
            with self.subTest(ptype=ptype):
                raw = wire(FLOOD, path=b'\x01', payload=b'\x0a\x0b\x0c', ptype=ptype)
                p = MC_Incoming(raw)
                self.assertEqual(p.type, ptype)
                self.assertEqual(p.payload, b'\x0a\x0b\x0c')
                self.assertEqual(bytes(p.packet), raw)


if __name__ == '__main__':
    unittest.main()


class AuditRegressionTests(unittest.TestCase):
    """Findings from the P0-P2 self-audit of this change set."""

    def test_an_advert_claiming_a_position_it_does_not_carry_is_rejected(self):
        """P0: the flags byte can claim LATLON while the sentence carries none. The short
        slice then made `struct.unpack` raise `struct.error` — not an InvalidPacket — which
        escaped the RX loop and terminated the process. Any transmitter on the frequency
        could do it, unauthenticated, before the advert's signature was ever checked."""
        raw = bytes([hdr(FLOOD, MC_Packet.TYPE_ADVERT)]) + bytes([0]) \
            + b'\x00' * 32 + b'\x00' * 4 + b'\x00' * 64 + bytes([0x10])
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Incoming.convert_packet(raw, None, None, None)

    def test_str_prints_whole_multi_byte_hops(self):
        """The path is count*size bytes; slicing by count truncated it."""
        p = MC_Incoming(wire(FLOOD, path=b'\xaa\xbb\xcc\xdd', payload=b'\x01', hash_size=2))
        out = str(p)
        self.assertIn("aabb", out)
        self.assertIn("ccdd", out)
