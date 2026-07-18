import asyncio
import logging
import math
import os
import stat
import time
import re

from collections import deque

from aiotools import current_taskgroup

from configuration import ConfigView, get_config

from .interface import Interface

logger = logging.getLogger(__name__)


def _loraham_sockpath(configured):
    """systemd deployments serve the daemon sockets under /run/loraham; direct/user starts under /tmp
    (LORAHAM_SOCKET_DIR). Prefer the /run/loraham path when the socket already exists there, else the
    configured (/tmp) path — mirrors the daemon's clients (lorachat/igate)."""
    if not configured:
        return configured
    run = os.path.join("/run/loraham", os.path.basename(configured))
    try:
        if stat.S_ISSOCK(os.stat(run).st_mode):
            return run
    except OSError:
        pass
    return configured

VALID_BANDWIDTHS_HZ = {
    7800,
    10400,
    15600,
    20800,
    31250,
    41700,
    62500,
    125000,
    250000,
    500000,
}

FRAMED_DATA_HEADER_LEN = 3
FRAMED_DATA_TYPE_RX_PACKET = 0x01
FRAMED_DATA_TYPE_TX_PACKET = 0x02
FRAMED_DATA_TYPE_ERROR = 0x03
FRAMED_DATA_TYPE_TX_RESULT = 0x04
FRAMED_DATA_TYPES = {
    FRAMED_DATA_TYPE_RX_PACKET,
    FRAMED_DATA_TYPE_TX_PACKET,
    FRAMED_DATA_TYPE_ERROR,
    FRAMED_DATA_TYPE_TX_RESULT,
}

# RX_PACKET payload: [rssi_cdbm i16 LE][snr_cdb i16 LE][RF payload].
FRAMED_DATA_RX_META_LEN = 4
# TX_RESULT payload: [status u8][flags u8][seq u16 LE].
FRAMED_DATA_TX_RESULT_PAYLOAD_LEN = 4
# Sentinel for unavailable RSSI/SNR, in centi-units.
FRAMED_DATA_SIGNAL_UNAVAILABLE = -32768

# TX_RESULT wire status codes (loraham_daemon framed_data.h, v111). These are
# the on-the-wire values in the TX_RESULT payload, NOT the daemon's internal
# TxResult enum. There is no CAD_TIMEOUT status: a not-sent MANAGED TX reports
# CHANNEL_BUSY (2); a send-after-timeout reports OK (0) with the CAD_TIMEOUT flag.
TX_RESULT_STATUS_OK = 0
TX_RESULT_STATUS_BUSY = 1
TX_RESULT_STATUS_CHANNEL_BUSY = 2
TX_RESULT_STATUS_RADIO_NOT_READY = 3
TX_RESULT_STATUS_RADIO_ERROR = 4
TX_RESULT_STATUS_INVALID_PACKET = 5
TX_RESULT_STATUS_INVALID_BAND = 6

# TX_RESULT flag bits (informational; status is authoritative).
TX_RESULT_FLAG_MANAGED = 0x01
TX_RESULT_FLAG_DEFERRED = 0x02
TX_RESULT_FLAG_CAD_TIMEOUT = 0x04

# Daemon CADWAIT default (seconds) if the status reply does not report it.
DEFAULT_CADWAIT_S = 1.5

LORAHAM_PRESETS = {
    "eu_uk_long": {
        "frequency": 869525000,
        "bw": 250000,
        "sf": 11,
        "cr": 5,
        "preamble": 16,
        "ldro": False,
        "txpower": 14,
        "txmaxpower": 14,
    },
    "eu_uk_medium": {
        "frequency": 869525000,
        "bw": 250000,
        "sf": 10,
        "cr": 5,
        "preamble": 16,
        "ldro": False,
        "txpower": 14,
        "txmaxpower": 14,
    },
    "eu_uk_narrow": {
        "frequency": 869618000,
        "bw": 62500,
        "sf": 8,
        "cr": 8,
        "preamble": 16,
        "txpower": 14,
        "txmaxpower": 14,
    },
}



