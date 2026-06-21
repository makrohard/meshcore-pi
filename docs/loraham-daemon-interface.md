# LoRaHAM daemon interface for meshcore-pi

## Purpose

This interface lets `meshcore-pi` use a LoRaHAM Pi HAT through
`loraham_daemon`, instead of accessing the radio chips directly over SPI/GPIO.

## Requirements

Requires Python 3.11+ (matching the rest of `meshcore-pi`) and a
`loraham_daemon` 111-candidate or newer (framed `RX_PACKET` metadata,
`TX_RESULT` frames, and the `MANAGED` TX mode).

The LoRaHAM daemon must expose:

```text
/tmp/lora868f.sock      framed data socket
/tmp/loraconf868.sock   CONF/status socket
```

On connect the interface uses these CONF commands:

```text
GET STATUS            -> read CADWAIT to size the TX result timeout
SET TXMODE=MANAGED    -> daemon performs CAD/LBT (only when enable_tx)
SET TXRESULT=1        -> daemon returns one TX_RESULT per TX (only when enable_tx)
```

Channel access (LBT/CAD) is delegated to the daemon. The interface no longer
polls `TX`/`CAD` status lines; it transmits and waits for the `TX_RESULT` frame.

> Note: `TXMODE`, `TXRESULT`, and `CADWAIT` are **global per-band** daemon
> state shared by all clients of that band. On a radio band dedicated to this
> MeshCore node that is unproblematic. `SET TXMODE=MANAGED` is sent explicitly in
> case the daemon was started with `--tx-mode-<band>=direct`.

The unframed raw socket is not used.

## Radio ownership

Only one stack should control one physical LoRa radio at a time.

Do not run `meshtasticd`, LoRaHAM APRS/chat/KISS clients, and `meshcore-pi`
against the same LoRaHAM radio at the same time.

## Example configuration

See `examples/config-loraham868.toml`.

Minimal RX-only interface block:

```toml
[interface.loraham868]
type = "loraham"
data_socket = "/tmp/lora868f.sock"
config_socket = "/tmp/loraconf868.sock"
preset = "eu_uk_long"
```

Useful TX options:

```toml
enable_tx = true
tx_result_margin = 1.0
airtime = 10
```

`airtime` is the duty-cycle limit in percent. `tx_result_margin` (seconds) is
added on top of `CADWAIT + packet airtime` when waiting for the `TX_RESULT`.

## Presets

```text
eu_uk_long    869.525 MHz, BW 250 kHz, SF11, CR5, preamble 16, TX 14 dBm
eu_uk_medium  869.525 MHz, BW 250 kHz, SF10, CR5, preamble 16, TX 14 dBm
eu_uk_narrow  869.618 MHz, BW 62.5 kHz, SF8, CR8, preamble 16, TX 14 dBm
```

Explicit values such as `frequency`, `bw`, `sf`, `cr`, `preamble`, `ldro`,
or `txpower` may override preset fields.

## RX path

The interface reads `RX_PACKET` frames from the framed data socket. Each frame
carries 4 bytes of metadata (`int16` RSSI in centi-dBm, `int16` SNR in
centi-dB, little-endian) before the RF payload. The interface decodes these to
dBm/dB and puts a `(payload, rssi, snr)` tuple into the `meshcore-pi` receive
queue, like the other interfaces. The sentinel `-32768` (unavailable) maps to
`0.0`.

## TX path

TX is disabled by default. Enable it explicitly:

```toml
enable_tx = true
```

TX is **managed by the daemon**: the interface writes one `TX_PACKET` frame and
waits for the corresponding `TX_RESULT` frame. There is no client-side CAD/busy
polling.

The `TX_RESULT` payload is exactly 4 bytes: `[status u8][flags u8][seq u16 LE]`.
The wire status values (loraham_daemon `framed_data.h`) are:

```text
0 OK
1 BUSY
2 CHANNEL_BUSY
3 RADIO_NOT_READY
4 RADIO_ERROR
5 INVALID_PACKET
6 INVALID_BAND
```

There is **no** CAD-timeout status. CAD-timeout context is flag bit `0x04`:
a not-sent MANAGED TX reports `CHANNEL_BUSY (2)`; a send-after-timeout reports
`OK (0)` with the `0x04` flag set (i.e. it was transmitted). Flags:
`MANAGED=0x01`, `DEFERRED=0x02`, `CAD_TIMEOUT=0x04` (informational; status is
authoritative).

```text
write TX_PACKET
wait for TX_RESULT, timeout = CADWAIT + airtime(packet) + tx_result_margin

OK (0)                       -> count airtime, return airtime (ms)
                                (also for OK + CAD_TIMEOUT flag = sent after wait)
BUSY (1) / CHANNEL_BUSY (2)  -> not sent, return 0, no duty-cycle entry
RADIO_NOT_READY (3) / RADIO_ERROR (4) -> log error, return 0
INVALID_PACKET (5) / INVALID_BAND (6) -> log error, return 0
ERROR frame                  -> in-flight TX resolved as failed, return 0
timeout / malformed result   -> log warning, return 0, force reconnect
```

The daemon delivers exactly one final `TX_RESULT` per `TX_PACKET`. The whole TX
transaction (arm pending result -> write -> await -> consume) is serialised, so
at most one result is outstanding at a time; the `seq` field is logged as a
sanity check. A TX_RESULT whose payload is not exactly 4 bytes is treated as
malformed and ignored. When TX is enabled, the interface only transmits once the
connect handshake has verified a valid `CADWAIT` **and** `TXMODE=MANAGED` **and**
`TXRESULT=1` (both are global per-band daemon state another client could change);
otherwise TX is inhibited while RX keeps working. A timed-out or cancelled TX
clears the pending slot and forces a reconnect handshake before the next TX.

Packet length is validated before writing a `TX_PACKET` frame. `transmit()`
returns the calculated LoRa airtime in milliseconds (on success) for dispatcher
statistics.

`transmit_wait()` uses the last five recorded LoRaHAM transmissions and returns
a suggested wait time in seconds for the configured `airtime` duty-cycle limit.
Airtime is recorded only for transmissions the daemon confirmed with `OK`.

## Tests

Functional tests use a fake LoRaHAM daemon with Unix sockets:

```bash
python -m unittest tests.test_lorahaminterface -v
```

Covered behavior:

```text
connect sends SET TXMODE=MANAGED + SET TXRESULT=1 and reads CADWAIT
RX_PACKET decoded to (payload, rssi, snr), incl. sentinel and max (255 RF) frame
oversized RX frame dropped without reconnect
TX result by raw wire status: OK(0) counts airtime (also OK+0x04 flag);
  BUSY(1)/CHANNEL_BUSY(2) -> 0; RADIO_NOT_READY(3)/RADIO_ERROR(4) -> 0;
  INVALID_PACKET(5)/INVALID_BAND(6) -> 0; unknown -> 0
concurrent transmit() calls serialise (one pending result at a time)
TX inhibited until a valid CADWAIT is known (enable_tx)
malformed TX_RESULT (not 4 bytes) ignored; timeout returns 0 + reconnect
ERROR frame resolves an in-flight TX as failed
reconnect after daemon restart
```
