
# Repeater

import asyncio
from aiotools import current_taskgroup
from binascii import unhexlify, hexlify

from exceptions import *
from clidevice import CLIDevice


import logging

logger = logging.getLogger(__name__)


class Repeater(CLIDevice):
    """
    Mesh for a repeater
    """
    def __init__(self, me, ids, dispatcher, hardware, config):
        super().__init__(me, ids, dispatcher, hardware, config)
        # This is a repeater
        self.repeater = True

        self.internalname = "Repeater"


    # Respond to a trace
    async def rx_trace(self, rx_packet):
        logger.debug("Trace packet")

        # Trace is 4+4+1 bytes (tag, auth, flags) plus a path
        # We only care about the path; the other bits are for the originating client
        # Compare HOPS, not bytes: `tracepath` entries are `1 << (flags & 3)` bytes each,
        # while `path` collects one SNR byte per hop. A byte-to-byte comparison declared a
        # 2/4/8-byte-hash trace finished (or over-long) while it still had hops to walk.
        hops = rx_packet.trace_hops
        done = len(rx_packet.path)
        if done == hops:
            # Have reached the last hop. Repeaters don't originate traces(?), so ignore
            logger.debug("End of trace reached. Not for us.")
        elif done > hops:
            # Packet path (SNR data) is longer than trace path - something is wrong
            raise InvalidMeshcorePacket("Trace data is longer than trace path")
        else:
            currenthop = rx_packet.trace_hop(done)
            logger.debug(f"Current hop is: {currenthop.hex() if currenthop else None}")

            if currenthop == self.me.path_hash(rx_packet.trace_hash_size):
                # Current hop matches my pubkey hash, so this is (probably) for me
                # Add the current packet SNR to the path.
                #
                # BOUNDED: the path length field carries a 6-bit COUNT, so a 64th entry is
                # unrepresentable and encoded_pathlen would raise while ENCODING the reply —
                # inside the transmit task, where nothing catches it. Drop the trace instead
                # of building a packet that cannot be serialised.
                if len(rx_packet.path) >= 63:
                    logger.warning("Trace path is full; not appending our SNR")
                    return
                rx_packet.path += bytes([int(rx_packet.snr * 4) & 0xff])
                # And resend the packet
                current_taskgroup.get().create_task(self.transmit_packet(rx_packet))

