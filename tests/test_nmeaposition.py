"""Live position from an NMEA stream.

Two things this must never do, and both are tested: advertise a position it does not
actually have (no fix, empty coordinates, a stale fix), and leak coordinates or raw
sentences into the log.

The endpoint tests drive a REAL PTY, not a mocked reader, because "works against a PTY" is
the actual integration contract — that is how an external supplier hands NMEA over.
"""

import asyncio
import logging
import os
import pty
import time
import unittest

from nmeaposition import (
    DEFAULT_STALE_AFTER,
    NMEAPosition,
    nmea_checksum_ok,
    parse_position,
)


def nmea(body):
    """Append the correct XOR checksum to a `$...` sentence body."""
    ck = 0
    for c in body[1:].encode():
        ck ^= c
    return f"{body}*{ck:02X}".encode()


# Greenwich. Publicly known, obviously synthetic, and nobody's home.
GGA_FIX = nmea("$GPGGA,123519,5128.6640,N,00000.0900,W,1,08,0.9,545.4,M,46.9,M,,")
GGA_NOFIX = nmea("$GPGGA,123519,5128.6640,N,00000.0900,W,0,00,,,M,,M,,")
GGA_EMPTY = nmea("$GPGGA,123519,,,,,1,08,0.9,545.4,M,46.9,M,,")
RMC_FIX = nmea("$GPRMC,123519,A,5128.6640,N,00000.0900,W,022.4,084.4,230394,003.1,W")
RMC_VOID = nmea("$GPRMC,123519,V,5128.6640,N,00000.0900,W,022.4,084.4,230394,003.1,W")
GLL_FIX = nmea("$GPGLL,5128.6640,N,00000.0900,W,123519,A")
GLL_VOID = nmea("$GPGLL,5128.6640,N,00000.0900,W,123519,V")
GNS_FIX = nmea("$GNGNS,123519,5128.6640,N,00000.0900,W,AA,08,0.9,545.4,46.9,,")
GNS_NOFIX = nmea("$GNGNS,123519,5128.6640,N,00000.0900,W,NN,00,,,,,")

LAT, LON = 51.4777333, -0.0015


class _Id:
    """Stand-in for SelfIdentity: only `latlon` matters here."""

    def __init__(self, latlon=None):
        self.latlon = latlon


class ChecksumTests(unittest.TestCase):
    def test_valid_checksum(self):
        self.assertTrue(nmea_checksum_ok(GGA_FIX))

    def test_corrupted_body_fails(self):
        bad = GGA_FIX.replace(b'5128', b'5129')
        self.assertFalse(nmea_checksum_ok(bad))

    def test_malformed_shapes_fail_without_raising(self):
        for bad in (b'', b'$', b'nonsense', b'$GPGGA,1,2,3', b'$GPGGA*', b'$GPGGA*ZZ',
                    b'*AA', b'$GPGGA,1*'):
            self.assertFalse(nmea_checksum_ok(bad), bad)


class ParseTests(unittest.TestCase):
    def test_gga_and_rmc_and_gll_and_gns_fixes(self):
        for s in (GGA_FIX, RMC_FIX, GLL_FIX, GNS_FIX):
            with self.subTest(s[:6]):
                got = parse_position(s)
                self.assertIsNotNone(got, s)
                self.assertAlmostEqual(got[0], LAT, places=4)
                self.assertAlmostEqual(got[1], LON, places=4)

    def test_a_sentence_claiming_no_fix_is_not_a_position(self):
        for s in (GGA_NOFIX, RMC_VOID, GLL_VOID, GNS_NOFIX):
            with self.subTest(s[:6]):
                self.assertIsNone(parse_position(s), s)

    def test_a_fix_with_empty_coordinates_is_not_a_position(self):
        """A receiver that is still searching emits exactly this. Reading it as a position
        would put the node at 0,0 — in the Gulf of Guinea."""
        self.assertIsNone(parse_position(GGA_EMPTY))

    def test_bad_checksum_is_not_a_position(self):
        self.assertIsNone(parse_position(GGA_FIX.replace(b'5128', b'5129')))

    def test_unknown_and_malformed_sentences_are_ignored(self):
        for s in (nmea("$GPTXT,01,01,02,ANTSTATUS=OK"), nmea("$GPVTG,054.7,T,034.4,M"),
                  b'', b'garbage', b'$*00', b'$GP*00', nmea("$GPGGA")):
            with self.subTest(s[:10]):
                self.assertIsNone(parse_position(s), s)

    def test_southern_and_western_hemispheres_are_signed(self):
        south = nmea("$GPGGA,123519,3350.0000,S,15112.0000,E,1,08,0.9,0,M,0,M,,")
        got = parse_position(south)
        self.assertLess(got[0], 0)
        self.assertGreater(got[1], 0)

    def test_out_of_range_values_are_rejected(self):
        self.assertIsNone(parse_position(
            nmea("$GPGGA,123519,9999.0000,N,00000.0900,W,1,08,0.9,0,M,0,M,,")))


