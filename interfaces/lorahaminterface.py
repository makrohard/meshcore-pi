import logging

from configuration import ConfigView, get_config

from .interface import Interface

logger = logging.getLogger(__name__)


class LoRaHAMInterface(Interface):
    """
    LoRaHAM daemon socket interface.

    This initial skeleton only parses and validates configuration. It does
    not open daemon sockets, receive packets, or transmit RF data yet.
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

        for name in ("freq", "sf", "bw", "cr", "preamble", "syncword", "txpower", "txmaxpower"):
            value = getattr(self, name)
            if not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")

        if self.freq <= 0:
            raise ValueError("frequency must be positive")
        if self.bw <= 0:
            raise ValueError("bw must be positive")
        if self.preamble <= 0:
            raise ValueError("preamble must be positive")
        if not 5 <= self.sf <= 12:
            raise ValueError("sf must be between 5 and 12")
        if not 4 <= self.cr <= 8:
            raise ValueError("cr must be between 4 and 8")
        if not 0 <= self.syncword <= 0xff:
            raise ValueError("syncword must fit in one byte")

        if not isinstance(self.crc, bool):
            raise ValueError("crc must be boolean")
        if not isinstance(self.ldro, bool):
            raise ValueError("ldro must be boolean")
        if not isinstance(self.enable_tx, bool):
            raise ValueError("enable_tx must be boolean")

    async def transmit(self, tx_packet):
        """
        Transmit packet.

        TX is intentionally not implemented in this skeleton.
        """
        if self.enable_tx:
            logger.warning("LoRaHAM daemon TX requested, but TX is not implemented yet")
        else:
            logger.debug("LoRaHAM daemon TX disabled; packet discarded")

        return 0

    def transmit_wait(self):
        """
        Duty-cycle waiting is not implemented in the skeleton.
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
        Start the interface.

        The skeleton deliberately does not open LoRaHAM daemon sockets yet.
        """
        logger.info(
            "LoRaHAM daemon interface configured for %s MHz; socket I/O not implemented yet",
            self.freq / 1000000,
        )
        return None
