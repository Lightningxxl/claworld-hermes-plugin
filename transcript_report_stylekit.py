"""Rendering helpers shared by transcript report styles."""

from __future__ import annotations

import html
import os
import struct
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


RESVG_REQUIREMENT = "resvg_py>=0.3.3,<0.5"

# One ordered system-font policy is shared by the SVG source and every PNG
# render. Families with dependable semibold/bold faces come first. Missing
# families are skipped by resvg's system-font database, so this same list works
# on macOS, Windows, and common Linux distributions without bundling fonts.
SYSTEM_UI_FONT_FAMILIES = (
    # macOS and Windows CJK UI fonts; PingFang keeps the preferred macOS look.
    "PingFang SC",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    # Linux CJK families commonly shipped by Fedora, Ubuntu, and derivatives.
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "Hiragino Sans GB",
    # Japanese, Korean, and Traditional Chinese system fallbacks.
    "Yu Gothic UI",
    "Yu Gothic",
    "Meiryo",
    "Hiragino Kaku Gothic ProN",
    "Apple SD Gothic Neo",
    "Malgun Gothic",
    "Noto Sans CJK JP",
    "Noto Sans CJK KR",
    "Noto Sans CJK TC",
    "Noto Sans CJK HK",
    # Broad Latin, Greek, and Cyrillic UI coverage.
    "Noto Sans",
    "Segoe UI",
    "SF Pro Text",
    "Arial",
    # Script-specific Noto families are used when the broad fonts lack glyphs.
    "Noto Sans Arabic",
    "Noto Sans Hebrew",
    "Noto Sans Devanagari",
    "Noto Sans Bengali",
    "Noto Sans Gurmukhi",
    "Noto Sans Gujarati",
    "Noto Sans Tamil",
    "Noto Sans Telugu",
    "Noto Sans Kannada",
    "Noto Sans Malayalam",
    "Noto Sans Thai",
    "Noto Sans Lao",
    "Noto Sans Khmer",
    "Noto Sans Myanmar",
    "Noto Sans Ethiopic",
    # Keep emoji last so it only supplies glyphs unavailable above.
    "Apple Color Emoji",
    "Segoe UI Emoji",
    "Noto Color Emoji",
)

SYSTEM_MONO_FONT_FAMILIES = (
    "SF Mono",
    "Cascadia Mono",
    "JetBrains Mono",
    "Fira Code",
    "Menlo",
    "Monaco",
    "Noto Sans Mono CJK SC",
    "Sarasa Mono SC",
    "Microsoft YaHei UI",
    "PingFang SC",
)

SCRIPT_FONT_FAMILIES = {
    "arabic": (
        "Noto Sans Arabic",
        "Geeza Pro",
        "Segoe UI",
        "Tahoma",
        "Arial",
    ),
    "hebrew": (
        "Noto Sans Hebrew",
        "Arial Hebrew",
        "Segoe UI",
        "Arial",
    ),
    "devanagari": (
        "Noto Sans Devanagari",
        "Kohinoor Devanagari",
        "Nirmala UI",
        "Mangal",
    ),
    "bengali": ("Noto Sans Bengali", "Kohinoor Bangla", "Nirmala UI", "Vrinda"),
    "gurmukhi": ("Noto Sans Gurmukhi", "Kohinoor Gurmukhi", "Nirmala UI", "Raavi"),
    "gujarati": ("Noto Sans Gujarati", "Nirmala UI", "Shruti"),
    "tamil": ("Noto Sans Tamil", "Tamil Sangam MN", "Nirmala UI", "Latha"),
    "telugu": ("Noto Sans Telugu", "Kohinoor Telugu", "Nirmala UI", "Gautami"),
    "kannada": ("Noto Sans Kannada", "Nirmala UI", "Tunga"),
    "malayalam": ("Noto Sans Malayalam", "Nirmala UI", "Kartika"),
    "thai": ("Noto Sans Thai", "Thonburi", "Leelawadee UI", "Tahoma"),
    "lao": ("Noto Sans Lao", "Lao Sangam MN", "Leelawadee UI", "DokChampa"),
    "khmer": ("Noto Sans Khmer", "Khmer Sangam MN", "Leelawadee UI", "DaunPenh"),
    "myanmar": ("Noto Sans Myanmar", "Myanmar Sangam MN", "Myanmar Text"),
    "ethiopic": ("Noto Sans Ethiopic", "Kefa", "Nyala"),
    "japanese": (
        "Hiragino Kaku Gothic ProN",
        "Yu Gothic UI",
        "Yu Gothic",
        "Meiryo",
        "Noto Sans CJK JP",
    ),
    "korean": (
        "Apple SD Gothic Neo",
        "Malgun Gothic",
        "Noto Sans CJK KR",
    ),
    "cjk": (
        "PingFang SC",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "Source Han Sans SC",
        "Hiragino Sans GB",
    ),
}


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _css_font_family(families: tuple[str, ...], generic: str) -> str:
    return ", ".join(f"'{family}'" for family in families) + f", {generic}"


