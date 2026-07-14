"""Comic grid transcript report style."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import TranscriptReportStyle
from ..transcript_report_stylekit import (
    EMOJI_INLINE_X_OFFSET,
    clip_display,
    display_cols,
    ellipsize_text,
    esc,
    font_css_rules,
    font_family,
    text_runs,
    text_units,
    wrap_text,
    write_png_from_svg,
)
from ..transcript_report_types import LayoutPage, MeasuredBubble, TranscriptMessage


CANVAS_MARGIN = 24
FRAME_MARGIN = 16
HEADER_Y = 48
HEADER_CARD_HEIGHT_ONE_LINE = 92
HEADER_CARD_HEIGHT_TWO_LINES = 110
HEADER_BOTTOM_PAD = 20
BODY_TOP_GAP = 24
PAGE_BOTTOM = 54
ITEM_GAP = 22
TIME_ROW_HEIGHT = 42
ELLIPSIS_HEIGHT = 34
BUBBLE_PAD_X = 32
BUBBLE_PAD_Y = 22
LABEL_HEIGHT = 30
LABEL_OVERLAP = 18
LABEL_RAISE = 9
LABEL_MAX_COLS = 14
BUBBLE_MAX_RATIO = 0.64
BUBBLE_MIN_WIDTH = 200
FONT_SIZE = 18
SMALL_FONT_SIZE = 12
LABEL_FONT_SIZE = 15
TITLE_FONT_SIZE = 34
LINE_HEIGHT = 29
HEADER_SUBTITLE_MAX_UNITS = 33.0
HEADER_SUBTITLE_MAX_LINES = 2
HEADER_SUBTITLE_LINE_HEIGHT = 19
HEADER_TITLE_MAX_COLS = 26
TAG_HEIGHT = 58
TAG_ICON_SIZE = 30
TAG_ICON_GAP = 12
TAG_ICON_TOP_GAP = 8
TAG_FALLBACK_MAX_COLS = 10
TEXT_UNIT_PX = 18.0
BLACK = "#090909"

THEME = {
    "paper": "#FBF8EF",
    "paper_warm": "#FFFDF7",
    "header_fill": "#FEF5D8",
    "grid_minor": "#BED1D8",
    "grid_major": "#AABFC8",
    "ink": BLACK,
    "muted": "#222222",
    "left_fill": "#EFFFF5",
    "left_label": "#62E69D",
    "left_accent_a": "#58E58F",
    "left_accent_b": "#47B6FF",
    "right_fill": "#EFE0FF",
    "right_label": "#B785FF",
    "right_accent_a": "#A871FF",
    "right_accent_b": "#FF4EB4",
    "time_fill": "#FFFDF7",
    "time_accent_left": "#FF62DE",
    "time_accent_right": "#50D995",
}

TAG_ICON_THEMES = {
    "like": ("#D8F4FF", "#58B7FF"),
    "dislike": ("#FFE0EA", "#FF6A9A"),
    "request end": ("#FFF0A8", "#FF9F2F"),
}


def measure_item(item: dict[str, Any], width: int) -> MeasuredBubble:
    content_width = max(270, int(width * BUBBLE_MAX_RATIO))
    if item["kind"] == "ellipsis":
        return MeasuredBubble(kind="ellipsis", message=None, lines=[], width=content_width, height=ELLIPSIS_HEIGHT, omitted_count=item["omitted"], label=item["label"])
    if item["kind"] == "time":
        return MeasuredBubble(kind="time", message=None, lines=[], width=content_width, height=TIME_ROW_HEIGHT, label=item["label"])

    message = item["message"]
    max_units = max(18.0, (content_width - BUBBLE_PAD_X * 2) / TEXT_UNIT_PX)
    lines = wrap_text(message.text, max_units)
    label_units = text_units(_label_text(message.participant_label)) + 4.0
    content_units = max([text_units(line) for line in lines] + [_tag_row_units(message.tags), label_units, 10.0])
    bubble_w = int(min(content_width, max(BUBBLE_MIN_WIDTH, content_units * TEXT_UNIT_PX + BUBBLE_PAD_X * 2)))
    text_height = len(lines) * LINE_HEIGHT
    tag_height = TAG_HEIGHT if message.tags else 0
    bubble_h = BUBBLE_PAD_Y * 2 + text_height + tag_height
    row_h = LABEL_HEIGHT - LABEL_OVERLAP + bubble_h + 10
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
    header_height = _header_height(subtitle)
    used = header_height + BODY_TOP_GAP + PAGE_BOTTOM
    for idx, item in enumerate(items):
        item_h = item.height + ITEM_GAP
        needed_h = item_h
        if item.kind == "time" and idx + 1 < len(items):
            needed_h += items[idx + 1].height + ITEM_GAP
        if pages[-1] and used + needed_h > max_height:
            pages.append([])
            used = header_height + BODY_TOP_GAP + PAGE_BOTTOM
        pages[-1].append(item)
        used += item_h

    rendered: list[LayoutPage] = []
    total = len(pages)
    for page_no, page_items in enumerate(pages, start=1):
        y = header_height + BODY_TOP_GAP
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
            label = _label_text(item.message.participant_label)
            bubble_x, label_x, label_w, align = _positions(width, item.width, label, item.message.side)
            bubble_y = y + LABEL_HEIGHT - LABEL_OVERLAP
            bubble_h = BUBBLE_PAD_Y * 2 + item.text_height + item.tag_height
            layout_items.append(
                {
                    "kind": "message",
                    "y": y,
                    "bubbleX": bubble_x,
                    "bubbleY": bubble_y,
                    "labelX": label_x,
                    "labelY": y - LABEL_RAISE,
                    "labelWidth": label_w,
                    "label": label,
                    "align": align,
                    "width": item.width,
                    "bubbleHeight": bubble_h,
                    "lines": item.lines,
                    "message": item.message,
                    "tagHeight": item.tag_height,
                    "textHeight": item.text_height,
                }
            )
            y += item.height + ITEM_GAP
        height = max(520, min(max_height, y + PAGE_BOTTOM))
        footer = "visit claworld.love"
        rendered.append(LayoutPage(page=page_no, width=width, height=height, items=layout_items, title=title, subtitle=subtitle, footer=footer))
    return rendered


def render_svg(page: LayoutPage) -> str:
    title_id = f"claworld-report-title-{page.page}"
    desc_id = f"claworld-report-desc-{page.page}"
    desc = f"{page.title}. {page.subtitle}. {len(page.items)} transcript rows."
    parts = [
        f'<svg class="comic-grid" xmlns="http://www.w3.org/2000/svg" width="{page.width}" height="{page.height}" viewBox="0 0 {page.width} {page.height}" role="img" aria-labelledby="{title_id} {desc_id}">',
        f'<title id="{title_id}">{esc(page.title)}</title>',
        f'<desc id="{desc_id}">{esc(desc)}</desc>',
        _svg_defs(page),
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="{THEME["paper"]}"/>',
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#comicGridMinor)"/>',
        f'<rect x="0" y="0" width="{page.width}" height="{page.height}" fill="url(#comicGridMajor)" opacity="0.46"/>',
        f'<rect x="{FRAME_MARGIN}" y="{FRAME_MARGIN}" width="{page.width - FRAME_MARGIN * 2}" height="{page.height - FRAME_MARGIN * 2}" rx="46" fill="none" stroke="{BLACK}" stroke-width="6"/>',
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
        parts.append(
            _render_inline_text_svg(
                page.footer,
                page.width / 2,
                page.height - 24,
                font_size=SMALL_FONT_SIZE,
                font_weight=700,
                fill="#444444",
                anchor="middle",
            )
        )
    parts.append("</svg>")
    return "\n".join(parts)


def write_png(svg_path: Path, png_path: Path, page: LayoutPage) -> dict:
    return write_png_from_svg(svg_path, png_path, width=page.width, height=page.height)


def _render_inline_text_svg(
    text: str,
    x: float,
    y: float,
    *,
    font_size: int,
    font_weight: int,
    fill: str,
    anchor: str = "start",
    class_name: str = "",
) -> str:
    """Render normal and emoji runs as independent text nodes for resvg."""

    runs = text_runs(text)
    base_classes = class_name.split()
    if len(runs) == 1:
        run, script = runs[0]
        classes = " ".join((*base_classes, f"font-{script}"))
        weight = 400 if script == "emoji" else font_weight
        anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
        return (
            f'<text class="{classes}" x="{x:.1f}" y="{y:.1f}"{anchor_attr} '
            f'font-size="{font_size}" font-weight="{weight}" fill="{fill}">{esc(run)}</text>'
        )

    total_width = sum(text_units(run) * font_size for run, _script in runs)
    cursor = x
    if anchor == "middle":
        cursor -= total_width / 2
    elif anchor == "end":
        cursor -= total_width

    nodes = []
    for run, script in runs:
        classes = " ".join((*base_classes, f"font-{script}"))
        weight = 400 if script == "emoji" else font_weight
        render_x = cursor + (font_size * EMOJI_INLINE_X_OFFSET if script == "emoji" else 0)
        nodes.append(
            f'<text class="{classes}" x="{render_x:.1f}" y="{y:.1f}" '
            f'font-size="{font_size}" font-weight="{weight}" fill="{fill}">{esc(run)}</text>'
        )
        cursor += text_units(run) * font_size
    return "\n".join(nodes)


def _positions(width: int, bubble_w: int, label: str, side: str) -> tuple[int, int, int, str]:
    label_w = _label_width(label)
    inset = CANVAS_MARGIN + 38
    if side == "right":
        bubble_x = width - inset - bubble_w
        label_x = bubble_x + bubble_w - label_w - 16
        return bubble_x, label_x, label_w, "right"
    bubble_x = inset
    label_x = bubble_x + 16
    return bubble_x, label_x, label_w, "left"


def _render_header(page: LayoutPage) -> str:
    x = CANVAS_MARGIN + 26
    y = HEADER_Y
    w = page.width - (CANVAS_MARGIN + 26) * 2
    h = _header_card_height(page.subtitle)
    title = clip_display(_header_title(page.title), HEADER_TITLE_MAX_COLS)
    subtitle = _render_header_subtitle_svg(x + 35, y + 70, _header_subtitle_lines(page.subtitle))
    return "\n".join(
        [
            f'<rect x="{x + 11}" y="{y + 6}" width="{w + 2}" height="{h + 10}" rx="22" fill="{BLACK}"/>',
            f'<rect x="{x + 7}" y="{y + 6}" width="{w}" height="{h + 4}" rx="22" fill="url(#headerAccent)"/>',
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="{THEME["header_fill"]}" stroke="{BLACK}" stroke-width="4"/>',
            _render_inline_text_svg(
                title,
                x + 28,
                y + 43,
                font_size=TITLE_FONT_SIZE,
                font_weight=900,
                fill=BLACK,
            ),
            subtitle,
            _decorative_star_svg(x + w - 62, y + 34, 22, "#FFFFFF", "url(#headerAccent)"),
            f'<circle cx="{x + w - 23}" cy="{y + 55}" r="9" fill="#72E3C0" stroke="{BLACK}" stroke-width="3"/>',
            f'<circle cx="{x + w - 25}" cy="{y + 53}" r="9" fill="#72E3C0" stroke="{BLACK}" stroke-width="3"/>',
        ]
    )


def _header_subtitle_lines(subtitle: str) -> list[str]:
    lines = wrap_text(str(subtitle or "").strip(), HEADER_SUBTITLE_MAX_UNITS)
    if len(lines) <= HEADER_SUBTITLE_MAX_LINES:
        return lines
    visible = lines[: HEADER_SUBTITLE_MAX_LINES - 1]
    remainder = " ".join(line.strip() for line in lines[HEADER_SUBTITLE_MAX_LINES - 1 :] if line.strip())
    visible.append(ellipsize_text(remainder, HEADER_SUBTITLE_MAX_UNITS, suffix="…"))
    return visible


def _header_card_height(subtitle: str) -> int:
    return HEADER_CARD_HEIGHT_TWO_LINES if len(_header_subtitle_lines(subtitle)) > 1 else HEADER_CARD_HEIGHT_ONE_LINE


def _header_height(subtitle: str) -> int:
    return HEADER_Y + _header_card_height(subtitle) + HEADER_BOTTOM_PAD


def _render_header_subtitle_svg(x: float, y: float, lines: list[str]) -> str:
    return "\n".join(
        [
            _render_inline_text_svg(
                line,
                x,
                y + idx * HEADER_SUBTITLE_LINE_HEIGHT,
                font_size=15,
                font_weight=700,
                fill=THEME["muted"],
                class_name="header-subtitle-line",
            )
            for idx, line in enumerate(lines)
        ]
    )


def _render_ellipsis_svg(page: LayoutPage, item: dict[str, Any]) -> str:
    y = item["y"] + 7
    return _render_inline_text_svg(
        item["label"],
        page.width / 2,
        y + 14,
        font_size=SMALL_FONT_SIZE,
        font_weight=700,
        fill="#555555",
        anchor="middle",
    )


def _render_time_svg(page: LayoutPage, item: dict[str, Any]) -> str:
    label = clip_display(item["label"], 22)
    label_w = max(150, display_cols(label) * 8 + 44)
    x = page.width / 2 - label_w / 2
    y = item["y"] + 4
    return "\n".join(
        [
            '<g class="time-row">',
            _diamond_svg(x - 28, y + 16, 13, "#FF5BE2"),
            f'<rect x="{x + 3:.1f}" y="{y + 4}" width="{label_w}" height="30" rx="15" fill="{BLACK}"/>',
            f'<rect x="{x:.1f}" y="{y}" width="{label_w}" height="30" rx="15" fill="{THEME["time_fill"]}" stroke="{BLACK}" stroke-width="3"/>',
            _render_inline_text_svg(
                label,
                page.width / 2,
                y + 21,
                font_size=FONT_SIZE,
                font_weight=700,
                fill=BLACK,
                anchor="middle",
            ),
            _diamond_svg(x + label_w + 28, y + 16, 13, "#5FE0A7"),
            "</g>",
        ]
    )


def _render_message_svg(item: dict[str, Any]) -> str:
    message: TranscriptMessage = item["message"]
    colors = _side_colors(message.side)
    label_text = esc(f"{message.participant_label}: {ellipsize_text(message.text, 42)}")
    parts = [
        f'<g class="message-row {message.side}" role="listitem" aria-label="{label_text}">',
        f'<title>{label_text}</title>',
        _bubble_layers_svg(item, colors),
        f'<rect x="{item["labelX"]}" y="{item["labelY"]}" width="{item["labelWidth"]}" height="{LABEL_HEIGHT}" rx="9" fill="{colors["label"]}" stroke="{BLACK}" stroke-width="3"/>',
        _render_inline_text_svg(
            item["label"],
            item["labelX"] + item["labelWidth"] / 2,
            item["labelY"] + 21,
            font_size=LABEL_FONT_SIZE,
            font_weight=900,
            fill=BLACK,
            anchor="middle",
        ),
    ]
    text_x = item["bubbleX"] + BUBBLE_PAD_X
    text_y = item["bubbleY"] + BUBBLE_PAD_Y + 17
    for line in item["lines"]:
        parts.append(
            _render_inline_text_svg(
                line,
                text_x,
                text_y,
                font_size=FONT_SIZE,
                font_weight=800,
                fill=BLACK,
            )
        )
        text_y += LINE_HEIGHT
    if message.tags:
        parts.append(_render_tag_icons_svg(message.tags, text_x, text_y + TAG_ICON_TOP_GAP))
    parts.append("</g>")
    return "\n".join(parts)


def _bubble_layers_svg(item: dict[str, Any], colors: dict[str, str]) -> str:
    x = item["bubbleX"]
    y = item["bubbleY"]
    w = item["width"]
    h = item["bubbleHeight"]
    shadow_x = x + 11
    shadow_y = y + 9
    accent_x = x + 11
    accent_y = y + 9
    return "\n".join(
        [
            f'<rect x="{shadow_x}" y="{shadow_y}" width="{w + 2}" height="{h + 4}" rx="17" fill="{BLACK}"/>',
            f'<rect x="{accent_x}" y="{accent_y}" width="{w - 3}" height="{h}" rx="17" fill="{colors["accent"]}" stroke="{BLACK}" stroke-width="3"/>',
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="17" fill="{colors["fill"]}" stroke="{BLACK}" stroke-width="4"/>',
        ]
    )


def _side_colors(side: str) -> dict[str, str]:
    if side == "right":
        return {
            "fill": THEME["right_fill"],
            "label": THEME["right_label"],
            "accent": "url(#rightAccent)",
        }
    return {
        "fill": THEME["left_fill"],
        "label": THEME["left_label"],
        "accent": "url(#leftAccent)",
    }


def _header_title(title: str) -> str:
    clean = str(title or "").strip()
    if clean.startswith("@"):
        return clean
    return "@" + clean if clean else "@claworld"


def _label_text(label: str) -> str:
    return clip_display(str(label or "AGENT").upper(), LABEL_MAX_COLS)


def _label_width(label: str) -> int:
    return max(86, min(158, display_cols(label) * 11 + 30))


def _tag_row_units(tags: list[str]) -> float:
    if not tags:
        return 0.0
    width = sum(_tag_width(tag) for tag in tags) + max(0, len(tags) - 1) * TAG_ICON_GAP
    return width / TEXT_UNIT_PX


def _render_tag_icons_svg(tags: list[str], x: float, y: float) -> str:
    parts = [f'<g class="tag-icons" transform="translate({x:.1f} {y:.1f})">']
    cursor = 0.0
    for tag in tags:
        parts.append(_tag_icon_svg(tag, cursor, 0))
        cursor += _tag_width(tag) + TAG_ICON_GAP
    parts.append("</g>")
    return "\n".join(parts)


def _tag_width(tag: str) -> int:
    normalized = _tag_name(tag)
    if normalized in {"like", "dislike", "request end"}:
        return TAG_ICON_SIZE
    return max(58, min(126, display_cols(_fallback_tag_label(normalized)) * 8 + 24))


def _tag_icon_svg(tag: str, x: float, y: float) -> str:
    normalized = _tag_name(tag)
    fill, accent = TAG_ICON_THEMES.get(normalized, ("#FFFFFF", "#7DD7FF"))
    label = esc(normalized or "tag")
    if normalized not in {"like", "dislike", "request end"}:
        return _fallback_tag_svg(normalized, x, y)
    size = TAG_ICON_SIZE
    icon = _request_end_icon_svg(x, y, accent) if normalized == "request end" else _thumb_icon_svg(x, y, accent, down=normalized == "dislike")
    return "\n".join(
        [
            f'<g class="tag-icon tag-{esc(normalized.replace(" ", "-"))}" role="img" aria-label="{label}">',
            f"<title>{label}</title>",
            f'<rect x="{x + 3:.1f}" y="{y + 4:.1f}" width="{size}" height="{size}" rx="9" fill="{BLACK}"/>',
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{size}" height="{size}" rx="9" fill="{fill}" stroke="{BLACK}" stroke-width="2.5"/>',
            icon,
            "</g>",
        ]
    )


def _tag_name(tag: str) -> str:
    return str(tag or "").strip().lower()


def _fallback_tag_label(tag: str) -> str:
    return clip_display(str(tag or "tag"), TAG_FALLBACK_MAX_COLS)


def _fallback_tag_svg(tag: str, x: float, y: float) -> str:
    label = _fallback_tag_label(tag)
    w = _tag_width(tag)
    return "\n".join(
        [
            f'<g class="tag-icon tag-fallback" role="img" aria-label="{esc(label)}">',
            f"<title>{esc(label)}</title>",
            f'<rect x="{x + 3:.1f}" y="{y + 4:.1f}" width="{w}" height="{TAG_ICON_SIZE}" rx="9" fill="{BLACK}"/>',
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{TAG_ICON_SIZE}" rx="9" fill="#F6F1FF" stroke="{BLACK}" stroke-width="2.5"/>',
            _render_inline_text_svg(
                label,
                x + w / 2,
                y + 20.5,
                font_size=LABEL_FONT_SIZE,
                font_weight=900,
                fill=BLACK,
                anchor="middle",
            ),
            "</g>",
        ]
    )


def _thumb_icon_svg(x: float, y: float, accent: str, *, down: bool = False) -> str:
    paths = "\n".join(
        [
            f'<path d="M{x + 7.8:.1f} {y + 13.3:.1f} H{x + 12.0:.1f} V{y + 24.0:.1f} H{x + 7.8:.1f} Z" fill="#FFFFFF" stroke="{BLACK}" stroke-width="2" stroke-linejoin="round"/>',
            f'<path d="M{x + 12.0:.1f} {y + 23.6:.1f} H{x + 21.2:.1f} C{x + 23.0:.1f} {y + 23.6:.1f} {x + 24.0:.1f} {y + 22.5:.1f} {x + 24.4:.1f} {y + 20.9:.1f} L{x + 25.4:.1f} {y + 15.6:.1f} C{x + 25.8:.1f} {y + 13.9:.1f} {x + 24.5:.1f} {y + 12.2:.1f} {x + 22.7:.1f} {y + 12.2:.1f} H{x + 18.6:.1f} L{x + 19.2:.1f} {y + 9.1:.1f} C{x + 19.5:.1f} {y + 7.4:.1f} {x + 18.4:.1f} {y + 5.7:.1f} {x + 16.7:.1f} {y + 5.4:.1f} L{x + 15.8:.1f} {y + 5.3:.1f} L{x + 12.0:.1f} {y + 12.9:.1f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="2" stroke-linejoin="round"/>',
        ]
    )
    if not down:
        return paths
    cx = x + TAG_ICON_SIZE / 2
    cy = y + TAG_ICON_SIZE / 2
    return f'<g transform="rotate(180 {cx:.1f} {cy:.1f})">\n{paths}\n</g>'


def _request_end_icon_svg(x: float, y: float, accent: str) -> str:
    return "\n".join(
        [
            f'<path d="M{x + 22.0:.1f} {y + 4.0:.1f} C{x + 25.2:.1f} {y + 5.4:.1f} {x + 26.6:.1f} {y + 7.8:.1f} {x + 26.5:.1f} {y + 10.6:.1f}" fill="none" stroke="{BLACK}" stroke-width="2" stroke-linecap="round"/>',
            f'<path d="M{x + 9.215:.3f} {y + 18.117:.3f} C{x + 10.043:.3f} {y + 22.457:.3f} {x + 13.384:.3f} {y + 25.036:.3f} {x + 17.132:.3f} {y + 23.961:.3f} L{x + 19.440:.3f} {y + 23.300:.3f} C{x + 22.323:.3f} {y + 22.473:.3f} {x + 23.488:.3f} {y + 19.642:.3f} {x + 22.538:.3f} {y + 16.690:.3f} L{x + 21.435:.3f} {y + 12.845:.3f} L{x + 9.227:.3f} {y + 16.345:.3f} L{x + 9.215:.3f} {y + 18.117:.3f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="2" stroke-linejoin="round"/>',
            f'<path d="M{x + 9.665:.3f} {y + 7.897:.3f} L{x + 9.473:.3f} {y + 7.952:.3f} C{x + 8.756:.3f} {y + 8.158:.3f} {x + 8.286:.3f} {y + 8.709:.3f} {x + 8.422:.3f} {y + 9.184:.3f} L{x + 10.192:.3f} {y + 15.356:.3f} C{x + 10.328:.3f} {y + 15.831:.3f} {x + 11.019:.3f} {y + 16.049:.3f} {x + 11.736:.3f} {y + 15.843:.3f} L{x + 11.928:.3f} {y + 15.788:.3f} C{x + 12.645:.3f} {y + 15.583:.3f} {x + 13.115:.3f} {y + 15.031:.3f} {x + 12.979:.3f} {y + 14.557:.3f} L{x + 11.209:.3f} {y + 8.384:.3f} C{x + 11.073:.3f} {y + 7.910:.3f} {x + 10.382:.3f} {y + 7.692:.3f} {x + 9.665:.3f} {y + 7.897:.3f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="1.8"/>',
            f'<path d="M{x + 11.930:.3f} {y + 5.999:.3f} L{x + 11.738:.3f} {y + 6.055:.3f} C{x + 11.021:.3f} {y + 6.260:.3f} {x + 10.553:.3f} {y + 6.820:.3f} {x + 10.692:.3f} {y + 7.306:.3f} L{x + 12.729:.3f} {y + 14.408:.3f} C{x + 12.868:.3f} {y + 14.894:.3f} {x + 13.562:.3f} {y + 15.122:.3f} {x + 14.279:.3f} {y + 14.916:.3f} L{x + 14.471:.3f} {y + 14.861:.3f} C{x + 15.188:.3f} {y + 14.655:.3f} {x + 15.656:.3f} {y + 14.095:.3f} {x + 15.516:.3f} {y + 13.609:.3f} L{x + 13.480:.3f} {y + 6.507:.3f} C{x + 13.341:.3f} {y + 6.021:.3f} {x + 12.647:.3f} {y + 5.794:.3f} {x + 11.930:.3f} {y + 5.999:.3f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="1.8"/>',
            f'<path d="M{x + 14.636:.3f} {y + 5.640:.3f} L{x + 14.443:.3f} {y + 5.695:.3f} C{x + 13.727:.3f} {y + 5.900:.3f} {x + 13.258:.3f} {y + 6.457:.3f} {x + 13.396:.3f} {y + 6.938:.3f} L{x + 15.338:.3f} {y + 13.714:.3f} C{x + 15.476:.3f} {y + 14.195:.3f} {x + 16.169:.3f} {y + 14.418:.3f} {x + 16.886:.3f} {y + 14.213:.3f} L{x + 17.078:.3f} {y + 14.157:.3f} C{x + 17.795:.3f} {y + 13.952:.3f} {x + 18.264:.3f} {y + 13.395:.3f} {x + 18.126:.3f} {y + 12.914:.3f} L{x + 16.183:.3f} {y + 6.139:.3f} C{x + 16.045:.3f} {y + 5.658:.3f} {x + 15.352:.3f} {y + 5.434:.3f} {x + 14.636:.3f} {y + 5.640:.3f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="1.8"/>',
            f'<path d="M{x + 17.800:.3f} {y + 6.085:.3f} L{x + 17.702:.3f} {y + 6.113:.3f} C{x + 16.971:.3f} {y + 6.322:.3f} {x + 16.482:.3f} {y + 6.851:.3f} {x + 16.609:.3f} {y + 7.294:.3f} L{x + 18.176:.3f} {y + 12.759:.3f} C{x + 18.303:.3f} {y + 13.202:.3f} {x + 18.998:.3f} {y + 13.391:.3f} {x + 19.729:.3f} {y + 13.182:.3f} L{x + 19.827:.3f} {y + 13.154:.3f} C{x + 20.557:.3f} {y + 12.944:.3f} {x + 21.046:.3f} {y + 12.415:.3f} {x + 20.919:.3f} {y + 11.972:.3f} L{x + 19.352:.3f} {y + 6.507:.3f} C{x + 19.225:.3f} {y + 6.065:.3f} {x + 18.530:.3f} {y + 5.875:.3f} {x + 17.800:.3f} {y + 6.085:.3f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="1.8"/>',
            f'<path d="M{x + 9.546:.3f} {y + 19.999:.3f} L{x + 6.000:.3f} {y + 17.791:.3f} C{x + 4.736:.3f} {y + 17.009:.3f} {x + 5.668:.3f} {y + 15.181:.3f} {x + 7.138:.3f} {y + 15.592:.3f} L{x + 10.615:.3f} {y + 17.196:.3f} L{x + 9.546:.3f} {y + 19.999:.3f} Z" fill="{accent}" stroke="{BLACK}" stroke-width="1.8" stroke-linejoin="round"/>',
            f'<rect x="{x + 10.887:.3f}" y="{y + 14.834:.3f}" width="9.584" height="3.800" transform="rotate(-16.9438 {x + 10.887:.3f} {y + 14.834:.3f})" fill="{accent}"/>',
            f'<path d="M{x + 10.500:.1f} {y + 19.500:.1f} L{x + 11.000:.1f} {y + 20.000:.1f} L{x + 11.500:.1f} {y + 17.000:.1f} L{x + 9.500:.1f} {y + 17.500:.1f} L{x + 9.000:.1f} {y + 18.500:.1f} L{x + 10.500:.1f} {y + 19.500:.1f} Z" fill="{accent}"/>',
        ]
    )


def _decorative_star_svg(cx: float, cy: float, r: float, fill: str, accent: str) -> str:
    back_path = " ".join(f"{x:.1f},{y:.1f}" for x, y in _star_points(cx + 3, cy + 5, r))
    front_path = " ".join(f"{x:.1f},{y:.1f}" for x, y in _star_points(cx, cy, r))
    return "\n".join(
        [
            f'<polygon points="{back_path}" fill="{accent}"/>',
            f'<polygon points="{front_path}" fill="{fill}" stroke="{BLACK}" stroke-width="3"/>',
        ]
    )


def _diamond_svg(cx: float, cy: float, r: float, fill: str) -> str:
    shadow = f'<polygon points="{cx + 1:.1f},{cy + 2 - r:.1f} {cx + 1 + r * 0.46:.1f},{cy + 2:.1f} {cx + 1:.1f},{cy + 2 + r:.1f} {cx + 1 - r * 0.46:.1f},{cy + 2:.1f}" fill="{BLACK}" stroke="{BLACK}" stroke-width="2.5"/>'
    front = f'<polygon points="{cx:.1f},{cy - r:.1f} {cx + r * 0.46:.1f},{cy:.1f} {cx:.1f},{cy + r:.1f} {cx - r * 0.46:.1f},{cy:.1f}" fill="{fill}" stroke="{BLACK}" stroke-width="2.5"/>'
    return shadow + "\n" + front


def _star_points(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [
        (cx, cy - r),
        (cx + r * 0.28, cy - r * 0.28),
        (cx + r, cy),
        (cx + r * 0.28, cy + r * 0.28),
        (cx, cy + r),
        (cx - r * 0.28, cy + r * 0.28),
        (cx - r, cy),
        (cx - r * 0.28, cy - r * 0.28),
    ]


def _svg_defs(page: LayoutPage) -> str:
    font = font_family()
    script_fonts = font_css_rules(_page_text_values(page))
    return "\n".join(
        [
            "<defs>",
            f'<style><![CDATA[text {{ font-family: {font}; font-weight: 700; letter-spacing: 0; }} {script_fonts} .message-row:hover rect:last-of-type {{ filter: url(#comicLift); }}]]></style>',
            '<pattern id="comicGridMinor" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M 32 0 L 0 0 0 32" fill="none" stroke="#BED1D8" stroke-width="1" stroke-opacity="0.62"/></pattern>',
            '<pattern id="comicGridMajor" width="128" height="128" patternUnits="userSpaceOnUse"><path d="M 128 0 L 0 0 0 128" fill="none" stroke="#AABFC8" stroke-width="1.4" stroke-opacity="0.72"/></pattern>',
            '<linearGradient id="headerAccent" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#47B6FF"/><stop offset="52%" stop-color="#FF4EB4"/><stop offset="100%" stop-color="#FF8A2A"/></linearGradient>',
            '<linearGradient id="leftAccent" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#58E58F"/><stop offset="100%" stop-color="#47B6FF"/></linearGradient>',
            '<linearGradient id="rightAccent" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#A871FF"/><stop offset="100%" stop-color="#FF4EB4"/></linearGradient>',
            '<filter id="comicLift" x="-6%" y="-16%" width="112%" height="132%"><feDropShadow dx="0" dy="2" stdDeviation="1.2" flood-color="#000000" flood-opacity="0.14"/></filter>',
            "</defs>",
        ]
    )


def _page_text_values(page: LayoutPage) -> list[str]:
    values = [page.title, page.subtitle, page.footer]
    for item in page.items:
        values.append(str(item.get("label") or ""))
        values.extend(str(line) for line in item.get("lines") or [])
        message = item.get("message")
        if isinstance(message, TranscriptMessage):
            values.extend(message.tags)
    return values


STYLE = TranscriptReportStyle(
    name="claworld-comic-grid",
    measure_item=measure_item,
    paginate=paginate,
    render_svg=render_svg,
    write_png=write_png,
)
