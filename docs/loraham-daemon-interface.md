# LoRaHAM daemon interface for meshcore-pi

## Purpose

This interface lets `meshcore-pi` use a LoRaHAM Pi HAT through
`loraham_daemon`, instead of accessing the radio chips directly over SPI/GPIO.

## Requirements

The LoRaHAM daemon must expose:

```text
/tmp/lora868f.sock      framed data socket
/tmp/loraconf868.sock   CONF/status socket
```

Required CONF commands/status lines:

```text
GET STATUS
STATUS RADIO=READY TX=0|1 CAD=0|1 GETRSSI=0|1
TX=0|1
CAD=0|1
```

The daemon should push `TX` and `CAD` transitions. When cached state is busy,
the interface also sends `GET STATUS` before waiting.

The unframed raw socket is not used.

## Radio ownership

Only one stack should control one physical LoRa radio at a time.

Do not run `meshtasticd`, LoRaHAM APRS/chat/KISS clients, and `meshcore-pi`
against the same LoRaHAM radio at the same time.

## Example configuration

See `examples/config-loraham868.toml`.

Minimal interface block:

```toml
[interface.loraham868]
type = "loraham"
data_socket = "/tmp/lora868f.sock"
config_socket = "/tmp/loraconf868.sock"
preset = "eu_uk_long"
enable_tx = true
```

Useful TX/status options:

```toml
status_wait_timeout = 1.0
busy_wait_timeout = 5.0
tx_delay = 0.2
airtime = 10
```

`airtime` is the duty-cycle limit in percent.

## Presets

```text
eu_uk_long    869.525 MHz, BW 250 kHz, SF11, CR5, preamble 16, TX 14 dBm
eu_uk_medium  869.525 MHz, BW 250 kHz, SF10, CR5, preamble 16, TX 14 dBm
eu_uk_narrow  869.618 MHz, BW 62.5 kHz, SF8, CR5, preamble 16, TX 14 dBm
```

Explicit values such as `frequency`, `bw`, `sf`, `cr`, `preamble`, `ldro`,
or `txpower` may override preset fields.

## RX path

The interface reads `RX_PACKET` frames from the framed data socket and puts
the packet payload into the `meshcore-pi` receive queue.

RSSI/SNR metadata is not exposed by this first version.

## TX path

TX is enabled by default and can be disabled with:

```toml
enable_tx = false
```

Before TX, the interface tracks daemon `TX` and `CAD` state:

```text
if TX=0 and CAD=0:
  send immediately

if TX=1 or CAD=1:
  wait until TX=0 and CAD=0
  then wait tx_delay
  send if still clear

if timeout and TX=1:
  log warning, do not send

if timeout and TX=0 but CAD=1:
  log warning, send anyway

if status is unavailable after GET STATUS:
  log warning, do not send
```

Packet length is validated before writing a `TX_PACKET` frame. `transmit()`
returns calculated LoRa airtime in milliseconds for dispatcher statistics.

`transmit_wait()` uses the last five recorded LoRaHAM transmissions and returns
a suggested wait time in seconds for the configured `airtime` duty-cycle limit.

## Tests

Functional tests use a fake LoRaHAM daemon with Unix sockets:

```bash
python -m unittest tests.test_lorahaminterface -v
```

Covered behavior:

```text
GET STATUS handling
TX/CAD status tracking
immediate TX when clear
tx_delay after busy becomes clear
TX-busy timeout blocks TX
CAD-busy timeout sends anyway
RX_PACKET forwarding
```

## Local smoke tests

Tested locally with:

```text
Raspberry Pi 5 + LoRaHAM Pi HAT
LoRaHAM daemon framed sockets
meshcore-pi companion TCP on 127.0.0.1:5000
MeshCore Node Manager send/receive
```

Broader peer/preset interoperability testing is still pending.