def font_family() -> str:
    return _css_font_family(SYSTEM_UI_FONT_FAMILIES, "sans-serif")


def font_family_for_text(text: str) -> str:
    """Return a bold-capable system-font stack for the text's main script."""

    return font_family_for_script(_text_script(text))


def font_family_for_script(script: str) -> str:
    preferred = SCRIPT_FONT_FAMILIES.get(script, ())
    families = tuple(dict.fromkeys((*preferred, *SYSTEM_UI_FONT_FAMILIES)))
    return _css_font_family(families, "sans-serif")


def font_class_for_text(text: str) -> str:
    return f"font-{_text_script(text)}"


def font_css_rules(texts: list[str]) -> str:
    scripts = tuple(dict.fromkeys(_text_script(text) for text in texts))
    return " ".join(
        f".font-{script} {{ font-family: {font_family_for_script(script)}; }}"
        for script in scripts
    )


def terminal_font_family() -> str:
    return _css_font_family(SYSTEM_MONO_FONT_FAMILIES, "monospace")


def _text_script(text: str) -> str:
    codepoints = [ord(ch) for ch in str(text or "")]
    checks = (
        ("devanagari", ((0x0900, 0x097F), (0xA8E0, 0xA8FF))),
        ("bengali", ((0x0980, 0x09FF),)),
        ("gurmukhi", ((0x0A00, 0x0A7F),)),
        ("gujarati", ((0x0A80, 0x0AFF),)),
        ("tamil", ((0x0B80, 0x0BFF),)),
        ("telugu", ((0x0C00, 0x0C7F),)),
        ("kannada", ((0x0C80, 0x0CFF),)),
        ("malayalam", ((0x0D00, 0x0D7F),)),
        ("thai", ((0x0E00, 0x0E7F),)),
        ("lao", ((0x0E80, 0x0EFF),)),
        ("myanmar", ((0x1000, 0x109F), (0xAA60, 0xAA7F), (0xA9E0, 0xA9FF))),
        ("ethiopic", ((0x1200, 0x137F), (0x1380, 0x139F), (0x2D80, 0x2DDF))),
        ("khmer", ((0x1780, 0x17FF), (0x19E0, 0x19FF))),
        ("hebrew", ((0x0590, 0x05FF),)),
        (
            "arabic",
            (
                (0x0600, 0x06FF),
                (0x0750, 0x077F),
                (0x08A0, 0x08FF),
                (0xFB50, 0xFDFF),
                (0xFE70, 0xFEFF),
            ),
        ),
        ("japanese", ((0x3040, 0x30FF), (0x31F0, 0x31FF))),
        ("korean", ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF))),
        ("cjk", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    )
    counts = {
        script: sum(
            1
            for code in codepoints
            if any(start <= code <= end for start, end in ranges)
        )
        for script, ranges in checks
    }
    # Kana and Hangul establish the correct regional Han glyph style even when
    # the same line contains more ideographs than kana/syllables.
    if counts["japanese"]:
        return "japanese"
    if counts["korean"]:
        return "korean"
    script, count = max(counts.items(), key=lambda item: item[1])
    return script if count else "default"


def wrap_text(text: str, max_units: float) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        current_units = 0.0
        for token in wrap_tokens(paragraph):
            if token.isspace():
                if current and current_units + text_units(token) <= max_units:
                    current += token
                    current_units += text_units(token)
                continue
            token_units = text_units(token)
            if current and current_units + token_units > max_units:
                lines.append(current.rstrip())
                current = ""
                current_units = 0.0
            if token_units > max_units:
                for ch in token:
                    units = char_units(ch)
                    if current and current_units + units > max_units:
                        lines.append(current.rstrip())
                        current = ""
                        current_units = 0.0
                    current += ch
                    current_units += units
            else:
                current += token
                current_units += token_units
        if current or not lines:
            lines.append(current.rstrip())
    return lines


