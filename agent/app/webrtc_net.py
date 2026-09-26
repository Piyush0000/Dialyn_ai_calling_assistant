"""WebRTC networking for servers behind a firewall.

aiortc/aioice bind every WebRTC media socket to a random UDP port, which a
firewalled VPS drops. ``limit_udp_ports`` makes those binds use a fixed range
(e.g. 40000-40199) so only that range has to be opened. ``ice_servers`` turns the
STUN/TURN settings into the list both the server and browsers use.
"""

import asyncio
import random

from loguru import logger

from app.config import Settings


def parse_port_range(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    start, _, end = value.partition("-")
    first, last = int(start), int(end or start)
    if not (1024 <= first <= last <= 65535):
        raise ValueError(f"Invalid WEBRTC_UDP_PORTS range: {value!r}")
    return first, last


def limit_udp_ports(loop: asyncio.AbstractEventLoop, first: int, last: int) -> None:
    """Make UDP binds that ask for "any port" (port 0) pick one inside [first, last]."""
    original = loop.create_datagram_endpoint

    async def create_datagram_endpoint(protocol_factory, local_addr=None, **kwargs):
        if not local_addr or local_addr[1] != 0:
            return await original(protocol_factory, local_addr=local_addr, **kwargs)
        ports = list(range(first, last + 1))
        random.shuffle(ports)
        error: OSError | None = None
        for port in ports:
            try:
                return await original(protocol_factory, local_addr=(local_addr[0], port), **kwargs)
            except OSError as e:  # in use; try the next one
                error = e
        logger.error(f"No free UDP port in {first}-{last}; raise WEBRTC_UDP_PORTS")
        raise error or OSError("No free UDP port")

    loop.create_datagram_endpoint = create_datagram_endpoint
    logger.info(f"WebRTC media limited to UDP ports {first}-{last}")


def ice_servers(settings: Settings) -> list[dict]:
    """STUN (free) plus an optional TURN relay, in RTCIceServer format."""
    servers = [{"urls": url.strip()} for url in settings.webrtc_stun_urls.split(",") if url.strip()]
    if settings.webrtc_turn_url:
        servers.append(
            {
                "urls": settings.webrtc_turn_url,
                "username": settings.webrtc_turn_username,
                "credential": settings.webrtc_turn_credential,
            }
        )
    return servers
