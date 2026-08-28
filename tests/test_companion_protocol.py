"""Companion/App protocol compatibility with current official clients.

Where possible these use the CURRENT OFFICIAL CLIENT as an interoperability oracle rather
than restating this node's own assumptions on both sides of an assertion: the frames this
node emits are handed to `meshcore_py`'s real `MessageReader` and the parsed result is
checked. When that client is not importable the oracle tests skip and the byte-layout
tests still run.

Oracle: meshcore-dev/meshcore_py @ dd5502b968e65c1ff046dfc0674ccda10a337902
Firmware reference: meshcore-dev/MeshCore @ 0679dbeffc504d562d2f09eb072fdc223f8ffc2a

To run the oracle tests, put meshcore_py (and its deps) on PYTHONPATH.
"""

import asyncio
import struct
import unittest

import companionradio as cr


def _oracle():
    """The official client's frame reader, or None if it is not installed.

    Two obstacles, both handled here rather than in the shell:

    * This repository has its OWN `meshcore.py` at the root, and it is a runnable node
      script, not a package. It sits earlier on `sys.path` than any installed client, so a
      plain `import meshcore.reader` finds the script — and importing it would START A
      NODE. The official package is therefore located as a DIRECTORY package and imported
      with its own path first, and the result is checked to be a package.
    * `cayennelpp` is a telemetry-only dependency of the client's parsing module; the
      BATTERY and DEVICE_INFO paths never touch it, so a missing one is stubbed rather
      than losing the whole oracle.
    """
    import os
    import sys
    import types

    src = next((p for p in sys.path
                if p and os.path.isfile(os.path.join(p, "meshcore", "reader.py"))), None)
    if src is None:
        return None

    try:
        import cayennelpp  # noqa: F401
    except ImportError:
        m = types.ModuleType("cayennelpp")
        m.LppFrame = m.LppData = object
        sys.modules.setdefault("cayennelpp", m)

    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items()
                  if k == "meshcore" or k.startswith("meshcore.")}
    try:
        for k in saved_mods:
            del sys.modules[k]
        sys.path.insert(0, src)
        import meshcore
        if not hasattr(meshcore, "__path__"):
            return None                      # we got the node script, not the client
        from meshcore.reader import MessageReader
        return MessageReader
    except Exception:
        return None
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules
                  if k == "meshcore" or k.startswith("meshcore.")]:
            del sys.modules[k]
        sys.modules.update(saved_mods)


def _parse(frame):
    """Parse one frame with the official client; returns [(event type, payload)]."""
    MessageReader = _oracle()

    class Cap:
        def __init__(self):
            self.events = []

        async def dispatch(self, ev):
            self.events.append(ev)

    async def go():
        cap = Cap()
        await MessageReader(cap).handle_rx(frame)
        return [(e.type.value, e.payload) for e in cap.events]

    return asyncio.run(go())


class BattAndStorageLayoutTests(unittest.TestCase):
    """Command 20 is CMD_GET_BATT_AND_STORAGE in current firmware — battery AND storage.
    This node used to answer with the old 2-byte battery-only shape."""

    def test_frame_is_the_full_eleven_byte_shape(self):
        f = cr.batt_and_storage_resp()
        self.assertEqual(len(f), 11)
        code, mv, used, total = struct.unpack("<BHII", f)
        self.assertEqual(code, cr.RESP_CODE_BATT_AND_STORAGE)
        self.assertEqual(code, 12)                       # upstream RESP_CODE_BATT_AND_STORAGE
        self.assertEqual(mv, cr.BATTERY_NOT_APPLICABLE_MV)
        self.assertEqual((used, total), (0, 0))

    def test_storage_is_reported_as_unknown_not_invented(self):
        """0/0 means "not reported". Passing off the host filesystem's free space as device
        flash would be a confident wrong answer instead of an honest absent one."""
        _c, _mv, used, total = struct.unpack("<BHII", cr.batt_and_storage_resp())
        self.assertEqual((used, total), (0, 0))

    def test_battery_is_an_obvious_not_applicable_marker(self):
        """A Pi has no battery. 0xffff is not a plausible cell voltage, and unlike 0 it does
        not present as a node about to die."""
        self.assertEqual(cr.BATTERY_NOT_APPLICABLE_MV, 0xffff)

    def test_the_old_command_name_still_refers_to_the_same_number(self):
        self.assertEqual(cr.CMD_GET_BATT_AND_STORAGE, 20)
        self.assertEqual(cr.CMD_GET_BATTERY_VOLTAGE, cr.CMD_GET_BATT_AND_STORAGE)
        self.assertEqual(cr.RESP_CODE_BATTERY_VOLTAGE, cr.RESP_CODE_BATT_AND_STORAGE)


