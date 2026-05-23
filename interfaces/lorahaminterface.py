import asyncio
import logging

from aiotools import current_taskgroup

from configuration import ConfigView, get_config

from .interface import Interface

logger = logging.getLogger(__name__)

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


class LoRaHAMInterface(Interface):
    """
    LoRaHAM daemon socket interface.

    This interface uses persistent Unix stream connections to the LoRaHAM
    daemon data and configuration sockets. This milestone establishes the
    connection lifecycle and configuration path only. Data socket RX is drained
    and logged, but not yet forwarded into MeshCore until packet framing is
    handled deliberately.
    """

    def __init__(self, config: ConfigView):
        super().__init__()
        self._name = "LoRaHAM daemon interface"

        config.set_default(get_config({
            "data_socket": "/tmp/lora868.sock",
            "config_socket": "/tmp/loraconf868.sock",
            "frequency": 869618000,
            "sf": 8,
            "bw": 62500,
            "cr": 8,
            "crc": True,
            "preamble": 8,
            "syncword": "0x12",
            "ldro": False,
            "txpower": 14,
            "txmaxpower": 14,
            "enable_tx": False,
            "apply_config": True,
            "connect_timeout": 5.0,
            "reconnect_delay": 5.0,
            "max_packet_size": 255,
        }))

        self.data_socket = config.get("data_socket")
        self.config_socket = config.get("config_socket")

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
        self.enable_tx = config.get("enable_tx", False)

        self.apply_config = config.get("apply_config", True)
        self.connect_timeout = config.get("connect_timeout", 5.0)
        self.reconnect_delay = config.get("reconnect_delay", 5.0)
        self.max_packet_size = config.get("max_packet_size", 255)

        self._data_reader = None
        self._data_writer = None
        self._config_reader = None
        self._config_writer = None
        self._running = False
        self._discarded_rx_chunks = 0

        self._validate_config()

        logger.debug(
            "Configured LoRaHAM daemon interface: data_socket=%s, "
            "config_socket=%s, freq=%s Hz, bw=%s Hz, sf=%s, cr=%s, "
            "txpower=%s dBm, tx_enabled=%s",
            self.data_socket,
            self.config_socket,
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
            raise ValueError("txmaxpower must be between txpower and 20 dBm")
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

        logger.info("LoRaHAM daemon sockets connected")

    async def _send_config(self):
        if self._config_writer is None:
            raise ConnectionError("LoRaHAM config socket is not connected")

        command = self._format_config_command()
        logger.info("Applying LoRaHAM radio config: %s", command.strip())

        self._config_writer.write(command.encode("ascii"))
        await self._config_writer.drain()

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
                logger.debug(
                    "LoRaHAM config socket: %s",
                    line.decode(errors="replace").rstrip("\r"),
                )

            if len(buffer) > 4096:
                logger.warning("Discarding oversized LoRaHAM config socket buffer")
                buffer.clear()

    async def _data_reader_loop(self):
        warned = False

        while True:
            data = await self._data_reader.read(self.max_packet_size)
            if not data:
                raise ConnectionError("LoRaHAM data socket closed")

            self._discarded_rx_chunks += 1
            if not warned:
                logger.warning(
                    "LoRaHAM data RX is connected, but packet forwarding is not "
                    "enabled in this milestone; raw chunks are being discarded"
                )
                warned = True

            logger.debug(
                "Discarded LoRaHAM raw RX chunk %s, %s bytes",
                self._discarded_rx_chunks,
                len(data),
            )

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

                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    task.result()

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

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

    async def transmit(self, tx_packet):
        """
        Transmit packet.

        TX is intentionally not implemented in this milestone.
        """
        if self.enable_tx:
            logger.warning("LoRaHAM daemon TX requested, but TX is not implemented yet")
        else:
            logger.debug("LoRaHAM daemon TX disabled; packet discarded")

        return 0

    def transmit_wait(self):
        """
        Duty-cycle waiting is not implemented in this milestone.
        """
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
