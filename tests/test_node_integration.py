"""End-to-end: a real node against a fake LoRaHAM daemon and a real companion client.

This is the test that would have caught the bugs the unit tests cannot see on their own —
the pieces are wired together the way they run in production:

    fake loraham daemon  <--unix sockets-->  LoRaHAMInterface
                                                  |
                                             dispatcher
                                                  |
                                            CompanionRadio
                                                  |
                                       TCP :port  <--->  companion client

No RF hardware is involved, so on-air behaviour is still unproven here; everything up to
and including the framed daemon protocol is real code.
"""

import asyncio
import struct
import tempfile
import unittest

import companionradio as cr
from configuration import get_config
from dispatch import Dispatch
from ed25519_wrapper import ED25519_Wrapper
from identity import AdvertType, IdentityStore, SelfIdentity
from interfaces.lorahaminterface import LoRaHAMInterface

from tests.fake_loraham_daemon import FakeLoRaHAMDaemon


def frame(payload: bytes) -> bytes:
    return b'<' + len(payload).to_bytes(2, 'little') + payload


class _Client:
    """A companion client speaking the real framed protocol over the real TCP port."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, port, timeout=10.0):
        # The node starts its listener in a background task, so the port appears shortly
        # after start() returns. A real client connects when the node is up, too.
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                return cls(reader, writer)
            except (ConnectionRefusedError, OSError):
                if asyncio.get_event_loop().time() > deadline:
                    raise
                await asyncio.sleep(0.05)

    async def send(self, payload):
        self.writer.write(frame(payload))
        await self.writer.drain()

    async def recv(self, timeout=5.0):
        """One response frame: '>' + LE length + body."""
        async def read():
            while True:
                start = await self.reader.readexactly(1)
                if start == b'>':
                    break
            size = struct.unpack("<H", await self.reader.readexactly(2))[0]
            return await self.reader.readexactly(size)
        return await asyncio.wait_for(read(), timeout)

    async def request(self, payload, timeout=5.0):
        """Send a command and return the first frame that is not an async push."""
        await self.send(payload)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_event_loop().time()
            resp = await self.recv(max(0.1, left))
            if not resp or resp[0] < 0x80:          # 0x80+ are unsolicited pushes
                return resp

    async def close(self):
        self.writer.close()


class NodeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks = []
        self.clients = []

        self.daemon = FakeLoRaHAMDaemon(self.tmp.name, tx=True)
        await self.daemon.start()

        self.iface = LoRaHAMInterface(get_config({
            "data_socket": str(self.daemon.data_socket),
            "config_socket": str(self.daemon.config_socket),
            "apply_config": False,
            "connect_timeout": 0.5,
            "tx_result_margin": 0.2,
            "enable_tx": True,
            "preset": "eu_uk_narrow",
        }))

        self.dispatcher = Dispatch()
        key = ED25519_Wrapper()
        self.me = SelfIdentity(private_key=key, name=b"IntegrationNode",
                               latlon=None, devicetype=AdvertType.CHAT)

        import groupchannel
        self.channels = groupchannel.channels(None, 4, True)
        # Bind a real, free port: ask the OS for one and release it immediately, so
        # parallel runs cannot collide on a fixed number.
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.radio = cr.CompanionRadio(
            self.me, IdentityStore(), self.channels, self.dispatcher,
            get_config({"interface": "wifi",
                        "wifi": {"port": self.port, "allow": "127.0.0.1"}}))

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await self.iface._close_sockets()
        except Exception:
            pass
        await self.daemon.close()
        self.tmp.cleanup()

    async def _start(self):
        """Bring the whole stack up and return a connected client."""
        from aiotools import TaskGroup

        started = asyncio.Event()

        async def run():
            async with TaskGroup() as tg:                     # noqa: F841
                await self.iface.start()
                self.dispatcher.add_interface(self.iface)
                await self.dispatcher.start()
                await self.radio.start()
                started.set()
                await asyncio.sleep(3600)

        self.tasks.append(asyncio.create_task(run()))
        await asyncio.wait_for(started.wait(), 10)
        client = await _Client.connect(self.port)
        self.clients.append(client)
        return client

    async def test_startup_device_query_and_battery(self):
        """The sequence a current client performs on connect."""
        client = await self._start()

        # CMD_APP_START -> RESP_CODE_SELF_INFO
        resp = await client.request(bytes([cr.CMD_APP_START, 1]) + b'\0' * 6 + b'test')
        self.assertEqual(resp[0], cr.RESP_CODE_SELF_INFO)

        # CMD_DEVICE_QUERY -> RESP_CODE_DEVICE_INFO, at the advertised version
        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY, 3]))
        self.assertEqual(resp[0], cr.RESP_CODE_DEVICE_INFO)
        self.assertEqual(resp[1], cr.FIRMWARE_VER_CODE)

        # CMD_GET_BATT_AND_STORAGE -> the full 11-byte battery+storage frame
        resp = await client.request(bytes([cr.CMD_GET_BATT_AND_STORAGE]))
        self.assertEqual(resp[0], cr.RESP_CODE_BATT_AND_STORAGE)
        self.assertEqual(len(resp), 11)
        _c, mv, used, total = struct.unpack("<BHII", resp)
        self.assertEqual((mv, used, total), (cr.BATTERY_NOT_APPLICABLE_MV, 0, 0))

    async def test_an_unsupported_command_is_reported_as_such(self):
        client = await self._start()
        resp = await client.request(bytes([200]))          # no such command
        self.assertEqual(resp[0], cr.RESP_CODE_ERR)
        self.assertEqual(resp[1], cr.ERR_CODE_UNSUPPORTED_CMD)

    async def test_self_advert_is_transmitted_through_the_daemon(self):
        """TX path: a client-requested advert must reach the daemon as a real TX_PACKET."""
        client = await self._start()
        await client.request(bytes([cr.CMD_APP_START, 1]) + b'\0' * 6 + b'test')

        before = len(self.daemon.tx_packets)
        resp = await client.request(bytes([cr.CMD_SEND_SELF_ADVERT]))
        self.assertEqual(resp[0], cr.RESP_CODE_OK)

        for _ in range(100):
            if len(self.daemon.tx_packets) > before:
                break
            await asyncio.sleep(0.05)
        self.assertGreater(len(self.daemon.tx_packets), before,
                           "the advert never reached the daemon")

        # And it is a well-formed advert on the wire. The default is a ZERO-HOP advert
        # (direct, empty path); flood is opt-in via the second byte.
        from packet import MC_Incoming, MC_Packet
        p = MC_Incoming(bytes(self.daemon.tx_packets[-1]))
        self.assertEqual(p.type, MC_Packet.TYPE_ADVERT)
        self.assertTrue(p.is_direct())
        self.assertEqual(p.pathlen, 0)

    async def test_a_flood_advert_is_transmitted_as_flood(self):
        """Its own test on a FRESH node: an advert's packet hash covers type+payload, not
        the route, so a second advert with the same timestamp is correctly deduplicated —
        asking for both in one test would silently measure the first one twice."""
        client = await self._start()
        await client.request(bytes([cr.CMD_APP_START, 1]) + b'\0' * 6 + b'test')

        before = len(self.daemon.tx_packets)
        resp = await client.request(bytes([cr.CMD_SEND_SELF_ADVERT, 1]))
        self.assertEqual(resp[0], cr.RESP_CODE_OK)
        for _ in range(100):
            if len(self.daemon.tx_packets) > before:
                break
            await asyncio.sleep(0.05)
        self.assertGreater(len(self.daemon.tx_packets), before)

        from packet import MC_Incoming, MC_Packet
        p = MC_Incoming(bytes(self.daemon.tx_packets[-1]))
        self.assertEqual(p.type, MC_Packet.TYPE_ADVERT)
        self.assertTrue(p.is_flood())

    async def test_a_truncated_frame_does_not_kill_the_command_loop(self):
        """A command handler indexes its arguments directly, so a short frame used to raise
        straight out of the serving loop — ending command processing for the life of the
        process. One malformed frame must not be a denial of service."""
        client = await self._start()

        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY]))       # missing version
        self.assertEqual(resp[0], cr.RESP_CODE_ERR)

        # Still serving afterwards.
        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY, 3]))
        self.assertEqual(resp[0], cr.RESP_CODE_DEVICE_INFO)

    async def test_an_old_protocol_version_is_refused_without_ending_the_session(self):
        """This used to `break` out of the serving loop, so one frame from an old client
        stopped the node answering anything, forever."""
        client = await self._start()

        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY, 1]))    # version < 3
        self.assertEqual(resp[0], cr.RESP_CODE_ERR)

        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY, 3]))
        self.assertEqual(resp[0], cr.RESP_CODE_DEVICE_INFO)

    async def test_an_empty_frame_is_ignored(self):
        client = await self._start()
        await client.send(b'')
        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY, 3]))
        self.assertEqual(resp[0], cr.RESP_CODE_DEVICE_INFO)

    async def test_an_idle_client_stays_connected(self):
        """The regression, end to end: no traffic for well over a poll interval, and the
        session is still usable afterwards."""
        client = await self._start()
        await client.request(bytes([cr.CMD_APP_START, 1]) + b'\0' * 6 + b'test')

        await asyncio.sleep(1.5)                # silence

        resp = await client.request(bytes([cr.CMD_DEVICE_QUERY, 3]))
        self.assertEqual(resp[0], cr.RESP_CODE_DEVICE_INFO)


if __name__ == '__main__':
    unittest.main()