class LoRaHAMInterface(Interface):
    """
    LoRaHAM daemon socket interface.

    This interface uses persistent Unix stream connections to the LoRaHAM
    daemon framed data and configuration sockets. RX_PACKET frames are decoded
    into ``(payload, rssi, snr)`` tuples and forwarded into MeshCore.

    Channel access (LBT/CAD) is delegated to the daemon's MANAGED TX mode. Each
    transmitted TX_PACKET is answered by exactly one TX_RESULT frame, which this
    interface waits for instead of doing client-side CAD/busy polling.

    Note: ``TXMODE`` and ``CADWAIT`` are global per-band daemon state. On a
    radio band dedicated to this MeshCore node that is unproblematic.
    """

    def __init__(self, config: ConfigView):
        super().__init__()
        self._name = "LoRaHAM daemon interface"

        preset = config.get("preset", "eu_uk_long")
        if not isinstance(preset, str):
            raise ValueError("preset must be a string")
        if preset not in LORAHAM_PRESETS:
            raise ValueError(
                "preset must be one of: "
                + ", ".join(sorted(LORAHAM_PRESETS))
            )

        defaults = {
            "preset": preset,
            "data_socket": "/tmp/lora868f.sock",
            "config_socket": "/tmp/loraconf868.sock",
            "crc": True,
            "preamble": 8,
            "syncword": "0x12",
            "ldro": False,
            "enable_tx": False,
            "apply_config": True,
            "connect_timeout": 5.0,
            "reconnect_delay": 5.0,
            "tx_result_margin": 1.0,
            "max_packet_size": 255,
            "airtime": 10,
        }
        defaults.update(LORAHAM_PRESETS[preset])
        config.set_default(get_config(defaults))

        self.preset = config.get("preset")
        self.data_socket = _loraham_sockpath(config.get("data_socket"))
        self.config_socket = _loraham_sockpath(config.get("config_socket"))

        self.freq = config.get("frequency")
        self.sf = config.get("sf")
        self.bw = config.get("bw")
        self.cr = config.get("cr")
        self.crc = config.get("crc")
        self.preamble = config.get("preamble")
        self.syncword = self._parse_syncword(config.get("syncword"))
        self.ldro = config.get("ldro")

        self.txpower = config.get("txpower")
        self.txmaxpower = config.get("txmaxpower", self.txpower)
        self.enable_tx = config.get("enable_tx")

        self.apply_config = config.get("apply_config", True)
        self.connect_timeout = config.get("connect_timeout", 5.0)
        self.reconnect_delay = config.get("reconnect_delay", 5.0)
        self.tx_result_margin = config.get("tx_result_margin", 1.0)
        self.max_packet_size = config.get("max_packet_size", 255)
        self.airtime_dutycycle = config.get("airtime", 10)

        self.airtime_txtimestamp = deque([0, 0, 0, 0, 0], maxlen=5)
        self.airtime_txtime = deque([0, 0, 0, 0, 0], maxlen=5)

        self._data_reader = None
        self._data_writer = None
        self._config_reader = None
        self._config_writer = None
        self._running = False
        self._data_write_lock = asyncio.Lock()
        # Serialises the whole TX transaction (arm pending -> write -> await ->
        # consume) so at most one _pending_tx_result exists at any time, even if
        # two transmit() calls overlap.
        self._tx_lock = asyncio.Lock()
        # Daemon-reported CAD wait timeout, used to size the TX_RESULT timeout.
        self._cadwait_s = DEFAULT_CADWAIT_S
        # TX is allowed only after the connect handshake verified all of: a valid
        # CADWAIT (to size the result timeout), TXMODE=MANAGED and TXRESULT=1
        # (both global per-band, so another client could have changed them).
        # Until then TX is inhibited; reset on disconnect/timeout/cancel.
        self._tx_ready = False
        # Exactly one TX is in flight at a time (the dispatcher serialises TX),
        # so a single pending-result slot is enough; no seq map is needed.
        self._pending_tx_result = None

        self._validate_config()

        logger.debug(
            "Configured LoRaHAM daemon interface: data_socket=%s, "
            "config_socket=%s, preset=%s, freq=%s Hz, bw=%s Hz, sf=%s, cr=%s, "
            "txpower=%s dBm, tx_enabled=%s",
            self.data_socket,
            self.config_socket,
            self.preset,
            self.freq,
            self.bw,
            self.sf,
            self.cr,
            self.txpower,
            self.enable_tx,
        )

    def _parse_syncword(self, value):
        if isinstance(value, bool):
            raise ValueError("syncword must be an integer or integer string")

        if isinstance(value, int):
            return value

        if isinstance(value, str):
            return int(value, 0)

        raise ValueError("syncword must be an integer or integer string")

    def _validate_config(self):
        for name in ("data_socket", "config_socket"):
            value = getattr(self, name)
            if not isinstance(value, str) or value == "":
                raise ValueError(f"{name} must be a non-empty string")

        for name in (
            "freq",
            "sf",
            "bw",
            "cr",
            "preamble",
            "syncword",
            "txpower",
            "txmaxpower",
            "max_packet_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")

        if self.freq <= 0:
            raise ValueError("frequency must be positive")
        if self.bw not in VALID_BANDWIDTHS_HZ:
            raise ValueError("bw must be a valid SX127x LoRa bandwidth in Hz")
        if not 6 <= self.preamble <= 65535:
            raise ValueError("preamble must be between 6 and 65535")
        if not 7 <= self.sf <= 12:
            raise ValueError("sf must be between 7 and 12")
        if not 5 <= self.cr <= 8:
            raise ValueError("cr must be between 5 and 8")
        if not 0 <= self.syncword <= 0xff:
            raise ValueError("syncword must fit in one byte")
        if not 0 <= self.txpower <= 20:
            raise ValueError("txpower must be between 0 and 20 dBm")
        if not self.txpower <= self.txmaxpower <= 20:
            raise ValueError(
                "txmaxpower must be between txpower and 20 dBm; "
                "set txmaxpower >= txpower when overriding txpower"
            )
        if not 1 <= self.max_packet_size <= 255:
            raise ValueError("max_packet_size must be between 1 and 255")

        for name in ("crc", "ldro", "enable_tx", "apply_config"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")

        for name in ("connect_timeout", "reconnect_delay"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        if (
            isinstance(self.tx_result_margin, bool)
            or not isinstance(self.tx_result_margin, (int, float))
        ):
            raise ValueError("tx_result_margin must be numeric")
        if self.tx_result_margin < 0:
            raise ValueError("tx_result_margin must be zero or positive")

        if (
            isinstance(self.airtime_dutycycle, bool)
            or not isinstance(self.airtime_dutycycle, (int, float))
        ):
            raise ValueError("airtime must be numeric")
        if not 0 < self.airtime_dutycycle <= 100:
            raise ValueError("airtime must be between 0 and 100 percent")

    @staticmethod
    def _format_decimal(value):
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _format_config_command(self):
        freq_mhz = self.freq / 1000000
        bw_khz = self.bw / 1000

        return (
            "SET MODE=LORA "
            f"FREQ={self._format_decimal(freq_mhz)} "
            f"SF={self.sf} "
            f"BW={self._format_decimal(bw_khz)} "
            f"CR={self.cr} "
            f"CRC={1 if self.crc else 0} "
            f"PREAMBLE={self.preamble} "
            f"SYNC=0x{self.syncword:02X} "
            f"LDRO={1 if self.ldro else 0} "
            f"POWER={self.txpower}\n"
        )

    async def _open_unix_connection(self, path, label):
        logger.info("Connecting LoRaHAM %s socket: %s", label, path)
        try:
            return await asyncio.wait_for(
                asyncio.open_unix_connection(path),
                timeout=self.connect_timeout,
            )
        except FileNotFoundError as exc:
            raise ConnectionError(f"LoRaHAM {label} socket not found: {path}") from exc
        except TimeoutError as exc:
            raise ConnectionError(f"Timed out connecting LoRaHAM {label} socket: {path}") from exc
        except OSError as exc:
            raise ConnectionError(f"Unable to connect LoRaHAM {label} socket {path}: {exc}") from exc

    async def _connect_sockets(self):
        # Drop any TX result left pending from a previous connection.
        self._fail_pending_tx()
        self._cadwait_s = DEFAULT_CADWAIT_S
        self._tx_ready = False

        self._data_reader, self._data_writer = await self._open_unix_connection(
            self.data_socket,
            "data",
        )
        self._config_reader, self._config_writer = await self._open_unix_connection(
            self.config_socket,
            "config",
        )

        if self.apply_config:
            await self._send_config()

        if self.enable_tx:
            # Delegate LBT to the daemon and ask for a per-TX result frame.
            await self._write_config_command("SET TXMODE=MANAGED\n")
            await self._write_config_command("SET TXRESULT=1\n")

        await self._handshake_status()

        logger.info("LoRaHAM daemon sockets connected")

    async def _write_config_command(self, command):
        writer = self._config_writer
        if writer is None:
            raise ConnectionError("LoRaHAM config socket is not connected")

        writer.write(command.encode("ascii"))
        await writer.drain()

    async def _send_config(self):
        command = self._format_config_command()
        logger.info("Applying LoRaHAM radio config: %s", command.strip())
        await self._write_config_command(command)

    @staticmethod
    def _parse_key_value_fields(line):
        return {
            key.upper(): value.upper()
            for key, value in re.findall(r"\b([A-Z]+)=([^\s]+)", line.upper())
        }

    async def _handshake_status(self):
        """
        One-shot connect handshake: request `GET STATUS` and read the reply to
        cache the daemon CADWAIT value. This is not ongoing polling; the ongoing
        config reader only drains/logs after this returns.
        """
        try:
            await self._write_config_command("GET STATUS\n")
        except Exception as exc:
            logger.warning("Unable to request LoRaHAM status: %s", exc)
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        buffer = bytearray()

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "No LoRaHAM CADWAIT in status; using default %.2f s",
                    self._cadwait_s,
                )
                return

            try:
                data = await asyncio.wait_for(
                    self._config_reader.read(256),
                    timeout=remaining,
                )
            except TimeoutError:
                logger.warning(
                    "No LoRaHAM status reply; using default CADWAIT %.2f s",
                    self._cadwait_s,
                )
                return

            if not data:
                raise ConnectionError(
                    "LoRaHAM config socket closed during status handshake"
                )

            buffer.extend(data)
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                decoded_line = line.decode(errors="replace").rstrip("\r")
                logger.debug("LoRaHAM config socket: %s", decoded_line)

                fields = self._parse_key_value_fields(decoded_line)
                if "RADIO" in fields:
                    logger.debug("LoRaHAM radio status: %s", fields["RADIO"])
                if "CADWAIT" in fields:
                    cadwait_ok = False
                    try:
                        self._cadwait_s = max(
                            int(fields["CADWAIT"]) / 1000.0, 0.0
                        )
                        cadwait_ok = True
                        logger.info(
                            "LoRaHAM CADWAIT=%s ms", fields["CADWAIT"]
                        )
                    except ValueError:
                        logger.warning(
                            "Bad LoRaHAM CADWAIT value: %s", fields["CADWAIT"]
                        )

                    # TX is only ready when the daemon really is in MANAGED mode
                    # with per-TX results enabled (both are global per-band state
                    # another client could have changed). Verify on every
                    # (re)connect handshake; RX-only clients never gate on this.
                    if self.enable_tx:
                        radio = fields.get("RADIO")
                        txmode = fields.get("TXMODE")
                        txresult = fields.get("TXRESULT")
                        if (cadwait_ok and radio == "READY"
                                and txmode == "MANAGED" and txresult == "1"):
                            self._tx_ready = True
                        else:
                            logger.warning(
                                "LoRaHAM TX not ready: RADIO=%s CADWAIT_ok=%s "
                                "TXMODE=%s TXRESULT=%s; TX inhibited",
                                radio, cadwait_ok, txmode, txresult,
                            )
                    return

    def _calculate_airtime_ms(self, payload_len):
        """
        Return LoRa packet airtime in milliseconds.
        """
        symbol_time = (2 ** self.sf) / self.bw
        low_data_rate = 1 if self.ldro else 0
        implicit_header = 0  # IH=0 means explicit header mode.
        crc_enabled = 1 if self.crc else 0

        payload_symbols = 8 + max(
            math.ceil(
                (
                    (8 * payload_len)
                    - (4 * self.sf)
                    + 28
                    + (16 * crc_enabled)
                    - (20 * implicit_header)
                )
                / (4 * (self.sf - (2 * low_data_rate)))
            )
            * self.cr,
            0,
        )

        airtime = (self.preamble + 4.25 + payload_symbols) * symbol_time
        return airtime * 1000

    async def _config_reader_loop(self):
        buffer = bytearray()

        while True:
            data = await self._config_reader.read(256)
            if not data:
                raise ConnectionError("LoRaHAM config socket closed")

            buffer.extend(data)

            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                decoded_line = line.decode(errors="replace").rstrip("\r")
                logger.debug("LoRaHAM config socket: %s", decoded_line)

            if len(buffer) > 4096:
                logger.warning("Discarding oversized LoRaHAM config socket buffer")
                buffer.clear()

    async def _read_exact(self, reader, size, label):
        try:
            return await reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError(
                f"LoRaHAM {label} socket closed while reading {size} bytes"
            ) from exc

    def _decode_frame_header(self, header):
        if len(header) != FRAMED_DATA_HEADER_LEN:
            raise ValueError("invalid LoRaHAM frame header length")

        frame_type = header[0]
        payload_len = header[1] | (header[2] << 8)

        if frame_type not in FRAMED_DATA_TYPES:
            raise ValueError(f"unknown LoRaHAM frame type 0x{frame_type:02X}")

        return frame_type, payload_len

    async def _read_frame(self, reader, label):
        header = await self._read_exact(reader, FRAMED_DATA_HEADER_LEN, label)
        frame_type, payload_len = self._decode_frame_header(header)

        if payload_len == 0:
            return frame_type, b""

        # Oversize guard. RX_PACKET carries 4 metadata bytes before the RF
        # payload, so compare the RF length (not the whole payload) against the
        # configured limit.
        if frame_type == FRAMED_DATA_TYPE_RX_PACKET:
            if payload_len > FRAMED_DATA_RX_META_LEN + self.max_packet_size:
                await self._read_exact(reader, payload_len, label)
                logger.warning(
                    "Dropping oversized LoRaHAM RX frame: %s bytes", payload_len
                )
                return None, b""
        elif frame_type == FRAMED_DATA_TYPE_TX_PACKET:
            if payload_len > self.max_packet_size:
                await self._read_exact(reader, payload_len, label)
                logger.warning(
                    "Dropping oversized LoRaHAM frame: %s bytes", payload_len
                )
                return None, b""

        payload = await self._read_exact(reader, payload_len, label)
        return frame_type, payload

    def _handle_rx_packet(self, payload):
        if len(payload) < FRAMED_DATA_RX_META_LEN:
            logger.warning(
                "Ignoring short LoRaHAM RX frame: %s bytes", len(payload)
            )
            return None

        rssi_cdbm = int.from_bytes(payload[0:2], "little", signed=True)
        snr_cdb = int.from_bytes(payload[2:4], "little", signed=True)
        rf = payload[FRAMED_DATA_RX_META_LEN:]

        if not rf:
            logger.warning("Ignoring empty LoRaHAM RX packet frame")
            return None

        rssi = 0.0 if rssi_cdbm == FRAMED_DATA_SIGNAL_UNAVAILABLE else rssi_cdbm / 100.0
        snr = 0.0 if snr_cdb == FRAMED_DATA_SIGNAL_UNAVAILABLE else snr_cdb / 100.0

        return bytes(rf), rssi, snr

    async def _data_reader_loop(self):
        while True:
            frame_type, payload = await self._read_frame(self._data_reader, "data")

            if frame_type is None:
                continue

            if frame_type == FRAMED_DATA_TYPE_RX_PACKET:
                decoded = self._handle_rx_packet(payload)
                if decoded is None:
                    continue

                rf, rssi, snr = decoded
                await self.rx_q.put((rf, rssi, snr))
                logger.debug(
                    "Queued LoRaHAM RX packet, %s bytes, rssi=%.2f snr=%.2f, rx_q=%s",
                    len(rf),
                    rssi,
                    snr,
                    self.rx_q.qsize(),
                )
                continue

            if frame_type == FRAMED_DATA_TYPE_TX_RESULT:
                self._deliver_tx_result(payload)
                continue

            if frame_type == FRAMED_DATA_TYPE_ERROR:
                logger.error(
                    "LoRaHAM framed data error: %s",
                    payload.decode("utf-8", errors="replace"),
                )
                # A daemon ERROR can arrive instead of a TX_RESULT; do not leave
                # an in-flight transmit waiting.
                self._fail_pending_tx()
                continue

            # TX_PACKET (0x02) or anything else from the daemon is unexpected;
            # log and keep the connection rather than tearing it down.
            logger.warning(
                "Ignoring unexpected LoRaHAM data frame type 0x%02X", frame_type
            )

    def _deliver_tx_result(self, payload):
        # TX_RESULT payload is exactly 4 bytes; any other length is malformed.
        # Ignore it (do not resolve the pending future) and let the timeout act.
        if len(payload) != FRAMED_DATA_TX_RESULT_PAYLOAD_LEN:
            logger.warning(
                "Ignoring malformed LoRaHAM TX_RESULT frame: %s bytes", len(payload)
            )
            return

        status = payload[0]
        flags = payload[1]
        seq = payload[2] | (payload[3] << 8)

        future = self._pending_tx_result
        if future is None or future.done():
            logger.warning(
                "Discarding unexpected LoRaHAM TX_RESULT (status=%s flags=0x%02X seq=%s)",
                status,
                flags,
                seq,
            )
            return

        self._pending_tx_result = None
        future.set_result((status, flags, seq))

    def _fail_pending_tx(self):
        future = self._pending_tx_result
        self._pending_tx_result = None
        if future is not None and not future.done():
            future.set_result(None)

    def _request_reconnect(self):
        # Closing the data writer drops the transport, which makes the reader
        # loops error out so _connection_loop reconnects.
        writer = self._data_writer
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:
                logger.debug("Error while forcing LoRaHAM reconnect: %s", exc)

    async def _connection_loop(self):
        while self._running:
            try:
                await self._connect_sockets()

                tasks = [
                    asyncio.create_task(
                        self._data_reader_loop(),
                        name="LoRaHAM data socket reader",
                    ),
                    asyncio.create_task(
                        self._config_reader_loop(),
                        name="LoRaHAM config socket reader",
                    ),
                ]

                done = set()
                try:
                    done, _ = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

                for task in done:
                    task.result()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("LoRaHAM socket connection error: %s", exc)
            finally:
                await self._close_sockets()

            if self._running:
                logger.info(
                    "Reconnecting LoRaHAM daemon sockets in %.1f seconds",
                    self.reconnect_delay,
                )
                await asyncio.sleep(self.reconnect_delay)

    async def _close_sockets(self):
        # Resolve any in-flight transmit so it does not hang across reconnect.
        self._fail_pending_tx()
        # CADWAIT must be re-learned from the next connect handshake before TX.
        self._tx_ready = False

        writers = (self._data_writer, self._config_writer)

        self._data_reader = None
        self._data_writer = None
        self._config_reader = None
        self._config_writer = None

        for writer in writers:
            if writer is None:
                continue

            writer.close()
            try:
                await writer.wait_closed()
            except Exception as exc:
                logger.debug("Error while closing LoRaHAM socket: %s", exc)

    def _encode_frame(self, frame_type, payload):
        if frame_type not in FRAMED_DATA_TYPES:
            raise ValueError(f"unknown LoRaHAM frame type 0x{frame_type:02X}")

        payload = bytes(payload)
        payload_len = len(payload)

        if frame_type in (FRAMED_DATA_TYPE_RX_PACKET, FRAMED_DATA_TYPE_TX_PACKET):
            if payload_len == 0:
                raise ValueError("LoRaHAM RF payload must not be empty")
            if payload_len > self.max_packet_size:
                raise ValueError(
                    f"LoRaHAM RF payload too large: {payload_len} bytes"
                )

        header = bytes([
            frame_type,
            payload_len & 0xff,
            (payload_len >> 8) & 0xff,
        ])
        return header + payload

    async def _write_frame(self, frame_type, payload):
        writer = self._data_writer
        if writer is None:
            raise ConnectionError("LoRaHAM data socket is not connected")

        frame = self._encode_frame(frame_type, payload)

        async with self._data_write_lock:
            writer.write(frame)
            await writer.drain()

    async def transmit(self, tx_packet):
        """
        Transmit one MeshCore packet as one LoRaHAM TX_PACKET frame and wait for
        the daemon's TX_RESULT. Returns the LoRa airtime in milliseconds on a
        successful send, or 0 if the packet was not sent.
        """
        if not self.enable_tx:
            logger.debug("LoRaHAM daemon TX disabled; packet discarded")
            return 0

        # Serialise the whole transaction so at most one TX_RESULT is pending.
        async with self._tx_lock:
            # Reject a missing or already-closing transport so a result of a
            # timed-out/cancelled TX cannot be inherited by a new future before
            # the reconnect handshake re-establishes a fresh stream.
            if self._data_writer is None or self._data_writer.is_closing():
                logger.warning("LoRaHAM data socket not connected; packet not sent")
                return 0

            # TX is allowed only after the handshake verified CADWAIT + MANAGED +
            # TXRESULT=1. Otherwise inhibit (RX keeps working).
            if not self._tx_ready:
                logger.warning(
                    "LoRaHAM TX not ready (CADWAIT/TXMODE/TXRESULT); packet not sent"
                )
                return 0

            airtime_ms = self._calculate_airtime_ms(len(tx_packet))
            timeout = self._cadwait_s + (airtime_ms / 1000.0) + self.tx_result_margin

            loop = asyncio.get_running_loop()
            future = loop.create_future()
            # Arm the pending slot before writing so a fast TX_RESULT is not missed.
            self._pending_tx_result = future

            try:
                await self._write_frame(FRAMED_DATA_TYPE_TX_PACKET, tx_packet)
                result = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.CancelledError:
                # Clear our slot before unwinding so a late TX_RESULT cannot be
                # mis-associated with the next transmit(); force a fresh handshake.
                if self._pending_tx_result is future:
                    self._pending_tx_result = None
                self._tx_ready = False
                self._request_reconnect()
                raise
            except TimeoutError:
                if self._pending_tx_result is future:
                    self._pending_tx_result = None
                self._tx_ready = False
                logger.warning(
                    "No LoRaHAM TX_RESULT after %.2f s; packet result lost, reconnecting",
                    timeout,
                )
                self._request_reconnect()
                return 0
            except (ConnectionError, OSError) as exc:
                # Stream error (e.g. drain failure): the frame may already have
                # reached the daemon, so a late TX_RESULT could be inherited by
                # the next transmit(). Invalidate the connection and reconnect.
                if self._pending_tx_result is future:
                    self._pending_tx_result = None
                self._tx_ready = False
                logger.error("LoRaHAM daemon TX stream error: %s", exc)
                self._request_reconnect()
                return 0
            except Exception as exc:
                # Local/encode error (e.g. ValueError): nothing went out, the
                # connection stays usable.
                if self._pending_tx_result is future:
                    self._pending_tx_result = None
                logger.error("LoRaHAM daemon TX failed: %s", exc)
                return 0

            if result is None:
                # Resolved by an ERROR frame or a connection drop.
                logger.warning("LoRaHAM TX aborted before a result was received")
                return 0

            status, flags, seq = result
            logger.debug(
                "LoRaHAM TX_RESULT status=%s flags=0x%02X seq=%s", status, flags, seq
            )

            if status == TX_RESULT_STATUS_OK:
                # OK also covers send-after-CAD-timeout (flag 0x04): transmitted.
                self.airtime_txtimestamp.append(time.time())
                self.airtime_txtime.append(airtime_ms)
                logger.debug(
                    "Sent LoRaHAM TX packet, %s bytes, airtime %.2f ms",
                    len(tx_packet),
                    airtime_ms,
                )
                return airtime_ms

            if status in (TX_RESULT_STATUS_BUSY, TX_RESULT_STATUS_CHANNEL_BUSY):
                # Channel busy; the daemon did not transmit. MeshCore decides on
                # retransmission, so do not count airtime.
                logger.info(
                    "LoRaHAM channel busy (status=%s); packet not sent", status
                )
                return 0

            if status in (TX_RESULT_STATUS_RADIO_NOT_READY,
                          TX_RESULT_STATUS_RADIO_ERROR):
                logger.error(
                    "LoRaHAM radio error (status=%s seq=%s); packet not sent",
                    status, seq,
                )
                return 0

            if status in (TX_RESULT_STATUS_INVALID_PACKET,
                          TX_RESULT_STATUS_INVALID_BAND):
                logger.error(
                    "LoRaHAM rejected packet (status=%s seq=%s)", status, seq
                )
                return 0

            logger.error(
                "LoRaHAM unknown TX_RESULT status=%s seq=%s", status, seq
            )
            return 0

    def transmit_wait(self):
        """
        Return the suggested duty-cycle wait time in seconds.
        """
        tx_earliest = self.airtime_txtimestamp[0]
        tx_period = time.time() - tx_earliest
        tx_total = sum(self.airtime_txtime)

        if tx_earliest <= 0 or tx_period <= 0 or tx_total <= 0:
            return 0

        duty_cycle = 100 * (tx_total / 1000) / tx_period
        logger.debug(
            "LoRaHAM duty cycle for last %s transmissions: %.2f%%",
            len(self.airtime_txtimestamp),
            duty_cycle,
        )

        for c in range(3):
            fraction = 1 / (1 << c)
            airtime_dutycycle = self.airtime_dutycycle * fraction

            if duty_cycle > airtime_dutycycle:
                tx_min = (
                    tx_earliest
                    + (tx_total / 1000) / (airtime_dutycycle / 100)
                    - time.time()
                ) * fraction

                if tx_min > 0:
                    logger.debug(
                        "LoRaHAM duty-cycle wait %.2f seconds at %.2f%% limit",
                        tx_min,
                        airtime_dutycycle,
                    )
                    return tx_min

        return 0

    def get_radioconfig(self):
        """
        Return frequency (kHz), bandwidth (Hz), spreading factor, coding rate,
        TX power (dBm), and maximum TX power (dBm).
        """
        return (self.freq // 1000, self.bw, self.sf, self.cr, self.txpower, self.txmaxpower)

    async def start(self):
        """
        Start persistent LoRaHAM daemon socket management.
        """
        if self._running:
            return None

        self._running = True
        current_taskgroup.get().create_task(
            self._connection_loop(),
            name="LoRaHAM daemon socket manager",
        )
        return None
