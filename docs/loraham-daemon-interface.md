# LoRaHAM daemon interface for meshcore-pi

## Purpose

This document describes the planned LoRaHAM daemon interface for `meshcore-pi`.

The goal is to let `meshcore-pi` use a LoRaHAM Pi HAT through `loraham_daemon`, instead of accessing the radio chips directly over SPI/GPIO.

## Non-goals

This interface is not a MeshCore GUI.

This interface is not a replacement for `loraham_daemon`.

This interface does not try to support Meshtastic at the same time as MeshCore on the same physical radio.

This interface does not initially change LoRaHAM daemon behavior.

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
/tmp/lora868.sock and /tmp/loraconf868.sock
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
data_socket = "/tmp/lora868.sock"
config_socket = "/tmp/loraconf868.sock"
frequency = 869618000
sf = 8
bw = 62500
cr = 8
crc = true
preamble = 8
syncword = "0x12"
ldro = false
txpower = 14
enable_tx = false

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

## Initial implementation plan

The implementation should be added in small steps.

1. Add documentation and example configuration.
2. Add a `LoRaHAMInterface` skeleton.
3. Parse and validate socket paths and radio parameters.
4. Open the daemon sockets without transmitting.
5. Implement RX-only packet forwarding into `rx_q`.
6. Add controlled TX, disabled by default.
7. Add reconnect/error handling.
8. Document smoke tests and operating modes.

## RX path

The interface should read raw packets from the LoRaHAM daemon data socket and put them into the `meshcore-pi` receive queue.

If LoRaHAM metadata such as RSSI and SNR is not available from the data socket, the first version may pass only the packet bytes. Later versions may add metadata if the daemon exposes it.

## TX path

TX must be disabled by default during early development.

TX should only be enabled when the configuration explicitly sets:

```toml
enable_tx = true
```

The TX implementation should validate packet length before writing to the daemon data socket.

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

The following LoRaHAM daemon details must be verified before TX is enabled:

- exact data socket packet boundary behavior
- maximum payload size
- whether RX metadata is available
- expected config socket command format
- daemon reconnect behavior
- safe behavior when another client is already using the same radio
