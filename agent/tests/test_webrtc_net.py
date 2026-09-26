import asyncio

import pytest

from app.config import Settings
from app.webrtc_net import ice_servers, limit_udp_ports, parse_port_range


async def test_udp_binds_stay_inside_range():
    loop = asyncio.get_running_loop()
    original = loop.create_datagram_endpoint
    limit_udp_ports(loop, 41000, 41002)
    try:
        transports = []
        for _ in range(3):
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
            )
            transports.append(transport)
        ports = sorted(t.get_extra_info("sockname")[1] for t in transports)
        assert ports == [41000, 41001, 41002]
        with pytest.raises(OSError):  # range exhausted
            await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
            )
        for t in transports:
            t.close()
    finally:
        loop.create_datagram_endpoint = original


def test_parse_port_range_and_ice_servers():
    assert parse_port_range("") is None
    assert parse_port_range("40000-40199") == (40000, 40199)
    with pytest.raises(ValueError):
        parse_port_range("80-90")
    settings = Settings(
        webrtc_turn_url="turn:t.example.com:3478",
        webrtc_turn_username="u",
        webrtc_turn_credential="p",
    )
    servers = ice_servers(settings)
    assert servers[0] == {"urls": "stun:stun.l.google.com:19302"}
    assert servers[1] == {"urls": "turn:t.example.com:3478", "username": "u", "credential": "p"}
