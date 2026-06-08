# LoRaHAM daemon interface for meshcore-pi

## Purpose

This document describes the LoRaHAM daemon interface for `meshcore-pi`.

The goal is to let `meshcore-pi` use a LoRaHAM Pi HAT through `loraham_daemon`, instead of accessing the radio chips directly over SPI/GPIO.

## Non-goals

This interface is not a MeshCore GUI.

This interface is not a replacement for `loraham_daemon`.

This interface does not try to support Meshtastic at the same time as MeshCore on the same physical radio.

This interface expects a LoRaHAM daemon version that exposes framed data sockets.

## Target architecture

```text
MeshCore app, Web app, or GUI
        |
        v
meshcore-pi companion, room, or repeater device
        |
        v
meshcore-pi LoRaHAM daemon interface
        |
        v
/tmp/lora868f.sock and /tmp/loraconf868.sock
        |
        v
loraham_daemon
        |
        v
LoRaHAM Pi HAT radio
```

The first implementation target is the 868 MHz LoRaHAM radio. A 433 MHz configuration should use the same code with different socket paths and radio parameters.

## Radio ownership

Only one software stack should control one physical LoRa radio at a time.

Do not run `meshtasticd`, LoRaHAM APRS/chat/KISS clients, and `meshcore-pi` against the same LoRaHAM radio at the same time.

A safe operating model is to use explicit modes:

```text
Mode 1: Meshtastic
  meshtasticd owns the radio.

Mode 2: LoRaHAM daemon clients
  loraham_daemon plus APRS/chat/KISS tooling owns the radio path.

Mode 3: MeshCore via LoRaHAM daemon
  loraham_daemon runs, and meshcore-pi uses the daemon sockets.
```

## Planned interface name

The planned `meshcore-pi` interface type is:

```toml
type = "loraham"
```

A first 868 MHz configuration may look like this:

```toml
interfaces = ["loraham868"]
devices = ["companion"]

[interface.loraham868]
type = "loraham"
data_socket = "/tmp/lora868f.sock"
config_socket = "/tmp/loraconf868.sock"

# Available presets:
#   eu_uk_long    869.525 MHz, BW 250 kHz, SF11, CR5, preamble 16, TX 14 dBm
#   eu_uk_narrow  869.618 MHz, BW 62.5 kHz, SF8, CR5, TX 14 dBm
preset = "eu_uk_long"

# Explicit values may override preset fields when needed.
enable_tx = true

[device.companion]
type = "companion"
name = "LoRaHAM MeshCore"
contacts = "contacts.mesh"
channels = 32
channelfile = "channels.json"
add_public_channel = true
interface = "wifi"

[device.companion.wifi]
port = 5000
listen = "0.0.0.0"
```

## Implementation status

The implementation has been added in small steps.

1. Add documentation and example configuration.
2. Add a `LoRaHAMInterface` skeleton.
3. Parse and validate socket paths and radio parameters.
4. Open the daemon sockets without transmitting.
5. Implement framed RX packet forwarding into `rx_q`.
6. Add controlled framed TX, enabled explicitly in the example config.
7. Add reconnect/error handling.
8. Document smoke tests and operating modes.

## Presets

The LoRaHAM interface supports named presets for common MeshCore EU/UK radio configurations:

```toml
preset = "eu_uk_long"
```

Available presets:

```text
eu_uk_long    869.525 MHz, BW 250 kHz, SF11, CR5, preamble 16, TX 14 dBm
eu_uk_narrow  869.618 MHz, BW 62.5 kHz, SF8, CR5, TX 14 dBm
```

Explicit radio fields such as `frequency`, `bw`, `sf`, `cr`, `preamble`, `ldro`, or `txpower` can still be set in the config and override the selected preset.

## Framed daemon sockets

The LoRaHAM interface uses the framed data socket variant of the LoRaHAM daemon protocol:

```text
/tmp/lora868f.sock
/tmp/loraconf868.sock
```

The framed data socket preserves packet boundaries for MeshCore packets. The unframed raw socket is not used by this interface.

## Local test status

The current development branch has been locally tested with:

```text
LoRaHAM daemon framed sockets
meshcore-pi companion TCP endpoint on 127.0.0.1:5000
meshcore-cli public channel TX
MeshCore Node Manager local GUI channel TX
```

The LoRaHAM daemon confirmed framed TX reception and RF transmit during the local tests.

## RX path

The interface reads framed RX_PACKET frames from the LoRaHAM daemon data socket and puts the packet payload into the `meshcore-pi` receive queue.

If LoRaHAM metadata such as RSSI and SNR is not available from the data socket, the first version may pass only the packet bytes. Later versions may add metadata if the daemon exposes it.

## TX path

TX is enabled by default for the LoRaHAM interface so that the example works without an extra TX option.

TX can be disabled explicitly with:

```toml
enable_tx = false
```

The TX implementation validates packet length before writing to the daemon data socket.

## UI and app paths

The radio interface should stay independent of the user interface.

The first practical UI test path is:

```text
MeshCore app or Web app
        |
        v
meshcore-pi companion device over WiFi, port 5000
        |
        v
LoRaHAM daemon interface
```

Other UI paths should remain possible later, including serial companion mode, a GUI wrapper, room server mode, and repeater mode.

## Open questions

The following LoRaHAM daemon details still need broader interoperability testing:

- exact data socket packet boundary behavior
- maximum payload size
- whether RX metadata is available
- expected config socket command format
- daemon reconnect behavior
- safe behavior when another client is already using the same radio
