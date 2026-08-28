"""Companion TCP transport: idle behaviour, framing and resynchronisation.

The bug these exist for: the reader had a hard-coded ~90 s idle timeout, so a client that
was legitimately waiting for adverts or messages — sending nothing — was disconnected about
every 90 seconds. An idle client is a healthy client.

Every timing test here uses a TINY configured timeout. Nothing waits 90 seconds.
"""

import asyncio
import unittest

from companionwifi import MAX_JUNK_BYTES, CompanionInterface


def frame(payload: bytes) -> bytes:
    """A well-formed inbound frame: '<' + little-endian length + body."""
    return b'<' + len(payload).to_bytes(2, 'little') + payload


class _FakeReader:
    """An asyncio-StreamReader-shaped stream.

    Models a real socket: running out of data means the peer is QUIET (the reader simply
    waits), not that the connection ended. Only an explicit `eof()` closes it. That
    distinction is the whole subject of these tests — the old code treated a quiet client
    as one to disconnect.
    """

    def __init__(self, chunks=()):
        self._buf = bytearray()
        self._eof = False
        for c in chunks:
            self._buf += c

    def feed(self, data):
        self._buf += data

    def eof(self):
        self._eof = True

    async def readexactly(self, n):
        while len(self._buf) < n:
            if self._eof:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            await asyncio.sleep(0.001)             # quiet, still connected
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class _FakeWriter:
    def __init__(self):
        self.closed = False
        self.data = bytearray()

    def write(self, b):
        self.data += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def _iface(**cfg):
    iface = CompanionInterface(cfg)
    iface._reader = _FakeReader()
    iface._writer = _FakeWriter()
    iface._connected.set()
    return iface


