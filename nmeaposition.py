"""Optional live position from an NMEA stream.

A generic, self-contained position source: point it at a serial device or a PTY and it
keeps a `SelfIdentity`'s `latlon` current, so subsequent adverts and self-information carry
where the node actually is.

Deliberately knows nothing about where the NMEA comes from. A real receiver, a PTY fed by
gpsd, a bridge, a replay — all identical from here. That keeps this node free of receiver
ownership, daemon clients, fixed-position policy and hardware discovery, all of which
belong to whatever supplies the stream.

Privacy: coordinates and raw sentences NEVER reach the log. Positions are the operator's
location; this module logs only that a fix arrived, or that one has gone stale.

Nothing is persisted. A live fix updates the in-memory identity only; the configured
static `lat`/`lon` in the config file are left exactly as the operator wrote them.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Sentences that can carry a position. GGA/RMC are the two every receiver emits; GLL and
# GNS are the same shape and cost nothing extra to accept.
#
#   value = (index of the latitude field, index of the status/quality field or None)
# Field indices count the sentence type itself as field 0.
_SENTENCES = {
    'GGA': (2, 6),      # ...,lat,N,lon,E,quality,...     quality 0 = no fix
    'RMC': (3, 2),      # ...,status,lat,N,lon,E,...      status 'V' = warning/invalid
    'GLL': (1, 6),      # ...,lat,N,lon,E,time,status     status 'V' = invalid
    'GNS': (2, None),   # ...,lat,N,lon,E,mode,...        mode handled below
}

# How long a fix stays valid once the sentences stop being useful. After this the position
# is CLEARED rather than kept: a node that has moved must not keep advertising where it
# used to be, and no position is more honest than a stale one.
DEFAULT_STALE_AFTER = 60.0

# Reconnect backoff for a device that disappears (unplugged receiver, PTY not created yet).
_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 30.0

# A single NMEA sentence is at most 82 bytes. Anything longer is a peer that never sends a
# newline, and must not be buffered without limit.
_MAX_LINE = 512


def nmea_checksum_ok(line):
    """True when `line` is a complete `$...*HH` sentence whose XOR checksum matches.

    A sentence without a valid checksum is not evidence of anything, so it is dropped
    rather than parsed.
    """
    if not line.startswith(b'$') or b'*' not in line:
        return False
    body, _, checksum = line[1:].partition(b'*')
    if len(checksum) < 2:
        return False
    try:
        want = int(checksum[:2], 16)
    except ValueError:
        return False
    got = 0
    for b in body:
        got ^= b
    return got == want


def _degrees(value, hemisphere):
    """NMEA ddmm.mmmm + hemisphere -> signed decimal degrees, or None.

    An empty field is the normal way a receiver says "no position yet", so it is not an
    error — it simply is not a fix.
    """
    if not value or not hemisphere:
        return None
    try:
        raw = float(value)
    except ValueError:
        return None
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    if minutes >= 60.0:
        return None
    out = degrees + minutes / 60.0
    if hemisphere in (b'S', b'W', 'S', 'W'):
        out = -out
    return out


def parse_position(line):
    """(lat, lon) from one NMEA sentence, or None.

    None covers every "no position" case: not a sentence we read, bad checksum, a status
    field saying there is no fix, or a fix claimed with empty coordinate fields — which is
    exactly what a receiver emits while it is still searching, and must never be read as
    a position at 0,0.
    """
    if not nmea_checksum_ok(line):
        return None
    fields = line[1:].split(b'*')[0].split(b',')
    if not fields or len(fields[0]) < 5:
        return None
    kind = fields[0][2:5].decode('ascii', 'ignore').upper()
    spec = _SENTENCES.get(kind)
    if spec is None:
        return None
    lat_i, status_i = spec

    if status_i is not None and status_i < len(fields):
        status = fields[status_i]
        if kind == 'GGA':
            # Fix quality: 0 means no fix.
            if not status or status == b'0':
                return None
        elif not status or status.upper().startswith(b'V'):
            # 'V' = navigation receiver warning (invalid) for RMC/GLL.
            return None
    if kind == 'GNS':
        # Mode indicator, one character per constellation: 'N' means no fix for that one.
        mode = fields[6] if len(fields) > 6 else b''
        if not mode or set(mode.upper()) <= {ord('N')}:
            return None

    if lat_i + 3 >= len(fields):
        return None
    lat = _degrees(fields[lat_i], fields[lat_i + 1])
    lon = _degrees(fields[lat_i + 2], fields[lat_i + 3])
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


class NMEAPosition:
    """Keeps `identity.latlon` current from an NMEA device.

    Only takes ownership of the position when it is actually configured — with no device,
    nothing here runs and the static `lat`/`lon` behave exactly as before.
    """

    def __init__(self, identity, device, baud=9600, stale_after=DEFAULT_STALE_AFTER):
        self.identity = identity
        self.device = device
        self.baud = baud
        self.stale_after = stale_after
        # The static position the operator configured, kept so it can be restored if the
        # dynamic source never produces a fix or goes stale. `None` when none was set.
        self.static_latlon = identity.latlon
        self.last_fix = None            # monotonic time of the last valid fix
        self._buf = bytearray()

    def _apply(self, latlon):
        """Publish a new position. Logs THAT a fix arrived, never where."""
        first = self.identity.latlon is None
        self.identity.latlon = latlon
        self.last_fix = time.monotonic()
        if first:
            logger.info("Position acquired from NMEA source")

    def _expire(self):
        """Drop a position that has stopped being refreshed.

        Reverts to the configured static position if there was one, otherwise to no
        position at all. Continuing to advertise the last known fix would tell the mesh the
        node is somewhere it has left.
        """
        if self.last_fix is None or self.identity.latlon == self.static_latlon:
            return
        if time.monotonic() - self.last_fix > self.stale_after:
            self.identity.latlon = self.static_latlon
            self.last_fix = None
            logger.warning("No valid position for %.0f s — cleared the live position",
                           self.stale_after)

    def feed(self, data):
        """Push received bytes through the line assembler. Returns the number of fixes."""
        fixes = 0
        self._buf += data
        while b'\n' in self._buf:
            line, _, rest = self._buf.partition(b'\n')
            self._buf = bytearray(rest)
            latlon = parse_position(bytes(line).strip())
            if latlon is not None:
                self._apply(latlon)
                fixes += 1
        if len(self._buf) > _MAX_LINE:
            # A source with no line breaks; keep the tail so a real sentence can still
            # start, but never let the buffer grow without bound.
            del self._buf[:-_MAX_LINE]
        self._expire()
        return fixes

    async def run(self, stop=None):
        """Read the device forever, reconnecting when it disappears.

        The endpoint legitimately may not exist yet (a PTY created later by whatever
        supplies the stream), so an open failure is a retry, not a fatal error.
        """
        import serial_asyncio

        delay = _RECONNECT_MIN
        while stop is None or not stop.is_set():
            writer = None
            try:
                reader, writer = await serial_asyncio.open_serial_connection(
                    url=self.device, baudrate=self.baud)
                logger.info("NMEA position source connected")
                delay = _RECONNECT_MIN
                while stop is None or not stop.is_set():
                    try:
                        data = await asyncio.wait_for(reader.read(256), timeout=1.0)
                    except (TimeoutError, asyncio.TimeoutError):
                        self._expire()      # quiet source still ages out
                        continue
                    if not data:
                        break               # endpoint closed
                    self.feed(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never log the exception's payload beyond its type/message: it can carry
                # the device path, which is fine, but keep it terse.
                logger.warning("NMEA position source unavailable (%s); retrying",
                               type(e).__name__)
            finally:
                # CLOSE THE TRANSPORT on every exit from the read loop. Dropping the writer
                # left the serial transport registered with the event loop, so it was never
                # collected: an endpoint that opens then EOFs (a PTY whose feeder exited)
                # reconnects once a second and leaks a descriptor each time, exhausting the
                # process's file descriptors within the hour — taking the radio interfaces
                # down with it.
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass
            self._expire()
            try:
                await asyncio.wait_for(
                    stop.wait() if stop is not None else asyncio.Event().wait(), delay)
                return                      # stop was set during the backoff
            except (TimeoutError, asyncio.TimeoutError):
                pass
            delay = min(delay * 2, _RECONNECT_MAX)
