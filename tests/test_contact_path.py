"""Contact path representation: one encoding from the wire to storage and back.

A route is (bytes-per-hop, hop count, bytes). Dropping the size anywhere in that chain
turns a learned multi-byte route into a different, wrong route on the next send — silently,
because every layer still holds a plausible-looking path.

Current MeshCore contact frame (examples/companion_radio/MyMesh.cpp writeContactRespFrame):
    code(1) pub_key(32) type(1) flags(1) out_path_len(1) out_path(64, FIXED) name(32) ...
with out_path_len encoded as (hash_size-1)<<6 | hop_count, and OUT_PATH_UNKNOWN = 0xFF.

Upstream reference: meshcore-dev/MeshCore @ 0679dbeffc504d562d2f09eb072fdc223f8ffc2a
Client reference:   meshcore_py reads plen>>6 and plen&0x3F; meshcore.js reads 64 bytes.
"""

import struct
import unittest

import companionradio as cr
from packet import MC_Packet


class OutPathLenEncodingTests(unittest.TestCase):
    def test_unknown_is_0xff_not_zero(self):
        """0 means "zero-hop direct" — claiming a direct route to a node we have never
        reached. Unknown must be 0xff so the client floods instead."""
        self.assertEqual(cr.OUT_PATH_UNKNOWN, 0xFF)

    def test_encoding_matches_upstream(self):
        for size in (1, 2, 3):
            for count in (0, 1, 5, 20):
                enc = ((size - 1) << 6) | count
                self.assertEqual(MC_Packet.decode_pathlen(enc), (size, count, count * size))

    def test_a_one_byte_route_still_encodes_as_a_bare_count(self):
        for count in range(64):
            self.assertEqual(((1 - 1) << 6) | count, count)


class _Advert:
    def __init__(self):
        self.adv_type = type("T", (), {"value": 1})()
        self.adv_flags = type("F", (), {"value": 0x80})()


class _Contact:
    def __init__(self, path, size=1):
        self.pubkey = bytes(range(32))
        self.advert = _Advert()
        self.path = path
        self.path_hash_size = size
        self.name = b"peer"
        self.timestamp = 1234
        self.latlon = None


class ContactFrameTests(unittest.TestCase):
    """The frame this node emits, checked field by field against the upstream layout."""

    def _frame(self, path, size=1):
        radio = cr.CompanionRadio.__new__(cr.CompanionRadio)
        return radio.contactframe(cr.RESP_CODE_CONTACT, _Contact(path, size))

    def test_layout_is_fixed_width(self):
        f = self._frame(b'\x11\x22')
        # code + pubkey + type + flags + out_path_len + 64 path + 32 name + 4+4+4+4
        self.assertEqual(len(f), 1 + 32 + 1 + 1 + 1 + 64 + 32 + 16)

    def test_known_one_byte_route(self):
        f = self._frame(b'\x11\x22\x33')
        self.assertEqual(f[35], 3)                       # size 1, 3 hops
        self.assertEqual(f[36:39], b'\x11\x22\x33')

    def test_known_multi_byte_route_encodes_size_and_count(self):
        f = self._frame(b'\xaa\xbb\xcc\xdd', size=2)
        self.assertEqual(f[35], ((2 - 1) << 6) | 2)      # size 2, 2 hops
        self.assertEqual(f[36:40], b'\xaa\xbb\xcc\xdd')

    def test_zero_hop_direct_is_zero_not_unknown(self):
        f = self._frame(b'')
        self.assertEqual(f[35], 0)

    def test_unknown_route_is_0xff(self):
        """A contact we have no route to must not be advertised as zero-hop direct."""
        f = self._frame(None)
        self.assertEqual(f[35], cr.OUT_PATH_UNKNOWN)

    def test_path_field_is_always_64_bytes(self):
        for path, size in ((None, 1), (b'', 1), (b'\x01', 1), (b'\x01\x02', 2)):
            f = self._frame(path, size)
            self.assertEqual(len(f[36:100]), 64)
            self.assertEqual(f[100:104], b'peer')        # name starts right after


