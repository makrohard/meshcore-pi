import asyncio

from binascii import hexlify
import ipaddress
import struct

import logging
logger = logging.getLogger(__name__)


_LOOPBACK_NET = ipaddress.ip_network('127.0.0.0/8')
_DEFAULT_ALLOW = ipaddress.ip_network('127.0.0.1/32')


def parse_allow_network(value):
    """
    Parse an allow-list entry (a bare IPv4 address or a CIDR) into an ip_network.

    A bare address becomes a /32. IPv4 only: an IPv6 value or any parse error
    fails closed to loopback-only (127.0.0.1/32) so a typo can never silently
    expose the port, and derive_listen_host stays address-family consistent.
    """
    try:
        net = ipaddress.ip_network(str(value), strict=False)
    except ValueError as e:
        logger.error(f"Invalid companion 'allow' value {value!r}, "
                     f"failing closed to 127.0.0.1: {e}")
        return _DEFAULT_ALLOW
    if net.version != 4:
        logger.error(f"Companion 'allow' value {value!r} is not IPv4 "
                     f"(IPv6 allow-lists are not supported) — failing closed to 127.0.0.1")
        return _DEFAULT_ALLOW
    return net


def derive_listen_host(net):
    """
    Derive the bind address from the allow-network: loopback when the allowed
    range sits within 127.0.0.0/8 (so the port stays unexposed), otherwise all
    interfaces (0.0.0.0) with the source filter applied on accept.
    """
    try:
        if net.version == 4 and net.subnet_of(_LOOPBACK_NET):
            return '127.0.0.1'
    except TypeError:
        pass
    return '0.0.0.0'


def peer_allowed(net, addr):
    """
    True if a peer address is inside the allow-network. Fails closed (False) on
    any parse or address-family mismatch.
    """
    try:
        return ipaddress.ip_address(addr) in net
    except (ValueError, TypeError):
        return False


class BaseCompanionInterface:
    """
    Base class for sending and receiving frames of data to and from Meshcore apps
    """
    def __init__(self):
        pass

    async def rx(self):
        pass

    async def tx(self, frame):
        pass

    async def start(self):
        pass


class CompanionInterface(BaseCompanionInterface):
    """
    Communicate with a Meshcore app over wifi.

    Input frames are requests which should be responded to
    Output frames are a mixture of responses and asynchronous notifications

    See https://github.com/ripplebiz/MeshCore/wiki/Companion-Radio-Protocol for the format
    
    WiFi uses the same wire format as the serial device
    """

    def __init__(self, config):
        super().__init__()

        self.port = config.get('port', 5000)

        # Source-IP allow-list. Only peers inside this network may connect; the
        # MeshCore companion protocol has no authentication, so this is the one
        # gate on remote access. Default 127.0.0.1 => loopback only.
        self._allow_net = parse_allow_network(config.get('allow', '127.0.0.1'))

        # `allow` derives the bind address; an explicit `listen` overrides it
        # (advanced). Deriving keeps the port unexposed unless the operator
        # widens `allow`.
        listen = config.get('listen', None)
        self.listen = listen if listen is not None else derive_listen_host(self._allow_net)

        self._reader = None
        self._writer = None

        self._connected = asyncio.Event()

    # Inbound queue
    async def rx(self):
        while True:
            while self._writer is None:
                logger.debug("Waiting for client to connect")
                # Wait for the connection to be established
                await self._connected.wait()

            try:
                while True:
                    logger.debug("Waiting for frame")
                    # Fetch one byte. Hopefully it's a '<'

                    if True:    # config.timeout
                        # The companion app requests battery status every minute, so we should
                        # not go for much longer than that without seeing something
                        r = await asyncio.wait_for(self._reader.readexactly(1), 90)
                    else:
                        r = await self._reader.readexactly(1)

                    if r != b'<':
                        # Not a start of frame
                        junkdata = r
                        while True:
                            try:
                                # Keep reading until we hit a < or data stops arriving (1 second pause)
                                r = await asyncio.wait_for(self._reader.readexactly(1), 1)
                                if r == b'<':
                                    if len(junkdata):
                                        logger.warning(f"Junk data before frame in companion serial data, {len(junkdata)} bytes: {hexlify(junkdata).decode()}")
                                    break
                                junkdata += r
                            except TimeoutError:
                                if len(junkdata):
                                    logger.warning(f"Junk data in companion serial data, {len(junkdata)} bytes: {hexlify(junkdata).decode()}")
                                    # FIXME this needs improving
                                break
                        

                    # Next two bytes are the frame size
                    try:
                        r = await asyncio.wait_for(self._reader.readexactly(2), 1)
                        size = struct.unpack("<H", r)[0]
                            
                        r = await asyncio.wait_for(self._reader.readexactly(size), 5)

                        logger.debug(f"Received frame, {len(r)} bytes")

                        return r

                    except TimeoutError:
                        logger.warning("Timed out waiting frame")
            except asyncio.exceptions.IncompleteReadError:
                # The connection was lost
                logger.info("Connection to Meshcore app lost")
                self._writer = None
                self._connected.clear()
            except TimeoutError:
                # Connection time out
                logger.info("Connection to Meshcore app timed out")
                self._writer.close()
                self._writer = None
                self._connected.clear()
            except Exception as e:
                logger.error(f"Connection lost due to: {repr(e)}")
                self._writer = None
                self._connected.clear()
    

    async def tx(self, frame):
        if self._writer is None:
            logger.debug(f"Unable to send frame, client is disconnected: {hexlify(frame).decode()} (len: {len(frame)})")
            return
        
        logger.debug(f"Sending frame: {hexlify(frame).decode()} (len: {len(frame)})")
        framelength = struct.pack("<H", len(frame))
        
        try:
            self._writer.write(b'>')
            self._writer.write(framelength)
            self._writer.write(frame)
            await self._writer.drain()
        except Exception as e:
            logger.debug(f"Exception sending data: {repr(e)}")
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
            self._connected.clear()
            return

        logger.debug("Data sent to app device.")

    async def connected(self, reader, writer):
        peer = writer.get_extra_info('peername')
        addr = peer[0] if peer else None

        # The allow-list is the ONLY access gate (no auth). If the peer address is
        # unavailable we cannot verify it, so fail closed.
        if addr is None or not peer_allowed(self._allow_net, addr):
            logger.info(f"[wifi] rejected connection from {peer} (not in allow-list)")
            writer.close()
            return

        logger.debug(f"Connection callback - client has connected from {addr}")

        if self._writer is not None:
            logger.info("Client already connected, disconnecting")
            writer.close()
            return

        self._reader = reader
        self._writer = writer
        self._connected.set()
    
    async def start(self):
        result = await asyncio.start_server(self.connected, host=self.listen, port=self.port, backlog=1)

        # If anything's wrong, it should have raised an exception
        if result.is_serving():
            for addr in result.sockets:
                logger.debug(f"Server listening on {addr.getsockname()}")

