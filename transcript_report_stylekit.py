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

# Native color emoji glyphs commonly paint a little beyond a nominal 1em text
# advance. Reserve a small amount of inline breathing room and nudge mixed-run
# emoji left so their ink does not collide with the following bold text.
EMOJI_INLINE_UNITS = 1.12
EMOJI_INLINE_X_OFFSET = -0.055
SYMBOL_INLINE_UNITS = 1.0

# Conservative advances for scripts whose shaped glyphs are often much wider
# than a Latin character count suggests.  These are layout budgets, not font
# metrics: final SVG/PNG rendering still uses the platform's real system font.
SCRIPT_CLUSTER_UNITS = {
    "devanagari": 1.30,
    "bengali": 1.35,
    "gurmukhi": 1.80,
    "gujarati": 1.40,
    "tamil": 2.50,
    "telugu": 2.10,
    "kannada": 2.00,
    "malayalam": 1.80,
    "thai": 1.10,
    "lao": 1.15,
    "myanmar": 2.30,
    "ethiopic": 1.70,
    "khmer": 1.45,
    "hebrew": 0.90,
    "arabic": 1.15,
}

SYSTEM_EMOJI_FONT_FAMILIES = (
    # Prefer each operating system's native color emoji face. The monochrome
    # families at the end keep symbols visible on minimal Linux images.
    "Apple Color Emoji",
    "Segoe UI Emoji",
    "Noto Color Emoji",
    "Noto Emoji",
    "Noto Sans Symbols 2",
    "Symbola",
)

# Keep standalone text-presentation symbols out of adjacent Latin/digit runs.
# resvg may otherwise select one symbol-capable fallback face for the complete
# run; some of those faces give ASCII digits a full-em advance even though the
# wrapping model correctly budgets them as proportional glyphs.
SYSTEM_SYMBOL_FONT_FAMILIES = (
    "Segoe UI Symbol",
    "Noto Sans Symbols 2",
    "Noto Sans Symbols",
    "Apple Symbols",
    "Symbola",
)

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
    # Keep emoji last for legacy whole-line consumers. Transcript SVG text is
    # split into explicit emoji runs so partial symbol coverage in a UI font
    # cannot prevent color-emoji shaping.
    *SYSTEM_EMOJI_FONT_FAMILIES,
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


def sanitize_xml_text(value: Any) -> str:
    """Replace characters forbidden by XML 1.0 before SVG serialization."""

    result = []
    for char in str(value or ""):
        code = ord(char)
        if code in {0x09, 0x0A, 0x0D} or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
            result.append(char)
        else:
            result.append("\uFFFD")
    return "".join(result)


def esc(value: Any) -> str:
    return html.escape(sanitize_xml_text(value), quote=True)


def _css_font_family(families: tuple[str, ...], generic: str) -> str:
    return ", ".join(f"'{family}'" for family in families) + f", {generic}"


def font_family() -> str:
    return _css_font_family(SYSTEM_UI_FONT_FAMILIES, "sans-serif")


def font_family_for_text(text: str) -> str:
    """Return a bold-capable system-font stack for the text's main script."""

    return font_family_for_script(_text_script(text))


def font_family_for_script(script: str) -> str:
    if script == "emoji":
        return _css_font_family(SYSTEM_EMOJI_FONT_FAMILIES, "sans-serif")
    if script == "symbol":
        families = tuple(dict.fromkeys((*SYSTEM_SYMBOL_FONT_FAMILIES, *SYSTEM_UI_FONT_FAMILIES)))
        return _css_font_family(families, "sans-serif")
    preferred = SCRIPT_FONT_FAMILIES.get(script, ())
    families = tuple(dict.fromkeys((*preferred, *SYSTEM_UI_FONT_FAMILIES)))
    return _css_font_family(families, "sans-serif")


def font_class_for_text(text: str) -> str:
    return f"font-{_text_script(text)}"


def font_css_rules(texts: list[str]) -> str:
    scripts = tuple(
        dict.fromkeys(
            script
            for text in texts
            for _run, script in text_runs(text)
        )
    )
    return " ".join(
        f".font-{script} {{ font-family: {font_family_for_script(script)}; }}"
        for script in scripts
    )


def terminal_font_family() -> str:
    return _css_font_family(SYSTEM_MONO_FONT_FAMILIES, "monospace")