class DeviceInfoLayoutTests(unittest.TestCase):
    def test_layout_matches_the_advertised_version(self):
        f = cr.device_info_resp(4)
        code, ver, maxc, maxch, pin = struct.unpack("<BBBBL", f[:8])
        self.assertEqual(code, cr.RESP_CODE_DEVICE_INFO)
        self.assertEqual(ver, cr.FIRMWARE_VER_CODE)
        self.assertEqual(maxch, 4)
        # ver(1) + 6 reserved + build(12) + model(40) + ver string(20)
        self.assertEqual(len(f), 1 + 1 + 6 + 12 + 40 + 20)

    def test_advertised_version_is_not_inflated_to_current_firmware(self):
        """The version is a promise about which fields follow. Current firmware is 13, but
        this node sends neither the >=9 `repeat` byte nor the >=10 `path_hash_mode` byte, so
        claiming 13 would make a client read bytes that are not there."""
        self.assertEqual(cr.FIRMWARE_VER_CODE, 5)
        self.assertLess(cr.FIRMWARE_VER_CODE, 9)


class UnsupportedCommandTests(unittest.TestCase):
    def test_error_codes_match_upstream(self):
        self.assertEqual(cr.ERR_CODE_UNSUPPORTED_CMD, 1)
        self.assertEqual(cr.ERR_CODE_NOT_FOUND, 2)

    def test_an_unknown_command_is_answered_unsupported_not_not_found(self):
        """Current clients probe for newer features. "Not found" describes a failed lookup;
        the honest answer to a command this node does not implement is UNSUPPORTED_CMD —
        and never a fake success."""
        self.assertEqual(cr.ERR(cr.ERR_CODE_UNSUPPORTED_CMD), bytes([1, 1]))


@unittest.skipIf(_oracle() is None, "meshcore_py (official client) not installed")
class OfficialClientInteropTests(unittest.TestCase):
    """The frames this node emits, parsed by the real current client."""

    def test_client_reads_battery_and_storage(self):
        events = _parse(cr.batt_and_storage_resp())
        self.assertEqual(len(events), 1)
        kind, payload = events[0]
        self.assertEqual(kind, "battery_info")
        self.assertEqual(payload["level"], 0xffff)
        # The storage fields are only read when the frame is the full 11 bytes; their
        # presence is the whole point of this change.
        self.assertIn("used_kb", payload)
        self.assertIn("total_kb", payload)
        self.assertEqual((payload["used_kb"], payload["total_kb"]), (0, 0))

    def test_the_old_short_frame_would_have_lost_the_storage_fields(self):
        """Proves the regression this fixes, using the client itself."""
        old = struct.pack("<BH", cr.RESP_CODE_BATT_AND_STORAGE, 0xffff)
        _kind, payload = _parse(old)[0]
        self.assertNotIn("used_kb", payload)

    def test_client_reads_device_info(self):
        events = _parse(cr.device_info_resp(4))
        self.assertEqual(len(events), 1)
        kind, payload = events[0]
        self.assertEqual(kind, "device_info")
        self.assertEqual(payload["fw ver"], cr.FIRMWARE_VER_CODE)
        self.assertEqual(payload["max_channels"], 4)
        self.assertEqual(payload["model"], "Python Companion")
        self.assertEqual(payload["fw_build"], cr.FIRMWARE_BUILD_DATE)

    def test_client_does_not_expect_fields_we_do_not_send(self):
        """At the advertised version the client must not look for the repeater or
        path-hash-mode bytes — if it did, our frame would be short."""
        _kind, payload = _parse(cr.device_info_resp(4))[0]
        self.assertNotIn("repeat", payload)
        self.assertNotIn("path_hash_mode", payload)


if __name__ == '__main__':
    unittest.main()
