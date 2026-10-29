"""The connector's icons and website, as the apps read them from the server."""

from __future__ import annotations

import struct
from collections.abc import Callable

import pytest
from mcp import Client

from fatsecret_mcp.icons.links import PUBLIC_FILES, WEBSITE_URL, server_icons
from fatsecret_mcp.icons.marks import read_file

pytestmark = pytest.mark.anyio

BASE_URL = "https://diary.example.com"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> str:
    """The width and height a PNG declares in its header, as an MCP icon's `sizes` writes them."""
    width, height = struct.unpack(">II", data[16:24])

    return f"{width}x{height}"


def test_each_icon_is_a_png_on_the_servers_own_origin_of_the_size_it_claims() -> None:
    icons = server_icons(BASE_URL)

    assert len(icons) == 2

    for icon in icons:
        name = icon.src.removeprefix(BASE_URL + "/")
        data = read_file(name)

        assert icon.src.startswith(BASE_URL + "/")
        assert name in PUBLIC_FILES
        assert icon.mime_type == "image/png"
        assert data.startswith(PNG_SIGNATURE)
        assert icon.sizes == [png_size(data)]


def test_the_favicon_is_served_as_an_icon() -> None:
    assert PUBLIC_FILES["favicon.ico"] == "image/x-icon"


async def test_a_local_server_names_its_website_but_no_icons(connect: Callable[[str], Client]) -> None:
    async with connect("jakub") as client:
        info = client.server_info

    assert info is not None
    assert info.website_url == WEBSITE_URL
    assert info.icons is None