def _text_script(text: str) -> str:
    codepoints = [ord(ch) for ch in sanitize_xml_text(text)]
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


def grapheme_clusters(text: str) -> list[str]:
    """Keep emoji modifiers, flags, variation selectors, and ZWJ chains intact."""

    value = sanitize_xml_text(text)
    clusters: list[str] = []
    index = 0
    while index < len(value):
        cluster = value[index]
        first_code = ord(value[index])
        index += 1

        # A national flag is one cluster made from two regional indicators.
        if _is_regional_indicator(first_code) and index < len(value):
            if _is_regional_indicator(ord(value[index])):
                cluster += value[index]
                index += 1

        while index < len(value):
            code = ord(value[index])
            if _is_grapheme_extend(value[index]):
                cluster += value[index]
                index += 1
                continue
            if code == 0x200D and index + 1 < len(value):
                cluster += value[index : index + 2]
                index += 2
                continue
            break
        clusters.append(cluster)
    return clusters


def text_runs(text: str) -> list[tuple[str, str]]:
    """Split one visible line into normal, symbol, and color-emoji font runs."""

    runs: list[tuple[str, str]] = []
    normal = ""
    for cluster in grapheme_clusters(text):
        special_script = (
            "emoji"
            if is_emoji_cluster(cluster)
            else "symbol"
            if is_text_symbol_cluster(cluster)
            else ""
        )
        if special_script:
            if normal:
                runs.append((normal, _text_script(normal)))
                normal = ""
            if runs and runs[-1][1] == special_script:
                runs[-1] = (runs[-1][0] + cluster, special_script)
            else:
                runs.append((cluster, special_script))
            continue
        normal += cluster
    if normal:
        runs.append((normal, _text_script(normal)))
    return runs or [("", "default")]


def is_emoji_cluster(cluster: str) -> bool:
    """Return whether a complete grapheme should use a native emoji font."""

    codes = [ord(ch) for ch in str(cluster or "")]
    if not codes or (0xFE0E in codes and 0xFE0F not in codes):
        return False
    if any(
        code == 0xFE0F
        or _is_emoji_modifier(code)
        or _is_regional_indicator(code)
        or 0xE0020 <= code <= 0xE007F
        for code in codes
    ):
        return True
    # ZWJ is also used by some complex writing systems. Only route a ZWJ
    # cluster to an emoji face when the cluster contains an emoji base.
    if 0x200D in codes and any(_is_emoji_codepoint(code) for code in codes):
        return True
    if 0x20E3 in codes:
        return True
    return any(_is_emoji_codepoint(code) for code in codes)


def is_text_symbol_cluster(cluster: str) -> bool:
    """Return whether a text-presentation symbol needs an isolated font run."""

    value = str(cluster or "")
    if not value or is_emoji_cluster(value):
        return False
    return any(
        0x2000 <= ord(ch) <= 0x2BFF and unicodedata.category(ch) == "So"
        for ch in value
    )


def _is_grapheme_extend(ch: str) -> bool:
    code = ord(ch)
    return (
        code in {0x200C, 0xFE0E, 0xFE0F, 0x20E3}
        or _is_emoji_modifier(code)
        or 0xE0020 <= code <= 0xE007F
        or unicodedata.category(ch) in {"Mn", "Mc", "Me"}
    )


def _is_emoji_modifier(code: int) -> bool:
    return 0x1F3FB <= code <= 0x1F3FF


def _is_regional_indicator(code: int) -> bool:
    return 0x1F1E6 <= code <= 0x1F1FF


def _is_emoji_codepoint(code: int) -> bool:
    if 0x1F000 <= code <= 0x1FAFF:
        return True
    return (
        code in {
            0x231A,
            0x231B,
            0x23F0,
            0x23F3,
            0x2614,
            0x2615,
            0x267F,
            0x2693,
            0x26A1,
            0x26AA,
            0x26AB,
            0x26BD,
            0x26BE,
            0x26C4,
            0x26C5,
            0x26CE,
            0x26D4,
            0x26EA,
            0x26F2,
            0x26F3,
            0x26F5,
            0x26FA,
            0x26FD,
            0x2705,
            0x270A,
            0x270B,
            0x2728,
            0x274C,
            0x274E,
            0x2757,
            0x27B0,
            0x27BF,
            0x2B1B,
            0x2B1C,
            0x2B50,
            0x2B55,
        }
        or 0x23E9 <= code <= 0x23EC
        or 0x25FD <= code <= 0x25FE
        or 0x2648 <= code <= 0x2653
        or 0x2753 <= code <= 0x2755
        or 0x2795 <= code <= 0x2797
    )


