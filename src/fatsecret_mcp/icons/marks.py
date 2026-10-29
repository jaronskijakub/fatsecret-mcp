"""The files of the connector's mark (fs.) and the author's mark (jj.), shipped in this package."""

from __future__ import annotations

from functools import cache
from importlib.resources import files

_FILES = files("fatsecret_mcp.icons") / "files"


@cache
def read_file(name: str) -> bytes:
    """One of the shipped files, read once: they never change while the server runs."""
    return (_FILES / name).read_bytes()


def mark_svg(name: str) -> str:
    """A mark as SVG markup, ready to put inside a page."""
    return read_file(name).decode()
