import asyncio
import tempfile
import time
import unittest

from configuration import get_config
from interfaces.lorahaminterface import (
    LoRaHAMInterface,
    TX_RESULT_STATUS_OK,
    TX_RESULT_STATUS_BUSY,
    TX_RESULT_STATUS_CAD_TIMEOUT,
    TX_RESULT_STATUS_RADIO_ERROR,
)

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
            "tx_result_margin": 0.2,
            "enable_tx": True,
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

        return iface

    # --- Config / presets ---------------------------------------------------

    async def test_eu_uk_narrow_preset_uses_preamble_16(self):
        daemon = await self.make_daemon()
        iface = self.make_interface(daemon, preset="eu_uk_narrow")

        self.assertEqual(iface.freq, 869618000)
        self.assertEqual(iface.bw, 62500)
        self.assertEqual(iface.sf, 8)
        self.assertEqual(iface.cr, 8)
        self.assertEqual(iface.preamble, 16)
        self.assertEqual(iface.txpower, 14)

    async def test_eu_uk_medium_preset_uses_tested_values(self):
        daemon = await self.make_daemon()
        iface = self.make_interface(daemon, preset="eu_uk_medium")

        self.assertEqual(iface.freq, 869525000)
        self.assertEqual(iface.bw, 250000)
        self.assertEqual(iface.sf, 10)
        self.assertEqual(iface.cr, 5)
        self.assertEqual(iface.preamble, 16)
        self.assertFalse(iface.ldro)
        self.assertEqual(iface.txpower, 14)

    async def test_enable_tx_default_is_false(self):
        daemon = await self.make_daemon()
        iface = LoRaHAMInterface(get_config({
            "data_socket": str(daemon.data_socket),
            "config_socket": str(daemon.config_socket),
            "apply_config": False,
        }))
        self.interfaces.append(iface)

        self.assertFalse(iface.enable_tx)
        self.assertEqual(await iface.transmit(b"default-off"), 0)
        self.assertNotIn(b"default-off", daemon.tx_packets)

    async def test_txmaxpower_error_mentions_txpower_override(self):
        daemon = await self.make_daemon()

        with self.assertRaisesRegex(ValueError, "set txmaxpower >= txpower"):
            self.make_interface(daemon, txpower=20, txmaxpower=14)

    # --- Connect handshake --------------------------------------------------

    async def test_connect_sets_managed_txresult_and_reads_cadwait(self):
        daemon = await self.make_daemon(cadwait_ms=2000)
        iface = await self.connect_interface(daemon)

        self.assertIn("SET TXMODE=MANAGED", daemon.config_commands)
        self.assertIn("SET TXRESULT=1", daemon.config_commands)
        self.assertIn("GET STATUS", daemon.config_commands)
        self.assertEqual(iface._cadwait_s, 2.0)

    async def test_rx_only_does_not_change_tx_mode(self):
        daemon = await self.make_daemon()
        iface = await self.connect_interface(daemon, enable_tx=False)

        self.assertNotIn("SET TXMODE=MANAGED", daemon.config_commands)
        self.assertNotIn("SET TXRESULT=1", daemon.config_commands)
        self.assertIn("GET STATUS", daemon.config_commands)

    # --- RX path ------------------------------------------------------------

    async def test_rx_packet_yields_payload_rssi_snr_tuple(self):
        daemon = await self.make_daemon()
        iface = await self.connect_interface(daemon)

        await daemon.send_rx(b"rx-payload", rssi_cdbm=-9000, snr_cdb=550)
        packet = await asyncio.wait_for(iface.rx_q.get(), timeout=1.0)

        self.assertEqual(packet, (b"rx-payload", -90.0, 5.5))

    async def test_rx_packet_sentinel_signal_maps_to_zero(self):
        daemon = await self.make_daemon()
        iface = await self.connect_interface(daemon)

        await daemon.send_rx(b"no-signal", rssi_cdbm=-32768, snr_cdb=-32768)
        packet = await asyncio.wait_for(iface.rx_q.get(), timeout=1.0)

        self.assertEqual(packet, (b"no-signal", 0.0, 0.0))

    async def test_oversized_rx_frame_is_dropped_without_reconnect(self):
        daemon = await self.make_daemon()
        iface = await self.connect_interface(daemon, max_packet_size=4)

        await daemon.send_rx(b"too-large")
        await daemon.send_rx(b"ok")

        rf, _rssi, _snr = await asyncio.wait_for(iface.rx_q.get(), timeout=1.0)

        self.assertEqual(rf, b"ok")
        self.assertEqual(iface.rx_q.qsize(), 0)

    # --- TX path (managed, via TX_RESULT) -----------------------------------

    async def test_transmit_ok_records_airtime(self):
        daemon = await self.make_daemon(tx_result_status=TX_RESULT_STATUS_OK)
        iface = await self.connect_interface(daemon)

        airtime_ms = await asyncio.wait_for(iface.transmit(b"ok"), timeout=1.0)
        await daemon.wait_tx(b"ok")

        self.assertGreater(airtime_ms, 0)
        self.assertEqual(iface.airtime_txtime[-1], airtime_ms)
        self.assertGreater(iface.airtime_txtimestamp[-1], 0)

    async def test_transmit_busy_not_sent_and_no_dutycycle(self):
        daemon = await self.make_daemon(tx_result_status=TX_RESULT_STATUS_BUSY)
        iface = await self.connect_interface(daemon)

        before_time = list(iface.airtime_txtime)
        before_stamp = list(iface.airtime_txtimestamp)

        result = await asyncio.wait_for(iface.transmit(b"busy"), timeout=1.0)

        self.assertEqual(result, 0)
        self.assertEqual(list(iface.airtime_txtime), before_time)
        self.assertEqual(list(iface.airtime_txtimestamp), before_stamp)

    async def test_transmit_cad_timeout_not_sent(self):
        daemon = await self.make_daemon(
            tx_result_status=TX_RESULT_STATUS_CAD_TIMEOUT,
            tx_result_flags=0x05,
        )
        iface = await self.connect_interface(daemon)

        before_time = list(iface.airtime_txtime)

        result = await asyncio.wait_for(iface.transmit(b"cad"), timeout=1.0)

        self.assertEqual(result, 0)
        self.assertEqual(list(iface.airtime_txtime), before_time)

    async def test_transmit_radio_error_returns_zero(self):
        daemon = await self.make_daemon(tx_result_status=TX_RESULT_STATUS_RADIO_ERROR)
        iface = await self.connect_interface(daemon)

        before_time = list(iface.airtime_txtime)

        result = await asyncio.wait_for(iface.transmit(b"err"), timeout=1.0)

        self.assertEqual(result, 0)
        self.assertEqual(list(iface.airtime_txtime), before_time)

    async def test_transmit_error_frame_aborts_pending(self):
        daemon = await self.make_daemon(respond_to_tx=False)
        iface = await self.connect_interface(daemon)

        async def fail_with_error():
            await daemon.wait_tx(b"to-err", timeout=1.0)
            await daemon.send_error("recoverable TX failure")

        helper = asyncio.create_task(fail_with_error())
        self.tasks.append(helper)

        result = await asyncio.wait_for(iface.transmit(b"to-err"), timeout=1.0)
        self.assertEqual(result, 0)

    async def test_transmit_timeout_triggers_reconnect(self):
        daemon = await self.make_daemon(respond_to_tx=False, cadwait_ms=10)
        iface = await self.connect_interface(
            daemon,
            sf=7,
            bw=500000,
            tx_result_margin=0.05,
        )

        writer = iface._data_writer

        result = await asyncio.wait_for(iface.transmit(b"timeout"), timeout=1.0)

        self.assertEqual(result, 0)
        self.assertTrue(writer.is_closing())

    # --- Duty cycle ---------------------------------------------------------

    async def test_calculated_airtime_grows_with_payload_size(self):
        daemon = await self.make_daemon()
        iface = await self.connect_interface(daemon)

        small = iface._calculate_airtime_ms(16)
        large = iface._calculate_airtime_ms(128)

        self.assertGreater(small, 0)
        self.assertGreater(large, small)

    async def test_transmit_wait_returns_wait_when_duty_cycle_exceeded(self):
        daemon = await self.make_daemon()
        iface = await self.connect_interface(daemon, airtime=10)

        now = time.time()
        iface.airtime_txtimestamp.clear()
        iface.airtime_txtime.clear()

        for _ in range(5):
            iface.airtime_txtimestamp.append(now - 1)
            iface.airtime_txtime.append(500)

        self.assertGreater(iface.transmit_wait(), 0)

    # --- Reconnect ----------------------------------------------------------

    async def test_connection_loop_reconnects_after_daemon_restart(self):
        daemon = await self.make_daemon()
        iface = self.make_interface(
            daemon,
            connect_timeout=0.2,
            reconnect_delay=0.05,
            enable_tx=False,
        )

        iface._running = True
        manager = asyncio.create_task(iface._connection_loop())
        self.tasks.append(manager)

        await daemon.wait_data_connection(timeout=1.0)

        await daemon.send_rx(b"before")
        rf, _rssi, _snr = await asyncio.wait_for(iface.rx_q.get(), timeout=1.0)
        self.assertEqual(rf, b"before")

        await daemon.close()
        await asyncio.sleep(0.1)

        daemon2 = await self.make_daemon()
        await daemon2.wait_data_connection(timeout=2.0)

        await daemon2.send_rx(b"after")
        rf2, _rssi2, _snr2 = await asyncio.wait_for(iface.rx_q.get(), timeout=2.0)
        self.assertEqual(rf2, b"after")

        iface._running = False
        manager.cancel()


if __name__ == "__main__":
    unittest.main()
