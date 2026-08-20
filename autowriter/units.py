"""Unit conversions between OOXML measurements and Google Docs dimensions.

OOXML uses a small zoo of units:

* twips (1/1440 inch) for indents, spacing and page geometry
* half-points for font sizes
* eighths of a point for border widths
* EMU (English Metric Units, 914400 per inch) for drawing extents
* 1/50 of a percent for some table widths

The Google Docs API speaks a single ``Dimension`` object with a magnitude and
a unit, and the only unit it accepts is ``PT``.  Everything funnels through
here so the rest of the code never has to think about it.
"""

from __future__ import annotations

from typing import Optional

EMU_PER_INCH = 914400
EMU_PER_POINT = 12700
TWIPS_PER_INCH = 1440
TWIPS_PER_POINT = 20


def twips_to_pt(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) / TWIPS_PER_POINT


def pt_to_twips(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return int(round(float(value) * TWIPS_PER_POINT))


def half_points_to_pt(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) / 2.0


def eighth_points_to_pt(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) / 8.0


def emu_to_pt(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(value) / EMU_PER_POINT


def dimension(magnitude: Optional[float]) -> Optional[dict]:
    """Wrap a point value in the Docs API ``Dimension`` shape."""
    if magnitude is None:
        return None
    return {"magnitude": round(float(magnitude), 4), "unit": "PT"}


def hex_to_rgb_color(value: Optional[str]) -> Optional[dict]:
    """``"1F4E79"`` -> the Docs API ``RgbColor`` shape.

    Returns ``None`` for missing values and for the OOXML sentinels ``auto``
    and ``none``, which mean "inherit / no colour" rather than a real colour.
    """
    if not value:
        return None
    text = value.strip().lstrip("#")
    if text.lower() in ("auto", "none"):
        return None
    if len(text) != 6:
        return None
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError:
        return None
    return {
        "red": round(red / 255.0, 6),
        "green": round(green / 255.0, 6),
        "blue": round(blue / 255.0, 6),
    }


def optional_color(value: Optional[str]) -> Optional[dict]:
    """``"1F4E79"`` -> the Docs API ``OptionalColor`` shape."""
    rgb = hex_to_rgb_color(value)
    if rgb is None:
        return None
    return {"color": {"rgbColor": rgb}}


def rgb_color_to_hex(color: Optional[dict]) -> Optional[str]:
    """Inverse of :func:`optional_color`, used when reading a doc back."""
    if not color:
        return None
    rgb = color.get("color", {}).get("rgbColor") if "color" in color else color.get("rgbColor")
    if rgb is None and set(color) <= {"red", "green", "blue"}:
        rgb = color
    if rgb is None:
        return None
    parts = []
    for key in ("red", "green", "blue"):
        parts.append(int(round(float(rgb.get(key, 0.0)) * 255)))
    return "%02X%02X%02X" % tuple(parts)


def u16len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units.

    Google Docs indexes documents in UTF-16 code units, so any character
    outside the Basic Multilingual Plane (emoji, rarer CJK, musical symbols)
    counts as two.  Using ``len()`` here would silently corrupt every index
    after the first emoji.
    """
    return len(text.encode("utf-16-le")) // 2
