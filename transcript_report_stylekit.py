"""Rendering helpers shared by transcript report styles."""

from __future__ import annotations

import html
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .transcript_report_types import LayoutPage, TranscriptMessage


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def font_family() -> str:
    return (
        "'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'WenQuanYi Zen Hei', "
        "'Noto Sans CJK SC', 'Noto Sans SC', 'Source Han Sans SC', 'IPA P Gothic', "
        "'AR PL UMing CN', 'Arial Unicode MS', -apple-system, BlinkMacSystemFont, "
        "'Segoe UI', Arial, sans-serif"
    )


def terminal_font_family() -> str:
    return (
        "'SF Mono', Menlo, Monaco, 'Cascadia Mono', 'Fira Code', 'JetBrains Mono', "
        "'Noto Sans Mono CJK SC', 'Sarasa Mono SC', 'WenQuanYi Zen Hei Mono', "
        "'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', monospace"
    )


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
    if ch == "\n":
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
    if ch == "\n":
        return 0.0
    if ch.isspace():
        return 0.35
    if unicodedata.east_asian_width(ch) in {"W", "F"}:
        return 1.0
    return 0.55


def text_units(text: str) -> float:
    return sum(char_units(ch) for ch in text)


def ellipsize_text(text: str, max_units: float) -> str:
    value = str(text or "")
    if text_units(value) <= max_units:
        return value
    suffix = "..."
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


def write_png_with_fallback(svg_path: Path, png_path: Path, page: LayoutPage, pillow_renderer: Callable[[LayoutPage, Path], None]) -> dict:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    if page_contains_cjk(page):
        if sys.platform == "darwin" and shutil.which("sips"):
            try:
                subprocess.run(["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return {"renderer": "sips-cjk"}
            except Exception as exc:
                errors.append(f"sips: {exc}")
        try:
            pillow_renderer(page, png_path)
            return {"renderer": "pillow-layout-cjk"}
        except Exception as exc:
            errors.append(f"pillow-cjk: {exc}")

    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))
        return {"renderer": "cairosvg"}
    except Exception as exc:
        errors.append(f"cairosvg: {exc}")

    for command, renderer in (
        (["rsvg-convert", str(svg_path), "-o", str(png_path)], "rsvg-convert"),
        (["resvg", str(svg_path), str(png_path)], "resvg"),
    ):
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return {"renderer": renderer}
        except Exception as exc:
            errors.append(f"{renderer}: {exc}")

    if sys.platform == "darwin" and shutil.which("sips"):
        try:
            subprocess.run(["sips", "-s", "format", "png", str(svg_path), "--out", str(png_path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return {"renderer": "sips"}
        except Exception as exc:
            errors.append(f"sips: {exc}")

    try:
        pillow_renderer(page, png_path)
        return {"renderer": "pillow-layout-fallback"}
    except Exception as exc:
        errors.append(f"pillow: {exc}")
    raise ValueError("PNG export failed: " + "; ".join(errors))


def page_contains_cjk(page: LayoutPage) -> bool:
    chunks = [page.title, page.subtitle, page.footer]
    for item in page.items:
        if item.get("kind") == "ellipsis":
            chunks.append(str(item.get("label") or ""))
            continue
        message = item.get("message")
        if isinstance(message, TranscriptMessage):
            chunks.extend([message.participant_label, message.text, *message.tags])
        chunks.extend(str(line) for line in item.get("lines") or [])
    return any(has_cjk(text) for text in chunks)


def has_cjk(text: str) -> bool:
    for ch in str(text or ""):
        code = ord(ch)
        if (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x2A6DF
            or 0x2A700 <= code <= 0x2B73F
            or 0x2B740 <= code <= 0x2B81F
            or 0x2B820 <= code <= 0x2CEAF
            or 0x3000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF
        ):
            return True
    return False


def rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    r, g, b = hex_to_rgb(color)
    return (r, g, b, alpha)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = str(color or "#000000").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        value = "000000"
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def draw_vertical_gradient(img: Any, top: str, bottom: str) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    width, height = img.size
    top_rgb = hex_to_rgb(top)
    bottom_rgb = hex_to_rgb(bottom)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(top_rgb[idx] + (bottom_rgb[idx] - top_rgb[idx]) * t) for idx in range(3))
        draw.line([(0, y), (width, y)], fill=(*color, 255))


def pil_terminal_font(size: int):
    from PIL import ImageFont

    for candidate in (
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
        "/Library/Fonts/SF-Mono-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansMonoCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansMonoCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    ):
        try:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return pil_font(size)


def pil_font(size: int):
    from PIL import ImageFont

    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/opentype/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/unifont/unifont.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()
