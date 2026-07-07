"""Light instant-message transcript report style."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import TranscriptReportStyle
from ..transcript_report_stylekit import (
    clip_display,
    draw_vertical_gradient,
    ellipsize_text,
    esc,
    font_family,
    page_contains_cjk,
    pil_font,
    rgba,
    text_units,
    wrap_text,
    write_png_with_fallback,
)
from ..transcript_report_types import LayoutPage, MeasuredBubble, TranscriptMessage


CANVAS_MARGIN = 28
HEADER_HEIGHT = 122
BODY_TOP_GAP = 18
PAGE_BOTTOM = 48
ITEM_GAP = 14
TIME_ROW_HEIGHT = 30
ELLIPSIS_HEIGHT = 34
AVATAR_SIZE = 34
BUBBLE_PAD_X = 15
BUBBLE_PAD_Y = 12
BUBBLE_MAX_RATIO = 0.66
BUBBLE_MIN_WIDTH = 168
FONT_SIZE = 15
SMALL_FONT_SIZE = 12
LINE_HEIGHT = 22
TAG_HEIGHT = 24
TEXT_UNIT_PX = 14.5

THEME = {
    "bg_top": "#F7FCFA",
    "bg_bottom": "#EEF7F4",
    "paper": "#FFFFFF",
    "ink": "#21322D",
    "muted": "#688078",
    "line": "#DDEAE5",
    "left_fill": "#FFFFFF",
    "left_border": "#D8E8E2",
    "left_text": "#243A33",
    "right_fill": "#DDF7EA",
    "right_border": "#A8DEC1",
    "right_text": "#163829",
    "accent": "#32B887",
    "accent_dark": "#238462",
    "time_fill": "#E8F3EF",
    "time_text": "#70877F",
    "tag_fill": "#F0F8F5",
    "tag_text": "#2B8063",
    "shadow": "#A9C8BE",
}

TAG_COLORS = {
    "like": ("#E7F7ED", "#2D8A56"),
    "dislike": ("#FBECEC", "#BC5252"),
    "request end": ("#E9F2FF", "#3E70B8"),
}


def measure_item(item: dict[str, Any], width: int) -> MeasuredBubble:
    content_width = max(280, int(width * BUBBLE_MAX_RATIO))
    if item["kind"] == "ellipsis":
        return MeasuredBubble(kind="ellipsis", message=None, lines=[], width=content_width, height=ELLIPSIS_HEIGHT, omitted_count=item["omitted"], label=item["label"])
    if item["kind"] == "time":
        return MeasuredBubble(kind="time", message=None, lines=[], width=content_width, height=TIME_ROW_HEIGHT, label=item["label"])
    message = item["message"]
    max_units = max(20.0, (content_width - BUBBLE_PAD_X * 2) / TEXT_UNIT_PX)
    lines = wrap_text(message.text, max_units)
    tag_line = " ".join(message.tags)
    content_units = max([text_units(line) for line in lines] + [text_units(tag_line), 18.0])
    bubble_w = int(min(content_width, max(BUBBLE_MIN_WIDTH, content_units * TEXT_UNIT_PX + BUBBLE_PAD_X * 2)))
    text_height = len(lines) * LINE_HEIGHT
    tag_height = TAG_HEIGHT if message.tags else 0
    bubble_h = BUBBLE_PAD_Y * 2 + text_height + tag_height
    row_h = max(AVATAR_SIZE, bubble_h)
    return MeasuredBubble(
        kind="message",
        message=message,
        lines=lines,
        width=bubble_w,
        height=row_h,
        meta_height=0,
        tag_height=tag_height,
        text_height=text_height,
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
                    "bubbleHeight": BUBBLE_PAD_Y * 2 + item.text_height + item.tag_height,
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
    parts = [
        f'<svg class="im-light" xmlns="http://www.w3.org/2000/svg" width="{page.width}" height="{page.height}" viewBox="0 0 {page.width} {page.height}" role="img" aria-labelledby="{title_id} {desc_id}">',
        f'<title id="{title_id}">{esc(page.title)}</title>',
        f'<desc id="{desc_id}">{esc(desc)}</desc>',
        _svg_defs(),
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#imLightBg)"/>',
        f'<rect x="{CANVAS_MARGIN}" y="24" width="{page.width - CANVAS_MARGIN * 2}" height="{page.height - 48}" rx="22" fill="{THEME["paper"]}" opacity="0.62"/>',
        _render_header(page),
        '<g role="list">',
    ]
    for item in page.items:
        if item["kind"] == "ellipsis":
            parts.append(_render_ellipsis_svg(page, item))
            continue
        if item["kind"] == "time":
            parts.append(_render_time_svg(page, item))
            continue
        parts.append(_render_message_svg(item))
    parts.append("</g>")
    if page.footer:
        parts.append(f'<text x="{page.width / 2:.1f}" y="{page.height - 24}" text-anchor="middle" font-size="{SMALL_FONT_SIZE}" fill="{THEME["muted"]}">{esc(page.footer)}</text>')
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
    font_regular = pil_font(FONT_SIZE * scale)
    font_small = pil_font(SMALL_FONT_SIZE * scale)
    font_title = pil_font(22 * scale)

    draw.rounded_rectangle([CANVAS_MARGIN * scale, 24 * scale, (page.width - CANVAS_MARGIN) * scale, (page.height - 24) * scale], radius=22 * scale, fill=rgba(THEME["paper"], 160))
    draw.text((CANVAS_MARGIN * scale, 38 * scale), page.title, fill=rgba(THEME["ink"]), font=font_title)
    draw.text((CANVAS_MARGIN * scale, 66 * scale), clip_display(page.subtitle, 76), fill=rgba(THEME["muted"]), font=font_small)
    draw.line([(CANVAS_MARGIN * scale, 96 * scale), ((page.width - CANVAS_MARGIN) * scale, 96 * scale)], fill=rgba(THEME["line"]), width=scale)

    for item in page.items:
        if item["kind"] == "ellipsis":
            label = f"{item['label']}"
            draw.rounded_rectangle([(page.width / 2 - 120) * scale, item["y"] * scale, (page.width / 2 + 120) * scale, (item["y"] + 24) * scale], radius=12 * scale, fill=rgba(THEME["time_fill"]))
            draw.text((page.width * scale / 2, (item["y"] + 5) * scale), label, fill=rgba(THEME["time_text"]), font=font_small, anchor="ma")
            continue
        if item["kind"] == "time":
            label = item["label"]
            draw.rounded_rectangle([(page.width / 2 - 55) * scale, item["y"] * scale, (page.width / 2 + 55) * scale, (item["y"] + 24) * scale], radius=12 * scale, fill=rgba(THEME["time_fill"]))
            draw.text((page.width * scale / 2, (item["y"] + 5) * scale), label, fill=rgba(THEME["time_text"]), font=font_small, anchor="ma")
            continue
        message: TranscriptMessage = item["message"]
        colors = _side_colors(message.side)
        avatar_box = [item["avatarX"] * scale, item["y"] * scale, (item["avatarX"] + AVATAR_SIZE) * scale, (item["y"] + AVATAR_SIZE) * scale]
        draw.ellipse(avatar_box, fill=rgba(colors["avatar_fill"]))
        avatar = _avatar_text(message.participant_label)
        draw.text(((item["avatarX"] + AVATAR_SIZE / 2) * scale, (item["y"] + 8) * scale), avatar, fill=rgba(colors["avatar_text"]), font=font_small, anchor="ma")
        box = [item["bubbleX"] * scale, item["y"] * scale, (item["bubbleX"] + item["width"]) * scale, (item["y"] + item["bubbleHeight"]) * scale]
        draw.rounded_rectangle([box[0] + 2 * scale, box[1] + 4 * scale, box[2] + 2 * scale, box[3] + 4 * scale], radius=14 * scale, fill=rgba(THEME["shadow"], 34))
        draw.rounded_rectangle(box, radius=14 * scale, fill=rgba(colors["fill"]), outline=rgba(colors["border"]), width=scale)
        text_x = (item["bubbleX"] + BUBBLE_PAD_X) * scale
        text_y = (item["y"] + BUBBLE_PAD_Y - 2) * scale
        for line in item["lines"]:
            draw.text((text_x, text_y), line, fill=rgba(colors["text"]), font=font_regular)
            text_y += LINE_HEIGHT * scale
        if message.tags:
            tag_x = text_x
            tag_y = text_y + 2 * scale
            for tag in message.tags:
                fill, ink = TAG_COLORS.get(tag, (THEME["tag_fill"], THEME["tag_text"]))
                tag_w = max(44, int(text_units(tag) * 8.0 + 22)) * scale
                draw.rounded_rectangle([tag_x, tag_y, tag_x + tag_w, tag_y + 18 * scale], radius=9 * scale, fill=rgba(fill))
                draw.text((tag_x + 11 * scale, tag_y + 2 * scale), tag, fill=rgba(ink), font=font_small)
                tag_x += tag_w + 6 * scale
    if page.footer:
        draw.text((page.width * scale / 2, (page.height - 34) * scale), page.footer, fill=rgba(THEME["muted"]), font=font_small, anchor="ma")
    img = img.resize((page.width, page.height), Image.Resampling.LANCZOS)
    img.convert("RGB").save(png_path)


def _positions(width: int, bubble_w: int, side: str) -> tuple[int, int, int, str]:
    if side == "right":
        avatar_x = width - CANVAS_MARGIN - AVATAR_SIZE
        bubble_x = avatar_x - 10 - bubble_w
        meta_x = bubble_x + bubble_w
        return bubble_x, avatar_x, meta_x, "right"
    avatar_x = CANVAS_MARGIN
    bubble_x = CANVAS_MARGIN + AVATAR_SIZE + 10
    return bubble_x, avatar_x, bubble_x, "left"


def _render_header(page: LayoutPage) -> str:
    title = clip_display(page.title, 52)
    subtitle = clip_display(page.subtitle, 86)
    return "\n".join(
        [
            f'<text x="{CANVAS_MARGIN}" y="52" font-size="22" font-weight="700" fill="{THEME["ink"]}">{esc(title)}</text>',
            f'<text x="{CANVAS_MARGIN}" y="77" font-size="{SMALL_FONT_SIZE}" fill="{THEME["muted"]}">{esc(subtitle)}</text>',
            f'<line x1="{CANVAS_MARGIN}" y1="97" x2="{page.width - CANVAS_MARGIN}" y2="97" stroke="{THEME["line"]}" stroke-width="1"/>',
        ]
    )


def _render_ellipsis_svg(page: LayoutPage, item: dict[str, Any]) -> str:
    w = min(290, page.width - CANVAS_MARGIN * 2)
    x = (page.width - w) / 2
    y = item["y"]
    return (
        f'<g class="ellipsis-row">'
        f'<rect x="{x:.1f}" y="{y}" width="{w}" height="24" rx="12" fill="{THEME["time_fill"]}"/>'
        f'<text x="{page.width / 2:.1f}" y="{y + 16}" text-anchor="middle" font-size="{SMALL_FONT_SIZE}" fill="{THEME["time_text"]}">{esc(item["label"])}</text>'
        f'</g>'
    )


def _render_time_svg(page: LayoutPage, item: dict[str, Any]) -> str:
    y = item["y"]
    return (
        f'<g class="time-row">'
        f'<rect x="{page.width / 2 - 58:.1f}" y="{y}" width="116" height="24" rx="12" fill="{THEME["time_fill"]}"/>'
        f'<text x="{page.width / 2:.1f}" y="{y + 16}" text-anchor="middle" font-size="{SMALL_FONT_SIZE}" fill="{THEME["time_text"]}">{esc(item["label"])}</text>'
        f'</g>'
    )


def _render_message_svg(item: dict[str, Any]) -> str:
    message: TranscriptMessage = item["message"]
    colors = _side_colors(message.side)
    label_text = esc(f"{message.participant_label}: {ellipsize_text(message.text, 42)}")
    avatar = esc(_avatar_text(message.participant_label))
    parts = [
        f'<g class="message-row {message.side}" role="listitem" aria-label="{label_text}">',
        f'<title>{label_text}</title>',
        f'<circle cx="{item["avatarX"] + AVATAR_SIZE / 2:.1f}" cy="{item["y"] + AVATAR_SIZE / 2:.1f}" r="{AVATAR_SIZE / 2:.1f}" fill="{colors["avatar_fill"]}"/>',
        f'<text x="{item["avatarX"] + AVATAR_SIZE / 2:.1f}" y="{item["y"] + 22}" text-anchor="middle" font-size="{SMALL_FONT_SIZE}" font-weight="700" fill="{colors["avatar_text"]}">{avatar}</text>',
        f'<rect x="{item["bubbleX"] + 2}" y="{item["y"] + 4}" width="{item["width"]}" height="{item["bubbleHeight"]}" rx="14" fill="{THEME["shadow"]}" opacity="0.18"/>',
        f'<rect x="{item["bubbleX"]}" y="{item["y"]}" width="{item["width"]}" height="{item["bubbleHeight"]}" rx="14" fill="{colors["fill"]}" stroke="{colors["border"]}" stroke-width="1"/>',
    ]
    text_x = item["bubbleX"] + BUBBLE_PAD_X
    text_y = item["y"] + BUBBLE_PAD_Y + 15
    for line in item["lines"]:
        parts.append(f'<text x="{text_x}" y="{text_y}" font-size="{FONT_SIZE}" fill="{colors["text"]}">{esc(line)}</text>')
        text_y += LINE_HEIGHT
    if message.tags:
        tag_x = text_x
        tag_y = text_y + 2
        for tag in message.tags:
            fill, ink = TAG_COLORS.get(tag, (THEME["tag_fill"], THEME["tag_text"]))
            tag_w = max(44, int(text_units(tag) * 8.0 + 22))
            parts.append(f'<rect x="{tag_x}" y="{tag_y}" width="{tag_w}" height="18" rx="9" fill="{fill}"/>')
            parts.append(f'<text x="{tag_x + 11}" y="{tag_y + 13}" font-size="{SMALL_FONT_SIZE}" fill="{ink}">{esc(tag)}</text>')
            tag_x += tag_w + 6
    parts.append("</g>")
    return "\n".join(parts)


def _side_colors(side: str) -> dict[str, str]:
    if side == "right":
        return {
            "fill": THEME["right_fill"],
            "border": THEME["right_border"],
            "text": THEME["right_text"],
            "avatar_fill": THEME["accent"],
            "avatar_text": "#FFFFFF",
        }
    return {
        "fill": THEME["left_fill"],
        "border": THEME["left_border"],
        "text": THEME["left_text"],
        "avatar_fill": "#DDEBE6",
        "avatar_text": THEME["accent_dark"],
    }


def _avatar_text(label: str) -> str:
    clean = "".join(ch for ch in str(label or "") if ch.isalnum())
    return (clean[:2] or "A").upper()


def _svg_defs() -> str:
    font = font_family()
    return "\n".join(
        [
            "<defs>",
            f'<style><![CDATA[text {{ font-family: {font}; letter-spacing: 0; }} .message-row:hover rect:last-of-type {{ stroke-width: 1.5; }}]]></style>',
            '<linearGradient id="imLightBg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#F7FCFA"/><stop offset="100%" stop-color="#EEF7F4"/></linearGradient>',
            "</defs>",
        ]
    )


STYLE = TranscriptReportStyle(
    name="claworld-im-light",
    measure_item=measure_item,
    paginate=paginate,
    render_svg=render_svg,
    write_png=write_png,
)