class FeedTests(unittest.TestCase):
    def test_a_fix_updates_the_identity(self):
        me = _Id()
        NMEAPosition(me, "/dev/null").feed(GGA_FIX + b'\r\n')
        self.assertAlmostEqual(me.latlon[0], LAT, places=4)

    def test_partial_lines_are_reassembled(self):
        me = _Id()
        src = NMEAPosition(me, "/dev/null")
        whole = GGA_FIX + b'\r\n'
        for i in range(0, len(whole), 7):
            src.feed(whole[i:i + 7])
        self.assertIsNotNone(me.latlon)

    def test_a_flood_without_newlines_does_not_grow_without_bound(self):
        me = _Id()
        src = NMEAPosition(me, "/dev/null")
        for _ in range(100):
            src.feed(b'x' * 1024)
        self.assertLessEqual(len(src._buf), 1024)
        self.assertIsNone(me.latlon)

    def test_static_position_is_untouched_when_no_fix_ever_arrives(self):
        me = _Id((1.0, 2.0))
        src = NMEAPosition(me, "/dev/null")
        src.feed(GGA_NOFIX + b'\r\n')
        self.assertEqual(me.latlon, (1.0, 2.0))


class StaleTests(unittest.TestCase):
    def test_a_stale_fix_is_cleared_not_kept(self):
        """A node that has moved must not keep advertising where it used to be."""
        me = _Id()
        src = NMEAPosition(me, "/dev/null", stale_after=0.05)
        src.feed(GGA_FIX + b'\r\n')
        self.assertIsNotNone(me.latlon)
        time.sleep(0.08)
        src.feed(b'')                      # any activity re-checks staleness
        self.assertIsNone(me.latlon)

    def test_staleness_reverts_to_the_configured_static_position(self):
        me = _Id((1.0, 2.0))
        src = NMEAPosition(me, "/dev/null", stale_after=0.05)
        src.feed(GGA_FIX + b'\r\n')
        self.assertNotEqual(me.latlon, (1.0, 2.0))
        time.sleep(0.08)
        src.feed(b'')
        self.assertEqual(me.latlon, (1.0, 2.0))

    def test_recovery_is_automatic_when_fixes_resume(self):
        me = _Id()
        src = NMEAPosition(me, "/dev/null", stale_after=0.05)
        src.feed(GGA_FIX + b'\r\n')
        time.sleep(0.08)
        src.feed(b'')
        self.assertIsNone(me.latlon)
        src.feed(RMC_FIX + b'\r\n')
        self.assertIsNotNone(me.latlon)

    def test_a_fresh_fix_is_not_expired(self):
        me = _Id()
        src = NMEAPosition(me, "/dev/null", stale_after=DEFAULT_STALE_AFTER)
        src.feed(GGA_FIX + b'\r\n')
        src.feed(b'')
        self.assertIsNotNone(me.latlon)