def wrap_terminal_text(text: str, max_cols: int) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        current_cols = 0
        for token in wrap_tokens(paragraph):
            if token.isspace():
                if current and current_cols + 1 <= max_cols:
                    current += " "
                    current_cols += 1
                continue
            token_cols = display_cols(token)
            if current and current_cols + token_cols > max_cols:
                lines.append(current.rstrip())
                current = ""
                current_cols = 0
            if token_cols > max_cols:
                for ch in token:
                    cols = char_cols(ch)
                    if current and current_cols + cols > max_cols:
                        lines.append(current.rstrip())
                        current = ""
                        current_cols = 0
                    current += ch
                    current_cols += cols
            else:
                current += token
                current_cols += token_cols
        if current or not lines:
            lines.append(current.rstrip())
    return lines


def wrap_tokens(paragraph: str) -> list[str]:
    tokens: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            tokens.append(current)
            current = ""

    for ch in paragraph:
        if ch.isspace():
            flush()
            tokens.append(" ")
        elif unicodedata.east_asian_width(ch) in {"W", "F"}:
            flush()
            tokens.append(ch)
        else:
            current += ch
    flush()
    return tokens


def display_cols(text: str) -> int:
    return sum(char_cols(ch) for ch in str(text or ""))


def char_cols(ch: str) -> int:
    if ch == "\n" or _is_nonspacing(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def clip_display(text: str, max_cols: int) -> str:
    value = str(text or "")
    if display_cols(value) <= max_cols:
        return value
    suffix = "..."
    target = max(0, max_cols - len(suffix))
    kept = ""
    used = 0
    for ch in value:
        cols = char_cols(ch)
        if used + cols > target:
            break
        kept += ch
        used += cols
    return kept.rstrip() + suffix


def pad_display(text: str, cols: int) -> str:
    value = clip_display(text, cols)
    return value + " " * max(0, cols - display_cols(value))


def char_units(ch: str) -> float:
    if ch == "\n" or _is_nonspacing(ch):
        return 0.0
    if ch.isspace():
        return 0.35
    if unicodedata.east_asian_width(ch) in {"W", "F"}:
        return 1.0
    return 0.55


def _is_nonspacing(ch: str) -> bool:
    return ch in {"\u200c", "\u200d", "\ufe0e", "\ufe0f"} or unicodedata.category(ch) in {
        "Mn",
        "Mc",
        "Me",
    }


def text_units(text: str) -> float:
    return sum(char_units(ch) for ch in text)


def ellipsize_text(text: str, max_units: float, *, suffix: str = "...") -> str:
    value = str(text or "")
    if text_units(value) <= max_units:
        return value
    allowed = max(0.0, max_units - text_units(suffix))
    kept = ""
    used = 0.0
    for ch in value:
        units = char_units(ch)
        if used + units > allowed:
            break
        kept += ch
        used += units
    return kept.rstrip() + suffix


def write_png_from_svg(svg_path: Path, png_path: Path, *, width: int, height: int) -> dict:
    """Rasterize the canonical SVG with resvg and no visual fallback."""

    try:
        import resvg_py  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PNG export requires the resvg renderer. Install it in the Hermes "
            f"Python environment with: python -m pip install '{RESVG_REQUIREMENT}'"
        ) from exc

    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        png_bytes = resvg_py.svg_to_bytes(
            svg_path=str(svg_path),
            width=width,
            height=height,
            resources_dir=str(svg_path.parent),
            skip_system_fonts=False,
            shape_rendering="geometric_precision",
            text_rendering="optimize_legibility",
            image_rendering="optimize_quality",
        )
    except Exception as exc:
        raise ValueError(f"resvg PNG export failed for {svg_path.name}: {exc}") from exc

    _validate_png(png_bytes, width=width, height=height)
    _atomic_write_bytes(png_path, png_bytes)
    return {
        "renderer": "resvg",
        "binding": "resvg_py",
        "bindingVersion": str(getattr(resvg_py, "__version__", "unknown")),
        "engineVersion": str(getattr(resvg_py, "__resvg_version__", "unknown")),
        "fontPolicy": "system-font-priority",
        "fontStrategy": "unicode-script-aware",
    }


def _validate_png(payload: bytes, *, width: int, height: int) -> None:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError("resvg returned invalid PNG data")
    actual_width, actual_height = struct.unpack(">II", payload[16:24])
    if (actual_width, actual_height) != (width, height):
        raise ValueError(
            "resvg returned unexpected PNG dimensions: "
            f"{actual_width}x{actual_height}; expected {width}x{height}"
        )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
