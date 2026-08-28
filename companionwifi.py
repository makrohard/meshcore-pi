import asyncio

from binascii import hexlify
import ipaddress
import struct

import logging
logger = logging.getLogger(__name__)


_LOOPBACK_NET = ipaddress.ip_network('127.0.0.0/8')
_DEFAULT_ALLOW = ipaddress.ip_network('127.0.0.1/32')

# Upper bound on the resynchronisation buffer. A peer that never sends a start-of-frame
# byte must not be able to grow this without limit: the junk is only ever logged as a
# length plus a hex dump, so an unbounded peer could exhaust memory (and then the log).
# One MTU is far more than any real desynchronisation needs.
MAX_JUNK_BYTES = 256

# Short timeouts that protect an INCOMPLETE frame — a peer that sent a start-of-frame byte
# and then stalled must not pin the reader forever. These are framing-progress guards and
# are unrelated to idle disconnection.
_RESYNC_BYTE_TIMEOUT = 1.0
_FRAME_HEADER_TIMEOUT = 1.0
_FRAME_BODY_TIMEOUT = 5.0


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

        # OPTIONAL idle disconnect, OFF by default.
        #
        # This used to be a hard-coded ~90 s read timeout on the assumption that a companion
        # app polls battery status every minute. A client that legitimately sits waiting for
        # adverts or messages sends nothing, so a healthy connection was dropped roughly every
        # 90 seconds — observed as a long-running CLI session dying while idle. A node must not
        # disconnect an otherwise healthy peer just because the peer has nothing to say.
        #
        # Unset or 0 (the default) => never disconnect on idle. A positive value re-enables a
        # disconnect after that many seconds without a complete frame.
        self.idle_timeout = self._parse_idle_timeout(config.get('idle_timeout', 0))

        self._reader = None
        self._writer = None

        self._connected = asyncio.Event()

    @staticmethod
    def _parse_idle_timeout(value):
        """Seconds of inactivity before disconnecting, or None for never.

        Fails SAFE: an unparseable or negative value disables the timeout rather than
        inventing one, because a spurious disconnect is the failure this option exists to
        avoid.
        """
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            logger.error(f"Invalid companion 'idle_timeout' value {value!r} — "
                         f"disabling the idle disconnect")
            return None
        if seconds <= 0:
            return None
        return seconds

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
                    # Fetch one byte. Hopefully it's a '<'.
                    #
                    # NO timeout by default: an idle client is a healthy client. Only an
                    # explicitly configured `idle_timeout` re-arms a disconnect here.
                    if self.idle_timeout is None:
                        r = await self._reader.readexactly(1)
                    else:
                        r = await asyncio.wait_for(self._reader.readexactly(1),
                                                   self.idle_timeout)

                    if r != b'<':
                        # Not a start of frame — resynchronise. BOUNDED: a peer that never
                        # sends '<' must not grow this buffer without limit.
                        if not await self._resync(r):
                            continue        # never found a frame start; do not parse junk
                                            # as a length header (that misframed the stream)

                    # Next two bytes are the frame size. These timeouts protect an
                    # INCOMPLETE frame and are deliberately kept.
                    try:
                        r = await asyncio.wait_for(self._reader.readexactly(2),
                                                   _FRAME_HEADER_TIMEOUT)
                        size = struct.unpack("<H", r)[0]

                        r = await asyncio.wait_for(self._reader.readexactly(size),
                                                   _FRAME_BODY_TIMEOUT)

                        logger.debug(f"Received frame, {len(r)} bytes")

                        return r

                    except TimeoutError:
                        # The frame START was accepted, so the boundary is already known —
                        # and a partially delivered header/body means we no longer know
                        # where the next frame begins. Continuing on the same connection
                        # would let leftover header/body bytes (possibly a '<' inside a
                        # payload) be read as framing, staying desynchronised indefinitely.
                        # Drop it; the client reconnects and both sides resynchronise.
                        logger.warning("Timed out mid-frame — dropping the connection to "
                                       "resynchronise")
                        self._reset_connection(close=True)
                        break
            except asyncio.exceptions.IncompleteReadError:
                # The connection was lost
                logger.info("Connection to Meshcore app lost")
                self._reset_connection(close=True)
            except TimeoutError:
                # Idle timeout (only reachable when one is configured)
                logger.info("Connection to Meshcore app timed out")
                self._reset_connection(close=True)
            except Exception as e:
                logger.error(f"Connection lost due to: {repr(e)}")
                self._reset_connection(close=True)

    async def _resync(self, first):
        """Consume bytes until a start-of-frame '<'. True when one was found.

        Returns False when the peer stops sending or floods more than MAX_JUNK_BYTES of
        non-frame data — the caller must then NOT read a length header, or it would
        interpret junk as a frame and stay misframed.
        """
        junkdata = bytearray(first)
        while True:
            if len(junkdata) >= MAX_JUNK_BYTES:
                logger.warning(f"Discarding {len(junkdata)} bytes of junk from companion "
                               f"data without finding a frame start")
                return False
            try:
                r = await asyncio.wait_for(self._reader.readexactly(1),
                                           _RESYNC_BYTE_TIMEOUT)
            except TimeoutError:
                logger.warning(f"Junk data in companion data, {len(junkdata)} bytes: "
                               f"{hexlify(bytes(junkdata)).decode()}")
                return False
            if r == b'<':
                if junkdata:
                    logger.warning(f"Junk data before frame in companion data, "
                                   f"{len(junkdata)} bytes: "
                                   f"{hexlify(bytes(junkdata)).decode()}")
                return True
            junkdata += r

    def _reset_connection(self, close=True):
        """Drop reader AND writer together, so a stale reader can never be used against a
        connection we have already given up on.

        ALWAYS close the transport by default: a peer that hangs up leaves the socket in
        CLOSE_WAIT until the writer is closed, and asyncio keeps the transport alive after
        EOF, so a client that reconnects (a phone changing network) leaked a descriptor per
        cycle.
        """
        if close and self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = None
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
            self._reset_connection(close=True)
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

