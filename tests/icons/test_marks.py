"""The marks shipped in the package, as the server serves them and puts them in its sign-in page."""

from __future__ import annotations

import pytest

from fatsecret_mcp.icons.marks import mark_svg, read_file

ICO_HEADER = b"\x00\x00\x01\x00"


def test_the_favicon_is_an_icon_file() -> None:
    assert read_file("favicon.ico").startswith(ICO_HEADER)


@pytest.mark.parametrize("name", ["fs.svg", "jj.svg"])
def test_the_marks_are_plain_svg_safe_to_put_inside_a_page(name: str) -> None:
    mark = mark_svg(name)

    assert mark.startswith("<svg ")
    assert 'viewBox="0 0 64 64"' in mark
    assert "<script" not in mark
    assert "href" not in mark
    assert "style" not in mark
