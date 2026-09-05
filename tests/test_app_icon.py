"""The application mark: what the shell declares, and what those files are.

Trellmark owns no edge configuration, so these tests stop at the repository
boundary: the assets exist, they are the formats their declarations promise,
and the SVG pulls nothing in from outside itself.
"""

import re
import struct
from xml.etree import ElementTree

from trellmark import config

PLATE_COLOR = "#8a423b"  # --red
MARK_COLOR = "#f9f6f2"  # --n-00

DECLARED_ICONS = {
    '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">': "favicon.svg",
    '<link rel="icon" href="/static/favicon.ico" sizes="48x48 32x32 16x16">': (
        "favicon.ico"
    ),
    '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">': (
        "apple-touch-icon.png"
    ),
}


def _png_header(data):
    """(width, height, bit depth, color type) from the IHDR that must lead."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR", "IHDR is not the first chunk"
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, depth, color_type


def _ico_frames(data):
    """The (width, height) of every image packed into an ICO directory."""
    reserved, resource_type, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0, "ICO header is not an ICO header"
    assert resource_type == 1, "resource is a cursor, not an icon"
    frames = []
    for index in range(count):
        entry = data[6 + index * 16 : 6 + (index + 1) * 16]
        # A zero byte means 256 in the ICO directory, which is its only escape.
        frames.append((entry[0] or 256, entry[1] or 256))
    return frames


def test_the_app_shell_declares_every_icon_it_ships():
    shell = config.INDEX_FILE.read_text(encoding="utf-8")

    for declaration, filename in DECLARED_ICONS.items():
        assert declaration in shell, declaration
        assert (config.STATIC_DIR / filename).is_file(), filename

    head = shell.partition("</head>")[0]
    assert head.index('rel="icon"') < head.index('<link rel="stylesheet"'), (
        "the icon links belong ahead of the stylesheet"
    )


def test_the_svg_icon_is_well_formed_and_self_contained():
    icon = (config.STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")

    # Parsing is half the assertion: a stray "--" inside an XML comment, or any
    # other malformation, makes the file unparseable and browsers drop it
    # silently rather than reporting an error.
    root = ElementTree.fromstring(icon)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox") == "0 0 32 32"
    # Without an intrinsic size some consumers refuse to rasterise the file.
    assert root.get("width") == "32"
    assert root.get("height") == "32"

    forbidden = ("http://", "https://", "//", "url(", "@import", "xlink:", "data:")
    for needle in forbidden:
        if needle == "//":
            # The namespace declaration is the one legitimate "//" in the file.
            assert icon.count(needle) == 1, needle
            continue
        assert needle not in icon.replace('xmlns="http://www.w3.org/2000/svg"', ""), (
            needle
        )

    for tag in ("image", "script", "use", "foreignObject", "style"):
        assert root.find(f".//{{http://www.w3.org/2000/svg}}{tag}") is None, tag


def test_the_svg_icon_paints_only_with_palette_tokens():
    icon = (config.STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")

    colors = set(re.findall(r"#[0-9a-fA-F]{3,8}", icon))
    assert colors == {PLATE_COLOR, MARK_COLOR}


def test_the_ico_carries_the_frames_browsers_ask_for():
    data = (config.STATIC_DIR / "favicon.ico").read_bytes()

    assert _ico_frames(data) == [(16, 16), (32, 32), (48, 48)]


def test_the_touch_icon_is_an_opaque_180_square():
    data = (config.STATIC_DIR / "apple-touch-icon.png").read_bytes()
    width, height, depth, color_type = _png_header(data)

    assert (width, height) == (180, 180)
    assert depth == 8
    # Color types 4 and 6 carry an alpha channel; a palette type carries one
    # through a tRNS chunk. iOS composites transparency against an unknown
    # ground, so the file must have none of either.
    assert color_type in (0, 2, 3), f"color type {color_type} has an alpha channel"
    assert b"tRNS" not in data