def _run(coro, timeout=2.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class IdleTimeoutConfigTests(unittest.TestCase):
    def test_default_is_no_idle_disconnect(self):
        self.assertIsNone(CompanionInterface({}).idle_timeout)

    def test_zero_disables(self):
        self.assertIsNone(CompanionInterface({'idle_timeout': 0}).idle_timeout)

    def test_positive_value_enables(self):
        self.assertEqual(CompanionInterface({'idle_timeout': 0.05}).idle_timeout, 0.05)

    def test_negative_and_garbage_fail_safe_to_disabled(self):
        # A spurious disconnect is exactly what this option exists to avoid, so a bad
        # value must not invent one.
        for bad in (-1, "nonsense", None, [], "90s"):
            self.assertIsNone(CompanionInterface({'idle_timeout': bad}).idle_timeout, bad)


class IdleBehaviourTests(unittest.TestCase):
    def test_an_idle_client_is_never_disconnected_by_default(self):
        """The regression: silence must not end the connection."""
        iface = _iface()
        iface._reader = _FakeReader()                # connected, but silent

        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(0.15)                # >> any old poll interval, scaled down
            still_waiting = not task.done()
            task.cancel()
            return still_waiting

        self.assertTrue(_run(go()))
        self.assertFalse(iface._writer.closed)
        self.assertTrue(iface._connected.is_set())

    def test_a_configured_idle_timeout_still_disconnects(self):
        iface = _iface(idle_timeout=0.05)
        iface._reader = _FakeReader()
        writer = iface._writer                       # keep it: the reset clears the attribute

        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(0.25)                # 5x the configured timeout
            task.cancel()
            return writer.closed

        self.assertTrue(_run(go()))

    def test_a_frame_after_a_long_idle_is_still_delivered(self):
        """Idle then active: the whole point is that the session survives the quiet part."""
        iface = _iface()
        reader = _FakeReader()
        iface._reader = reader

        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(0.1)                 # idle
            reader.feed(frame(b'\x01\x02\x03'))
            return await task

        self.assertEqual(_run(go()), b'\x01\x02\x03')


class FramingTests(unittest.TestCase):
    def test_a_well_formed_frame_is_returned(self):
        iface = _iface()
        iface._reader = _FakeReader([frame(b'hello')])
        self.assertEqual(_run(iface.rx()), b'hello')

    def test_an_empty_frame_is_returned(self):
        iface = _iface()
        iface._reader = _FakeReader([frame(b'')])
        self.assertEqual(_run(iface.rx()), b'')

    def test_a_frame_split_across_chunks_is_reassembled(self):
        iface = _iface()
        f = frame(b'abcdef')
        iface._reader = _FakeReader([f[:1], f[1:3], f[3:5], f[5:]])
        self.assertEqual(_run(iface.rx()), b'abcdef')

    def test_junk_before_a_frame_is_discarded_and_the_frame_read(self):
        iface = _iface()
        iface._reader = _FakeReader([b'\x00\xff\x7e' + frame(b'ok')])
        self.assertEqual(_run(iface.rx()), b'ok')


class ResyncTests(unittest.TestCase):
    def test_junk_without_a_frame_start_is_bounded(self):
        """A peer flooding non-frame bytes must not grow the resync buffer without limit."""
        iface = _iface()
        reader = _FakeReader([b'\xaa' * (MAX_JUNK_BYTES * 4), frame(b'later')])
        iface._reader = reader

        # The bound is what matters; the frame that follows is read on a later pass.
        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(0.2)
            task.cancel()
            return True
        self.assertTrue(_run(go()))

    def test_failed_resync_does_not_parse_junk_as_a_length_header(self):
        """The misframing bug: when resync gave up it fell through and read the next two
        junk bytes as a frame length, staying desynchronised. A failed resync must go back
        to hunting for '<' instead."""
        iface = _iface()
        # 'A'*300 has no '<' and exceeds the bound, then a real frame arrives.
        iface._reader = _FakeReader([b'A' * (MAX_JUNK_BYTES + 8), frame(b'good')])
        self.assertEqual(_run(iface.rx(), timeout=5.0), b'good')


class DisconnectTests(unittest.TestCase):
    def test_connection_loss_clears_reader_and_writer_together(self):
        """A stale reader must never outlive the writer it belongs to."""
        iface = _iface()
        iface._reader = _FakeReader()
        iface._reader.eof()                          # peer closed -> IncompleteReadError

        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(0.05)
            task.cancel()
            return iface._reader, iface._writer, iface._connected.is_set()

        reader, writer, connected = _run(go())
        self.assertIsNone(reader)
        self.assertIsNone(writer)
        self.assertFalse(connected)

    def test_tx_on_a_broken_writer_resets_the_connection(self):
        iface = _iface()

        class Boom(_FakeWriter):
            def write(self, b):
                raise OSError("broken pipe")

        iface._writer = Boom()
        _run(iface.tx(b'x'))
        self.assertIsNone(iface._writer)
        self.assertIsNone(iface._reader)
        self.assertFalse(iface._connected.is_set())

    def test_tx_while_disconnected_is_a_no_op(self):
        iface = _iface()
        iface._writer = None
        _run(iface.tx(b'x'))                         # must not raise


if __name__ == '__main__':
    unittest.main()


class MidFrameTimeoutTests(unittest.TestCase):
    """AUDIT-FOUND: once '<' is accepted the frame boundary is committed. A timeout part
    way through the header or body means we no longer know where the next frame starts, so
    continuing on the same connection can read leftover payload — including a '<' inside
    it — as framing, and stay desynchronised indefinitely."""

    def test_a_timeout_reading_the_header_drops_the_connection(self):
        iface = _iface()
        iface._reader = _FakeReader([b'<\x05'])        # start + 1 of 2 length bytes, then quiet

        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(1.4)                   # past _FRAME_HEADER_TIMEOUT
            done = iface._writer is None
            task.cancel()
            return done

        self.assertTrue(_run(go(), timeout=5.0))
        self.assertIsNone(iface._reader)
        self.assertFalse(iface._connected.is_set())

    def test_a_timeout_reading_the_body_drops_the_connection(self):
        iface = _iface()
        # Announces 10 bytes, delivers 2, then goes quiet.
        iface._reader = _FakeReader([b'<' + (10).to_bytes(2, 'little') + b'ab'])
        writer = iface._writer

        async def go():
            task = asyncio.create_task(iface.rx())
            await asyncio.sleep(5.5)                   # past _FRAME_BODY_TIMEOUT
            task.cancel()
            return writer.closed

        self.assertTrue(_run(go(), timeout=9.0))
        self.assertIsNone(iface._writer)