class ContactParseTests(unittest.TestCase):
    """Parsing a contact the app sends us. The path field is FIXED 64 bytes: consuming only
    the hop bytes desynchronised name, timestamps and position for every short path."""

    def _contactdata(self, out_path_len, path):
        return (bytes(range(32)) + bytes([1, 0x80, out_path_len])
                + path + b'\x00' * (64 - len(path))
                + b'peer'.ljust(32, b'\x00')
                + struct.pack("<Lll", 1234, 0, 0))

    def test_fields_after_a_short_path_are_still_aligned(self):
        data = self._contactdata(2, b'\x11\x22')
        rest = data[35 + 64:]
        self.assertEqual(rest[0:32].rstrip(b'\x00'), b'peer')
        self.assertEqual(struct.unpack("<L", rest[32:36])[0], 1234)

    def test_decoding_a_multi_byte_route(self):
        enc = ((2 - 1) << 6) | 2
        size, count, nbytes = MC_Packet.decode_pathlen(enc)
        self.assertEqual((size, count, nbytes), (2, 2, 4))

    def test_unknown_route_decodes_as_no_path(self):
        self.assertEqual(cr.OUT_PATH_UNKNOWN, 0xFF)
        # 0xff is not a valid encoded length; it must be handled before decoding.
        from exceptions import InvalidMeshcorePacket
        with self.assertRaises(InvalidMeshcorePacket):
            MC_Packet.decode_pathlen(0xFF)               # size 4 = reserved


class IdentityCarriesSizeTests(unittest.TestCase):
    def test_identity_defaults_to_one_byte_hops(self):
        from identity import Identity
        self.assertEqual(Identity.__init__.__defaults__[-1], 1)

    def test_a_learned_route_keeps_its_size_through_storage(self):
        """Round-trip through the on-disk contact file."""
        import tempfile, os
        from ed25519_wrapper import ED25519_Wrapper
        from identity import AdvertType, FileIdentityStore, SelfIdentity
        key = ED25519_Wrapper()
        me = SelfIdentity(private_key=key, name=b"me", latlon=None,
                          devicetype=AdvertType.CHAT)
        peerkey = ED25519_Wrapper()
        peer = SelfIdentity(private_key=peerkey, name=b"peer", latlon=None,
                            devicetype=AdvertType.CHAT)

        from identity import AdvertData, Identity
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "contacts.mesh")
            store = FileIdentityStore(path, key)
            ident = Identity(AdvertData(peer.data), bytearray(b'\xaa\xbb\xcc\xdd'),
                             path_hash_size=2)
            ident.create_shared_secret(key)
            store.add_identity(ident)

            reloaded = FileIdentityStore(path, key)
            got = reloaded.find_by_pubkey(peer.private_key.public_key)
            self.assertIsNotNone(got)
            self.assertEqual(bytes(got.path), b'\xaa\xbb\xcc\xdd')
            self.assertEqual(got.path_hash_size, 2)

    def test_a_legacy_file_without_a_size_loads_as_one(self):
        import tempfile, os
        from ed25519_wrapper import ED25519_Wrapper
        from identity import AdvertData, AdvertType, FileIdentityStore, Identity, SelfIdentity
        key = ED25519_Wrapper()
        peer = SelfIdentity(private_key=ED25519_Wrapper(), name=b"peer", latlon=None,
                            devicetype=AdvertType.CHAT)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "contacts.mesh")
            store = FileIdentityStore(path, key)
            ident = Identity(AdvertData(peer.data), bytearray(b'\x11\x22'))
            ident.create_shared_secret(key)
            store.add_identity(ident)
            # No "*size" suffix was written for the default.
            self.assertNotIn("*", open(path).read())
            got = FileIdentityStore(path, key).find_by_pubkey(peer.private_key.public_key)
            self.assertEqual(got.path_hash_size, 1)


