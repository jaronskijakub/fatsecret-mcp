"""Where the apps find the connector's icon and the project's website."""

from __future__ import annotations

from mcp.types import Icon

WEBSITE_URL = "https://github.com/jaronskijakub/fatsecret-mcp"

# Served without sign-in at the server's root: the MCP spec asks apps to fetch icons from the
# server's own origin without credentials, and apps without an icon fall back to the favicon.
# ChatGPT's connector form takes only an uploaded PNG of at most 10 KB, which fs-256.png fits.
PUBLIC_FILES = {
    "fs-512.png": "image/png",
    "fs-256.png": "image/png",
    "fs-64.png": "image/png",
    "favicon.ico": "image/x-icon",
}

_ICON_SIZES = {
    "fs-512.png": "512x512",
    "fs-256.png": "256x256",
    "fs-64.png": "64x64",
}


def server_icons(base_url: str) -> list[Icon]:
    """The connector's icons for `serverInfo`, as absolute URLs on the server's own origin."""
    icons: list[Icon] = []

    for name, size in _ICON_SIZES.items():
        icon = Icon(
            src=f"{base_url}/{name}",
            mime_type=PUBLIC_FILES[name],
            sizes=[size],
        )
        icons.append(icon)

    return icons