class PrivacyTests(unittest.TestCase):
    def test_no_coordinates_or_raw_sentences_reach_the_log(self):
        """Coordinates are the operator's location. They must not be logged, and neither
        must the sentences that carry them."""
        me = _Id()
        src = NMEAPosition(me, "/dev/null", stale_after=0.05)
        with self.assertLogs('nmeaposition', level='DEBUG') as cm:
            src.feed(GGA_FIX + b'\r\n')
            time.sleep(0.08)
            src.feed(b'')
        blob = "\n".join(cm.output)
        for leak in ("5128.6640", "00000.0900", "51.47", "-0.001", "$GPGGA", "GPGGA"):
            self.assertNotIn(leak, blob, f"leaked {leak!r}: {blob}")

    def test_the_identity_is_updated_but_nothing_is_persisted(self):
        """A live fix is in-memory only — it must never be written back to config."""
        me = _Id((1.0, 2.0))
        src = NMEAPosition(me, "/dev/null")
        src.feed(GGA_FIX + b'\r\n')
        self.assertEqual(src.static_latlon, (1.0, 2.0))   # the configured value is kept
        self.assertNotEqual(me.latlon, (1.0, 2.0))


class PtyEndpointTests(unittest.TestCase):
    """Against a REAL PTY — the integration contract for an external NMEA supplier."""

    def _run(self, coro, timeout=5.0):
        return asyncio.run(asyncio.wait_for(coro, timeout))

    def test_a_fix_written_to_a_pty_reaches_the_identity(self):
        master, slave = pty.openpty()
        me = _Id()
        src = NMEAPosition(me, os.ttyname(slave))

        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(src.run(stop))
            await asyncio.sleep(0.2)
            os.write(master, GGA_FIX + b'\r\n')
            for _ in range(50):
                await asyncio.sleep(0.05)
                if me.latlon is not None:
                    break
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            self._run(go())
        finally:
            os.close(master)
            os.close(slave)
        self.assertIsNotNone(me.latlon)
        self.assertAlmostEqual(me.latlon[0], LAT, places=4)

    def test_a_missing_endpoint_is_retried_not_fatal(self):
        """The supplier may create the PTY after this node starts."""
        me = _Id()
        src = NMEAPosition(me, "/nonexistent/tty-does-not-exist")

        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(src.run(stop))
            await asyncio.sleep(0.3)
            alive = not task.done()          # still retrying, not crashed
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return alive

        self.assertTrue(self._run(go()))
        self.assertIsNone(me.latlon)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()


class HemisphereTests(unittest.TestCase):
    """AUDIT-FOUND: anything that was not S/W was treated as positive, so a checksum-valid
    but malformed sentence could yield a confident position in the wrong hemisphere."""

    def test_an_invalid_hemisphere_letter_is_not_a_position(self):
        for lat_h, lon_h in (("X", "W"), ("N", "Q"), ("E", "W"), ("N", "S"), ("", "W")):
            s = nmea(f"$GPGGA,123519,5128.6640,{lat_h},00000.0900,{lon_h},"
                     f"1,08,0.9,545.4,M,46.9,M,,")
            with self.subTest(lat_h=lat_h, lon_h=lon_h):
                self.assertIsNone(parse_position(s), s)

    def test_latitude_only_accepts_n_or_s_and_longitude_e_or_w(self):
        for lat_h, sign in (("N", 1), ("S", -1)):
            got = parse_position(nmea(
                f"$GPGGA,123519,5128.6640,{lat_h},00000.0900,W,1,08,0.9,545.4,M,46.9,M,,"))
            self.assertIsNotNone(got)
            self.assertEqual(got[0] > 0, sign > 0)
        for lon_h, sign in (("E", 1), ("W", -1)):
            got = parse_position(nmea(
                f"$GPGGA,123519,5128.6640,N,00100.0900,{lon_h},1,08,0.9,545.4,M,46.9,M,,"))
            self.assertIsNotNone(got)
            self.assertEqual(got[1] > 0, sign > 0)

    def test_lowercase_hemispheres_are_accepted(self):
        self.assertIsNotNone(parse_position(nmea(
            "$GPGGA,123519,5128.6640,n,00000.0900,w,1,08,0.9,545.4,M,46.9,M,,")))