def wrap_text(text: str, max_units: float) -> list[str]:
    lines: list[str] = []
    for paragraph in sanitize_xml_text(text).splitlines() or [""]:
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
                for cluster in grapheme_clusters(token):
                    units = cluster_units(cluster)
                    if current and current_units + units > max_units:
                        lines.append(current.rstrip())
                        current = ""
                        current_units = 0.0
                    current += cluster
                    current_units += units
            else:
                current += token
                current_units += token_units
        if current or not lines:
            lines.append(current.rstrip())
    return lines


def wrap_terminal_text(text: str, max_cols: int) -> list[str]:
    lines: list[str] = []
    for paragraph in sanitize_xml_text(text).splitlines() or [""]:
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
                for cluster in grapheme_clusters(token):
                    cols = cluster_cols(cluster)
                    if current and current_cols + cols > max_cols:
                        lines.append(current.rstrip())
                        current = ""
                        current_cols = 0
                    current += cluster
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

    for cluster in grapheme_clusters(paragraph):
        if cluster.isspace():
            flush()
            tokens.append(" ")
        elif is_emoji_cluster(cluster) or any(
            unicodedata.east_asian_width(ch) in {"W", "F"} for ch in cluster
        ):
            flush()
            tokens.append(cluster)
        else:
            current += cluster
    flush()
    return tokens


def display_cols(text: str) -> int:
    return sum(cluster_cols(cluster) for cluster in grapheme_clusters(text))


def cluster_cols(cluster: str) -> int:
    if is_emoji_cluster(cluster):
        return 2
    return sum(char_cols(ch) for ch in cluster)


def char_cols(ch: str) -> int:
    if ch == "\n" or _is_nonspacing(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def clip_display(text: str, max_cols: int) -> str:
    value = sanitize_xml_text(text)
    if display_cols(value) <= max_cols:
        return value
    suffix = "..."
    target = max(0, max_cols - len(suffix))
    kept = ""
    used = 0
    for cluster in grapheme_clusters(value):
        cols = cluster_cols(cluster)
        if used + cols > target:
            break
        kept += cluster
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
    if ch in {"W", "M"}:
        return 0.95
    if ch in {"w", "m"}:
        return 0.82
    if ch.isupper():
        return 0.72
    if ch.islower():
        return 0.64
    if ch.isdigit():
        return 0.62
    if unicodedata.category(ch).startswith("P"):
        return 0.62
    return 0.64


def _is_nonspacing(ch: str) -> bool:
    return ch in {"\u200c", "\u200d", "\ufe0e", "\ufe0f"} or unicodedata.category(ch) in {
        "Mn",
        "Mc",
        "Me",
    }


def text_units(text: str) -> float:
    return sum(cluster_units(cluster) for cluster in grapheme_clusters(text))


def cluster_units(cluster: str) -> float:
    if is_emoji_cluster(cluster):
        return EMOJI_INLINE_UNITS
    if is_text_symbol_cluster(cluster):
        return SYMBOL_INLINE_UNITS
    codes = [ord(ch) for ch in cluster]
    # U+FDFD is a compatibility ligature whose rendered ink can approach ten
    # em in common Arabic fallback fonts despite being one Unicode scalar.
    if 0xFDFD in codes:
        return 10.0
    base_units = sum(char_units(ch) for ch in cluster)
    script = _text_script(cluster)
    if script == "arabic" and any(0xFB50 <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF for code in codes):
        return max(base_units, 2.8)
    return max(base_units, SCRIPT_CLUSTER_UNITS.get(script, 0.0))


def ellipsize_text(text: str, max_units: float, *, suffix: str = "...") -> str:
    value = sanitize_xml_text(text)
    if text_units(value) <= max_units:
        return value
    allowed = max(0.0, max_units - text_units(suffix))
    kept = ""
    used = 0.0
    for cluster in grapheme_clusters(value):
        units = cluster_units(cluster)
        if used + units > allowed:
            break
        kept += cluster
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
