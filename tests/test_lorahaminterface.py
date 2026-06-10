import asyncio
import tempfile
import unittest

from configuration import get_config
from interfaces.lorahaminterface import LoRaHAMInterface

from tests.fake_loraham_daemon import FakeLoRaHAMDaemon


class LoRaHAMInterfaceFunctionalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.daemons = []
        self.interfaces = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

        for iface in self.interfaces:
            await iface._close_sockets()

        for daemon in self.daemons:
            await daemon.close()

        self.tmp.cleanup()

    async def make_daemon(self, **kwargs):
        daemon = FakeLoRaHAMDaemon(self.tmp.name, **kwargs)
        await daemon.start()
        self.daemons.append(daemon)
        return daemon

    def make_interface(self, daemon, **overrides):
        config_data = {
            "data_socket": str(daemon.data_socket),
            "config_socket": str(daemon.config_socket),
            "apply_config": False,
            "connect_timeout": 0.5,
            "status_wait_timeout": 0.1,
            "busy_wait_timeout": 0.2,
            "tx_delay": 0.05,
        }
        config_data.update(overrides)

        iface = LoRaHAMInterface(get_config(config_data))
        self.interfaces.append(iface)
        return iface

    async def connect_interface(self, daemon, **overrides):
        iface = self.make_interface(daemon, **overrides)
        await iface._connect_sockets()

        self.tasks.append(asyncio.create_task(iface._data_reader_loop()))
        self.tasks.append(asyncio.create_task(iface._config_reader_loop()))

        await self.wait_status(iface)
        return iface

    async def wait_status(self, iface, timeout=1.0):
        async with iface._status_condition:
            await asyncio.wait_for(
                iface._status_condition.wait_for(iface._status_seen),
                timeout=timeout,
            )

    async def test_status_lines_track_tx_and_cad_state(self):
        daemon = await self.make_daemon(tx=False, cad=False)
        iface = await self.connect_interface(daemon)

        self.assertFalse(iface._tx_busy)
        self.assertFalse(iface._cad_busy)

        await daemon.set_status(tx=True)
        async with iface._status_condition:
            await asyncio.wait_for(
                iface._status_condition.wait_for(lambda: iface._tx_busy),
                timeout=1.0,
            )

        await daemon.set_status(cad=True)
        async with iface._status_condition:
            await asyncio.wait_for(
                iface._status_condition.wait_for(lambda: iface._cad_busy),
                timeout=1.0,
            )

    async def test_free_status_sends_immediately_without_tx_delay(self):
        daemon = await self.make_daemon(tx=False, cad=False)
        iface = await self.connect_interface(daemon, tx_delay=0.5)

        await asyncio.wait_for(iface.transmit(b"free"), timeout=0.2)
        await daemon.wait_tx(b"free")

    async def test_busy_then_free_waits_tx_delay_before_sending(self):
        daemon = await self.make_daemon(tx=False, cad=True)
        iface = await self.connect_interface(
            daemon,
            busy_wait_timeout=1.0,
            tx_delay=0.25,
        )

        tx_task = asyncio.create_task(iface.transmit(b"settle"))
        await asyncio.sleep(0.05)
        self.assertNotIn(b"settle", daemon.tx_packets)

        await daemon.set_status(cad=False)
        await asyncio.sleep(0.10)
        self.assertNotIn(b"settle", daemon.tx_packets)

        await daemon.wait_tx(b"settle", timeout=1.0)
        await tx_task

    async def test_tx_busy_timeout_does_not_send(self):
        daemon = await self.make_daemon(tx=True, cad=False)
        iface = await self.connect_interface(
            daemon,
            busy_wait_timeout=0.05,
        )

        await iface.transmit(b"blocked")
        await asyncio.sleep(0.05)

        self.assertNotIn(b"blocked", daemon.tx_packets)

    async def test_cad_busy_timeout_sends_anyway(self):
        daemon = await self.make_daemon(tx=False, cad=True)
        iface = await self.connect_interface(
            daemon,
            busy_wait_timeout=0.05,
        )

        await iface.transmit(b"cad-timeout")

        await daemon.wait_tx(b"cad-timeout", timeout=1.0)

    async def test_missing_status_after_get_status_does_not_send(self):
        daemon = await self.make_daemon(respond_to_status=False)
        iface = self.make_interface(
            daemon,
            status_wait_timeout=0.05,
            busy_wait_timeout=0.05,
        )
        await iface._connect_sockets()

        self.tasks.append(asyncio.create_task(iface._data_reader_loop()))
        self.tasks.append(asyncio.create_task(iface._config_reader_loop()))

        await iface.transmit(b"no-status")
        await asyncio.sleep(0.05)

        self.assertNotIn(b"no-status", daemon.tx_packets)
        self.assertGreaterEqual(daemon.config_commands.count("GET STATUS"), 1)

    async def test_rx_packet_frame_is_forwarded_to_rx_queue(self):
        daemon = await self.make_daemon(tx=False, cad=False)
        iface = await self.connect_interface(daemon)

        await daemon.send_rx(b"rx-payload")
        packet = await asyncio.wait_for(iface.rx_q.get(), timeout=1.0)

        self.assertEqual(packet, bytearray(b"rx-payload"))


if __name__ == "__main__":
    unittest.main()
