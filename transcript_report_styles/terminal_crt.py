"""CRT terminal transcript report style."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import TranscriptReportStyle
from ..transcript_report_stylekit import (
    clip_display,
    display_cols,
    draw_vertical_gradient,
    ellipsize_text,
    esc,
    pad_display,
    page_contains_cjk,
    pil_font,
    pil_terminal_font,
    rgba,
    terminal_font_family,
    text_units,
    wrap_terminal_text,
    write_png_with_fallback,
)
from ..transcript_report_types import LayoutPage, MeasuredBubble, TranscriptMessage


CANVAS_MARGIN = 34
HEADER_HEIGHT = 118
BODY_TOP_GAP = 22
PAGE_BOTTOM = 52
ITEM_GAP = 15
TIME_ROW_HEIGHT = 30
ELLIPSIS_HEIGHT = 32
TERMINAL_FONT_SIZE = 15
TERMINAL_SMALL_FONT_SIZE = 12
TERMINAL_LINE_HEIGHT = 22
TERMINAL_CHAR_WIDTH = 8.9
TERMINAL_BOX_PAD_X = 12
TERMINAL_BOX_PAD_Y = 10
TERMINAL_MIN_COLS = 28
TERMINAL_MAX_COLS = 58

THEME = {
    "bg_top": "#05120B",
    "bg_bottom": "#101006",
    "screen": "#07170E",
    "screen_alt": "#0D1D12",
    "phosphor": "#8CFF9A",
    "phosphor_dim": "#4FAF66",
    "amber": "#FFBF58",
    "amber_dim": "#B9863F",
    "cyan": "#72F5D0",
    "muted": "#6F8F72",
    "muted_amber": "#9C7441",
    "scanline": "#A5FFAA",
    "noise": "#1E3A24",
    "shadow": "#000000",
}

SIDE_STYLES = {
    "left": {
        "box_fill": "#07190F",
        "border": "#77F28A",
        "border_dim": "#3E9B53",
        "text": "#D4FFD4",
        "label": "#92FF9C",
    },
    "right": {
        "box_fill": "#1A1508",
        "border": "#FFBF58",
        "border_dim": "#AA7932",
        "text": "#FFE8BA",
        "label": "#FFD27A",
    },
}

TAG_STYLES = {
    "like": {"text": "#92FF9C"},
    "dislike": {"text": "#FF8B8B"},
    "request end": {"text": "#72F5D0"},
    "default": {"text": "#CDECC8"},
}


def measure_item(item: dict[str, Any], width: int) -> MeasuredBubble:
    if item["kind"] == "ellipsis":
        return MeasuredBubble(kind="ellipsis", message=None, lines=[], width=width - CANVAS_MARGIN * 2, height=ELLIPSIS_HEIGHT, omitted_count=item["omitted"], label=item["label"])
    if item["kind"] == "time":
        return MeasuredBubble(kind="time", message=None, lines=[], width=width - CANVAS_MARGIN * 2, height=TIME_ROW_HEIGHT, label=item["label"])
    message = item["message"]
    max_cols = _terminal_max_cols(width)
    lines = wrap_terminal_text(message.text, max_cols)
    tag_line = _tag_text(message.tags)
    content_cols = max([display_cols(line) for line in lines] + [display_cols(tag_line), display_cols(message.participant_label) + 10, TERMINAL_MIN_COLS])
    cols = max(TERMINAL_MIN_COLS, min(max_cols, content_cols))
    box_line_count = 2 + len(lines) + (1 if tag_line else 0)
    bubble_w = int((cols + 4) * TERMINAL_CHAR_WIDTH + TERMINAL_BOX_PAD_X * 2)
    bubble_h = TERMINAL_BOX_PAD_Y * 2 + box_line_count * TERMINAL_LINE_HEIGHT
    return MeasuredBubble(
        kind="message",
        message=message,
        lines=lines,
        width=bubble_w,
        height=bubble_h,
        meta_height=0,
        tag_height=TERMINAL_LINE_HEIGHT if tag_line else 0,
        text_height=len(lines) * TERMINAL_LINE_HEIGHT,
    )


def paginate(items: list[MeasuredBubble], width: int, max_height: int, title: str, subtitle: str) -> list[LayoutPage]:
    pages: list[list[MeasuredBubble]] = [[]]
    used = HEADER_HEIGHT + BODY_TOP_GAP + PAGE_BOTTOM
    for idx, item in enumerate(items):
        item_h = item.height + ITEM_GAP
        needed_h = item_h
        if item.kind == "time" and idx + 1 < len(items):
            needed_h += items[idx + 1].height + ITEM_GAP
        if pages[-1] and used + needed_h > max_height:
            pages.append([])
            used = HEADER_HEIGHT + BODY_TOP_GAP + PAGE_BOTTOM
        pages[-1].append(item)
        used += item_h
    rendered: list[LayoutPage] = []
    total = len(pages)
    for page_no, page_items in enumerate(pages, start=1):
        y = HEADER_HEIGHT + BODY_TOP_GAP
        layout_items = []
        for item in page_items:
            if item.kind == "ellipsis":
                layout_items.append({"kind": "ellipsis", "y": y, "height": item.height, "label": item.label})
                y += item.height + ITEM_GAP
                continue
            if item.kind == "time":
                layout_items.append({"kind": "time", "y": y, "height": item.height, "label": item.label})
                y += item.height + ITEM_GAP
                continue
            assert item.message is not None
            bubble_x, avatar_x, meta_x, align = _positions(width, item.width, item.message.side)
            layout_items.append(
                {
                    "kind": "message",
                    "y": y,
                    "bubbleX": bubble_x,
                    "avatarX": avatar_x,
                    "metaX": meta_x,
                    "align": align,
                    "width": item.width,
                    "metaHeight": item.meta_height,
                    "bubbleHeight": item.height - item.meta_height,
                    "lines": item.lines,
                    "message": item.message,
                }
            )
            y += item.height + ITEM_GAP
        height = max(420, min(max_height, y + PAGE_BOTTOM))
        footer = f"Page {page_no}/{total} | Claworld local render" if total > 1 else "Claworld local render"
        rendered.append(LayoutPage(page=page_no, width=width, height=height, items=layout_items, title=title, subtitle=subtitle, footer=footer))
    return rendered


def render_svg(page: LayoutPage) -> str:
    title_id = f"claworld-report-title-{page.page}"
    desc_id = f"claworld-report-desc-{page.page}"
    desc = f"{page.title}. {page.subtitle}. {len(page.items)} transcript rows."
    header_lines = _terminal_header_lines(page)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{page.width}" height="{page.height}" viewBox="0 0 {page.width} {page.height}" role="img" aria-labelledby="{title_id} {desc_id}">',
        f'<title id="{title_id}">{esc(page.title)}</title>',
        f'<desc id="{desc_id}">{esc(desc)}</desc>',
        _svg_defs(),
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#crtBg)"/>',
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#terminalNoise)"/>',
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#scanlines)"/>',
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#crtVignette)"/>',
        f'<rect x="20" y="16" width="{page.width - 40}" height="{page.height - 32}" rx="18" fill="none" stroke="#456746" stroke-width="1" stroke-opacity="0.30"/>',
        '<g class="crt-text">',
    ]
    header_y = 34
    for idx, line in enumerate(header_lines):
        color = THEME["phosphor"] if idx == 1 else THEME["phosphor_dim"]
        size = TERMINAL_FONT_SIZE if idx != 2 else TERMINAL_SMALL_FONT_SIZE
        parts.append(f'<text x="{CANVAS_MARGIN}" y="{header_y + idx * 22}" font-size="{size}" fill="{color}">{esc(line)}</text>')
    parts.append("</g>")
    parts.append('<g role="list" class="crt-text">')
    for item in page.items:
        if item["kind"] == "ellipsis":
            label = f"# {item['label']}"
            parts.append(f'<text x="{page.width / 2:.1f}" y="{item["y"] + 20}" text-anchor="middle" font-size="{TERMINAL_SMALL_FONT_SIZE}" fill="{THEME["muted"]}">{esc(label)}</text>')
            continue
        if item["kind"] == "time":
            label = f":: {item['label']} ::"
            parts.append(f'<text x="{page.width / 2:.1f}" y="{item["y"] + 20}" text-anchor="middle" font-size="{TERMINAL_SMALL_FONT_SIZE}" fill="{THEME["muted_amber"]}">{esc(label)}</text>')
            continue
        message: TranscriptMessage = item["message"]
        style = _side_style(message.side)
        label_text = esc(f"{message.participant_label}: {ellipsize_text(message.text, 42)}")
        parts.append(f'<g class="message-row {message.side}" role="listitem" aria-label="{label_text}">')
        parts.append(f'<title>{label_text}</title>')
        parts.append(f'<rect x="{item["bubbleX"] + 4}" y="{item["y"] + 4}" width="{item["width"]}" height="{item["bubbleHeight"]}" fill="#000000" opacity="0.08"/>')
        parts.append(f'<rect x="{item["bubbleX"]}" y="{item["y"]}" width="{item["width"]}" height="{item["bubbleHeight"]}" fill="{style["box_fill"]}" opacity="0.18"/>')
        text_x = item["bubbleX"] + TERMINAL_BOX_PAD_X
        text_y = item["y"] + TERMINAL_BOX_PAD_Y + 16
        for line in _terminal_box_lines(message, item["lines"], item["width"]):
            if line["kind"] == "border":
                fill = style["border"]
                css_class = "border-line"
                opacity = "0.86"
            elif line["kind"] == "tag":
                fill = _tag_style(message.tags[0])["text"] if message.tags else style["text"]
                css_class = "content-line tag-line"
                opacity = "1"
            else:
                fill = style["text"]
                css_class = "content-line"
                opacity = "1"
            parts.append(f'<text class="{css_class}" x="{text_x}" y="{text_y}" font-size="{TERMINAL_FONT_SIZE}" fill="{fill}" opacity="{opacity}">{esc(line["text"])}</text>')
            text_y += TERMINAL_LINE_HEIGHT
        parts.append("</g>")
    parts.append("</g>")
    if page.footer:
        footer = f":: {page.footer} ::"
        parts.append(f'<text class="crt-text" x="{page.width / 2:.1f}" y="{page.height - 24}" text-anchor="middle" font-size="{TERMINAL_SMALL_FONT_SIZE}" fill="{THEME["muted"]}">{esc(footer)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def write_png(svg_path: Path, png_path: Path, page: LayoutPage) -> dict:
    return write_png_with_fallback(svg_path, png_path, page, _render_png_with_pillow)


def _render_png_with_pillow(page: LayoutPage, png_path: Path) -> None:
    from PIL import Image, ImageDraw

    scale = 2
    img = Image.new("RGBA", (page.width * scale, page.height * scale), rgba(THEME["bg_top"]))
    draw_vertical_gradient(img, THEME["bg_top"], THEME["bg_bottom"])
    draw = ImageDraw.Draw(img)
    font_regular = pil_font(TERMINAL_FONT_SIZE * scale) if page_contains_cjk(page) else pil_terminal_font(TERMINAL_FONT_SIZE * scale)
    font_small = pil_font(TERMINAL_SMALL_FONT_SIZE * scale) if page_contains_cjk(page) else pil_terminal_font(TERMINAL_SMALL_FONT_SIZE * scale)

    width_px, height_px = img.size
    for y in range(0, height_px, 4 * scale):
        draw.rectangle([0, y, width_px, y + scale - 1], fill=rgba(THEME["scanline"], 7))
    for y in range(9 * scale, height_px, 37 * scale):
        for x in range((y // scale) % 17 * scale, width_px, 41 * scale):
            draw.point((x, y), fill=rgba(THEME["phosphor"], 44))
    draw.rounded_rectangle([20 * scale, 16 * scale, (page.width - 20) * scale, (page.height - 16) * scale], radius=18 * scale, outline=rgba("#456746", 76), width=scale)

    header_y = 34 * scale
    for idx, line in enumerate(_terminal_header_lines(page)):
        color = THEME["phosphor"] if idx == 1 else THEME["phosphor_dim"]
        font = font_regular if idx != 2 else font_small
        draw.text((CANVAS_MARGIN * scale, header_y + idx * 22 * scale - 14 * scale), line, fill=rgba(color), font=font)

    for item in page.items:
        if item["kind"] == "ellipsis":
            draw.text((page.width * scale / 2, (item["y"] + 7) * scale), f"# {item['label']}", fill=rgba(THEME["muted"]), font=font_small, anchor="ma")
            continue
        if item["kind"] == "time":
            draw.text((page.width * scale / 2, (item["y"] + 7) * scale), f":: {item['label']} ::", fill=rgba(THEME["muted_amber"]), font=font_small, anchor="ma")
            continue
        message: TranscriptMessage = item["message"]
        style = _side_style(message.side)
        box = [item["bubbleX"] * scale, item["y"] * scale, (item["bubbleX"] + item["width"]) * scale, (item["y"] + item["bubbleHeight"]) * scale]
        draw.rectangle([box[0] + 4 * scale, box[1] + 4 * scale, box[2] + 4 * scale, box[3] + 4 * scale], fill=rgba(THEME["shadow"], 20))
        draw.rectangle(box, fill=rgba(style["box_fill"], 46))
        text_x = (item["bubbleX"] + TERMINAL_BOX_PAD_X) * scale
        text_y = (item["y"] + TERMINAL_BOX_PAD_Y + 1) * scale
        for line in _terminal_box_lines(message, item["lines"], item["width"]):
            if line["kind"] == "border":
                fill = style["border"]
            elif line["kind"] == "tag":
                fill = _tag_style(message.tags[0])["text"] if message.tags else style["text"]
            else:
                fill = style["text"]
            draw.text((text_x, text_y), line["text"], fill=rgba(fill), font=font_regular)
            text_y += TERMINAL_LINE_HEIGHT * scale
    if page.footer:
        draw.text((page.width * scale / 2, (page.height - 34) * scale), f":: {page.footer} ::", fill=rgba(THEME["muted"]), font=font_small, anchor="ma")
    img = img.resize((page.width, page.height), Image.Resampling.LANCZOS)
    img.convert("RGB").save(png_path)


def _positions(width: int, bubble_w: int, side: str) -> tuple[int, int, int, str]:
    if side == "right":
        avatar_x = width - CANVAS_MARGIN
        bubble_x = width - CANVAS_MARGIN - bubble_w
        meta_x = bubble_x + bubble_w
        return bubble_x, avatar_x, meta_x, "right"
    avatar_x = CANVAS_MARGIN
    bubble_x = CANVAS_MARGIN
    return bubble_x, avatar_x, bubble_x, "left"


def _side_style(side: str) -> dict[str, str]:
    return SIDE_STYLES["right" if side == "right" else "left"]


def _tag_style(tag: str) -> dict[str, str]:
    return TAG_STYLES.get(str(tag or "").lower(), TAG_STYLES["default"])


def _terminal_max_cols(width: int) -> int:
    available = int((width * 0.70 - TERMINAL_BOX_PAD_X * 2) / TERMINAL_CHAR_WIDTH) - 4
    return max(TERMINAL_MIN_COLS, min(TERMINAL_MAX_COLS, available))


def _terminal_cols_from_width(width: int) -> int:
    return max(TERMINAL_MIN_COLS, int(round((width - TERMINAL_BOX_PAD_X * 2) / TERMINAL_CHAR_WIDTH)) - 4)


def _tag_text(tags: list[str]) -> str:
    return " ".join(f"[{tag}]" for tag in tags)


def _terminal_box_lines(message: TranscriptMessage, lines: list[str], width: int) -> list[dict[str, str]]:
    cols = _terminal_cols_from_width(width)
    label = clip_display(f"[{message.participant_label}]", cols)
    inner_width = cols + 2
    if message.side == "right":
        label_part = label + "--"
        top = "+" + "-" * max(0, inner_width - display_cols(label_part)) + label_part + "+"
    else:
        label_part = "--" + label
        top = "+" + label_part + "-" * max(0, inner_width - display_cols(label_part)) + "+"
    result = [{"kind": "border", "text": top}]
    for line in lines:
        result.append({"kind": "content", "text": f"| {pad_display(line, cols)} |"})
    tag_line = _tag_text(message.tags)
    if tag_line:
        result.append({"kind": "tag", "text": f"| {pad_display(tag_line, cols)} |"})
    result.append({"kind": "border", "text": "+" + "-" * inner_width + "+"})
    return result


def _terminal_header_lines(page: LayoutPage) -> list[str]:
    cols = max(46, int((page.width - CANVAS_MARGIN * 2) / TERMINAL_CHAR_WIDTH) - 2)
    label = "[claworld/transcript.crt]"
    top_inner = "--" + label
    top = "+" + top_inner + "-" * max(0, cols - display_cols(top_inner)) + "+"
    title = clip_display(f"> {page.title}", cols - 2)
    profile = clip_display(f"profile: {page.subtitle}", cols - 2)
    bottom = "+" + "-" * cols + "+"
    return [
        top,
        f"| {pad_display(title, cols - 2)} |",
        f"| {pad_display(profile, cols - 2)} |",
        bottom,
    ]


def _svg_defs() -> str:
    font = terminal_font_family()
    return "\n".join(
        [
            "<defs>",
            f'<style><![CDATA[text {{ font-family: {font}; letter-spacing: 0; }} .crt-text {{ filter: url(#crtGlow); }} .message-row:hover .content-line {{ fill: #FFFFFF; }} .message-row:hover .border-line {{ opacity: 1; }}]]></style>',
            '<linearGradient id="crtBg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#05120B"/><stop offset="55%" stop-color="#07170E"/><stop offset="100%" stop-color="#101006"/></linearGradient>',
            '<radialGradient id="crtVignette" cx="50%" cy="45%" r="72%"><stop offset="0%" stop-color="#173820" stop-opacity="0.16"/><stop offset="72%" stop-color="#020604" stop-opacity="0.12"/><stop offset="100%" stop-color="#000000" stop-opacity="0.42"/></radialGradient>',
            '<pattern id="scanlines" width="1" height="4" patternUnits="userSpaceOnUse"><rect x="0" y="0" width="1" height="1" fill="#A5FFAA" opacity="0.065"/></pattern>',
            '<pattern id="terminalNoise" width="18" height="18" patternUnits="userSpaceOnUse"><rect x="3" y="5" width="1" height="1" fill="#8CFF9A" opacity="0.08"/><rect x="13" y="11" width="1" height="1" fill="#FFBF58" opacity="0.06"/><rect x="7" y="15" width="1" height="1" fill="#72F5D0" opacity="0.05"/></pattern>',
            '<filter id="crtGlow" x="-10%" y="-20%" width="120%" height="140%"><feGaussianBlur stdDeviation="0.35" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>',
            "</defs>",
        ]
    )


STYLE = TranscriptReportStyle(
    name="claworld-terminal-crt",
    measure_item=measure_item,
    paginate=paginate,
    render_svg=render_svg,
    write_png=write_png,
)
