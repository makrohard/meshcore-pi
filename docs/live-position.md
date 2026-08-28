# Live position from an NMEA stream

`meshcore-pi` can take its position from a live NMEA stream instead of a fixed `lat`/`lon`.

The design rule is that **this node knows nothing about where the NMEA comes from**. It
opens a device path, reads sentences, and keeps its own position current. A real receiver,
a PTY fed by gpsd, a bridge or a replay are all identical from here — which is what keeps
receiver ownership, daemon clients, fixed-position policy and hardware discovery out of
this codebase entirely.

## Configuration

Under the device table (e.g. `[device.companion]`):

```toml
# The NMEA endpoint. Unset (the default) = no live position.
gps.device = "/dev/ttyACM0"

# Optional.
gps.baud = 9600          # default 9600
gps.stale_after = 60     # seconds; default 60
```

## Behaviour

| Situation | Result |
|---|---|
| `gps.device` unset | Nothing runs. The static `lat`/`lon` behave exactly as before. |
| `gps.device` set, endpoint does not exist yet | Retried with backoff (1 s up to 30 s). Not fatal — the supplier may create the PTY after this node starts. |
| Valid fix received | The node's position updates; later adverts and self-info carry it. |
| Endpoint disappears | Reconnects automatically. |
| No valid fix for `gps.stale_after` seconds | The live position is **cleared** — reverting to the configured static `lat`/`lon` if there was one, otherwise to no position. |
| Valid fixes resume | Picked up automatically; no restart needed. |

A *fix* means a checksum-valid GGA, RMC, GLL or GNS sentence whose status field reports a
fix **and** whose latitude/longitude fields are populated. A sentence that claims a fix
with empty coordinates — what a receiver emits while it is still searching — is not a
position, and is never read as 0,0.

Stale fixes are cleared rather than kept because a node that has moved must not keep
telling the mesh where it used to be. No position is more honest than a wrong one.

## Privacy and persistence

* Coordinates and raw NMEA sentences are **never** written to the log. The log records
  that a fix was acquired, or that the position went stale — never a value.
* A live fix is **never persisted**. It updates the in-memory identity only; the `lat`/`lon`
  in the config file stay exactly as the operator wrote them.

## Integration contract for LoRaHAM Pi Control (LHPC)

LHPC already resolves one global position source and can bridge it to an NMEA PTY. To drive
this node from it:

1. Have the LHPC GPS bridge publish an NMEA PTY for the MeshCore consumer, exactly as it
   does for other consumers that can only read a device.
2. Write that PTY path into the generated `meshcore-pi.toml` as
   `[device.companion] gps.device`.
3. Leave `gps.baud` and `gps.stale_after` unset unless there is a reason to change them.

Then:

* **Enabling** dynamic position = writing `gps.device`.
* **Disabling** it = omitting the key. The node then uses `lat`/`lon` (or no position),
  with no other behaviour change. There is no separate "off" value to set.
* **Before the PTY exists** the node keeps retrying, so LHPC may create the endpoint before
  or after starting the node; ordering does not matter and a missing PTY is not a start
  failure.
* **Static and dynamic can coexist**: a configured `lat`/`lon` acts as the fallback the node
  reverts to if the live source goes stale. Set only one of them if that is not wanted.
* This node performs **no** gpsd protocol, device discovery or receiver arbitration. If LHPC
  wants a fixed position instead, it should write `lat`/`lon` and leave `gps.device` unset —
  not point this node at a receiver and expect it to decide.

Note that the node reads `lat`/`lon` from config **once at startup**, so a config-time
position changes only on restart. `gps.device` is what makes the position follow the box
while it runs.
