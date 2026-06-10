import asyncio
from pathlib import Path

FRAMED_DATA_TYPE_RX_PACKET = 0x01
FRAMED_DATA_TYPE_TX_PACKET = 0x02


class FakeLoRaHAMDaemon:
    def __init__(self, root, *, tx=False, cad=False, respond_to_status=True):
        self.root = Path(root)
        self.data_socket = self.root / "lora868f.sock"
        self.config_socket = self.root / "loraconf868.sock"

        self.tx = tx
        self.cad = cad
        self.respond_to_status = respond_to_status

        self.data_server = None
        self.config_server = None
        self.data_writer = None
        self.config_writer = None

        self.config_commands = []
        self.tx_packets = []
        self._tx_condition = asyncio.Condition()
        self._data_connected = asyncio.Event()
        self._config_connected = asyncio.Event()

    async def start(self):
        for path in (self.data_socket, self.config_socket):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        self.data_server = await asyncio.start_unix_server(
            self._handle_data,
            path=str(self.data_socket),
        )
        self.config_server = await asyncio.start_unix_server(
            self._handle_config,
            path=str(self.config_socket),
        )

    async def close(self):
        for writer in (self.data_writer, self.config_writer):
            if writer is None:
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        for server in (self.data_server, self.config_server):
            if server is None:
                continue
            server.close()
            await server.wait_closed()

        for path in (self.data_socket, self.config_socket):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _status_line(self):
        return (
            f"STATUS RADIO=READY TX={1 if self.tx else 0} "
            f"CAD={1 if self.cad else 0} GETRSSI=0\n"
        )

    async def _send_config_line(self, line):
        if self.config_writer is None:
            return
        self.config_writer.write(line.encode("ascii"))
        await self.config_writer.drain()

    async def set_status(self, *, tx=None, cad=None):
        if tx is not None:
            self.tx = tx
            await self._send_config_line(f"TX={1 if tx else 0}\n")
        if cad is not None:
            self.cad = cad
            await self._send_config_line(f"CAD={1 if cad else 0}\n")

    async def send_rx(self, payload):
        if self.data_writer is None:
            raise RuntimeError("data socket is not connected")
        self.data_writer.write(self._frame(FRAMED_DATA_TYPE_RX_PACKET, payload))
        await self.data_writer.drain()

    async def wait_tx(self, payload, timeout=1.0):
        async with self._tx_condition:
            await asyncio.wait_for(
                self._tx_condition.wait_for(lambda: payload in self.tx_packets),
                timeout=timeout,
            )

    async def wait_data_connection(self, timeout=1.0):
        await asyncio.wait_for(self._data_connected.wait(), timeout=timeout)

    async def wait_config_connection(self, timeout=1.0):
        await asyncio.wait_for(self._config_connected.wait(), timeout=timeout)

    async def _handle_config(self, reader, writer):
        self.config_writer = writer
        self._config_connected.set()
        buffer = bytearray()

        try:
            while True:
                data = await reader.read(256)
                if not data:
                    break

                buffer.extend(data)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    command = line.decode("ascii", errors="replace").rstrip("\r")
                    self.config_commands.append(command)

                    if command == "GET STATUS" and self.respond_to_status:
                        await self._send_config_line(self._status_line())
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_data(self, reader, writer):
        self.data_writer = writer
        self._data_connected.set()

        try:
            while True:
                header = await reader.readexactly(3)
                frame_type = header[0]
                payload_len = header[1] | (header[2] << 8)
                payload = await reader.readexactly(payload_len)

                if frame_type == FRAMED_DATA_TYPE_TX_PACKET:
                    async with self._tx_condition:
                        self.tx_packets.append(payload)
                        self._tx_condition.notify_all()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _frame(frame_type, payload):
        payload = bytes(payload)
        return bytes([
            frame_type,
            len(payload) & 0xff,
            (len(payload) >> 8) & 0xff,
        ]) + payload