class DirectTxUsesTheLearnedSizeTests(unittest.TestCase):
    def test_outgoing_packet_inherits_the_destination_hash_size(self):
        """The whole point: a route learned with 2-byte hops must go out with 2-byte hops,
        or the hop count on the wire is wrong."""
        import packet
        from ed25519_wrapper import ED25519_Wrapper
        from identity import AdvertData, AdvertType, Identity, SelfIdentity
        me = SelfIdentity(private_key=ED25519_Wrapper(), name=b"me", latlon=None,
                          devicetype=AdvertType.CHAT)
        peer = SelfIdentity(private_key=ED25519_Wrapper(), name=b"peer", latlon=None,
                            devicetype=AdvertType.CHAT)
        dest = Identity(AdvertData(peer.data), bytearray(b'\xaa\xbb\xcc\xdd'),
                        path_hash_size=2)
        dest.create_shared_secret(me.private_key)
        p = packet.MC_Text_Out(me, dest, b"hi")
        self.assertEqual(p.path_hash_size, 2)
        self.assertEqual(p.pathlen, 2)                   # two HOPS, four bytes
        self.assertEqual(p.encoded_pathlen, ((2 - 1) << 6) | 2)


if __name__ == '__main__':
    unittest.main()


class OutboundAbstractionTests(unittest.TestCase):
    """AUDIT-FOUND: the size must arrive WITH the path at MC_Outgoing, not be patched on
    afterwards. Two outbound paths bypassed the abstraction and reverted to 1-byte hops,
    and validation ran before the size was known."""

    def _pair(self):
        from ed25519_wrapper import ED25519_Wrapper
        from identity import AdvertData, AdvertType, Identity, SelfIdentity
        me = SelfIdentity(private_key=ED25519_Wrapper(), name=b"me", latlon=None,
                          devicetype=AdvertType.CHAT)
        peer = SelfIdentity(private_key=ED25519_Wrapper(), name=b"peer", latlon=None,
                            devicetype=AdvertType.CHAT)
        dest = Identity(AdvertData(peer.data), bytearray(b'\xaa\xbb\xcc\xdd'),
                        path_hash_size=2)
        dest.create_shared_secret(me.private_key)
        return me, dest

    def test_a_full_length_multi_byte_route_is_not_rejected(self):
        """32 hops x 2 bytes = 64 = MAX_PATH_SIZE. Validating before the size was applied
        made this look like 64 one-byte hops and refused it."""
        import packet
        p = packet.MC_Outgoing(packet.MC_Packet.TYPE_RAW_CUSTOM,
                               bytearray(b'\x5a' * 64), path_hash_size=2)
        self.assertEqual(p.pathlen, 32)
        self.assertEqual(p.encoded_pathlen, ((2 - 1) << 6) | 32)

    def test_too_many_hops_is_still_rejected(self):
        import packet
        with self.assertRaises(ValueError):
            packet.MC_Outgoing(packet.MC_Packet.TYPE_RAW_CUSTOM,
                               bytearray(b'\x01' * 64), path_hash_size=1)   # 64 hops > 63

    def test_a_path_longer_than_max_path_size_is_rejected(self):
        import packet
        with self.assertRaises(ValueError):
            packet.MC_Outgoing(packet.MC_Packet.TYPE_RAW_CUSTOM,
                               bytearray(b'\x01' * 66), path_hash_size=2)

    def test_an_ack_replies_over_the_route_it_arrived_on(self):
        """A direct message received over a learned 2-byte route must be ACKed over that
        same route — not re-encoded as twice as many one-byte hops."""
        import packet
        me, dest = self._pair()
        text = packet.MC_Text_Out(me, dest, b"hi")
        ack = packet.MC_Ack_Outgoing(text, dest.path, path_hash_size=dest.path_hash_size)
        self.assertEqual(ack.path_hash_size, 2)
        self.assertEqual(ack.pathlen, 2)
        self.assertEqual(ack.encoded_pathlen, ((2 - 1) << 6) | 2)

    def test_an_anonymous_request_inherits_the_destination_size(self):
        import packet
        me, dest = self._pair()
        req = packet.MC_AnonReq_Out(me, dest, b"")
        self.assertEqual(req.path_hash_size, 2)
        self.assertEqual(req.pathlen, 2)

    def test_every_contact_derived_direct_packet_carries_the_size(self):
        """Sweep the outbound types that take a destination contact."""
        import packet
        me, dest = self._pair()
        for name, p in (
            ("text", packet.MC_Text_Out(me, dest, b"hi")),
            ("request", packet.MC_Req_Out(me, dest, 0x01, b"")),
            ("anonreq", packet.MC_AnonReq_Out(me, dest, b"")),
        ):
            with self.subTest(name):
                self.assertEqual(p.path_hash_size, 2, name)
                self.assertEqual(p.pathlen, 2, name)
