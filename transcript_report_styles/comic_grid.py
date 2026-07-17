"""Comic grid transcript report style."""

from __future__ import annotations

from dataclasses import replace
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
    grapheme_clusters,
    text_runs,
    text_units,
    wrap_text,
    wrap_tokens,
    write_png_from_svg,
)
from ..transcript_report_types import LayoutPage, MeasuredBubble, TranscriptMessage


CANVAS_MARGIN = 24
FRAME_MARGIN = 16
HEADER_Y = 48
HEADER_CARD_HEIGHT_FULL = 286
HEADER_CARD_HEIGHT_NO_CONTEXT = 168
HEADER_CARD_HEIGHT_COMPACT = 96
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
TITLE_FONT_SIZE = 25
LINE_HEIGHT = 29
HEADER_TOPIC_MAX_UNITS = 20.0
HEADER_TOPIC_MAX_LINES = 2
HEADER_TOPIC_LINE_HEIGHT = 26
HEADER_TOPIC_EMBLEM_HALF_WIDTH = 28
HEADER_TOPIC_EMBLEM_GAP = 12
HEADER_COMPACT_TOPIC_MAX_UNITS = 26.0
CONTEXT_CARD_HEIGHT = 54
CONTEXT_CARD_GAP = 8
CONTEXT_LABEL_FONT_SIZE = 12
CONTEXT_TEXT_FONT_SIZE = 13
CONTEXT_TEXT_LINE_HEIGHT = 18
CONTEXT_TEXT_MAX_LINES = 2
CONTEXT_TEXT_BASELINE_CENTER_OFFSET = 4
TAG_HEIGHT = 58
TAG_ICON_SIZE = 30
TAG_ICON_GAP = 12
TAG_ICON_TOP_GAP = 8
TAG_FALLBACK_MAX_COLS = 10
TEXT_UNIT_PX = 18.0
IDENTITY_WIDTH_SAFETY = 1.14
IDENTITY_CODE_GAP = 3.0
IDENTITY_NAME_FONT_SIZE = 26
IDENTITY_CODE_FONT_SIZE = 19
IDENTITY_COMPACT_NAME_FONT_SIZE = 22
IDENTITY_COMPACT_CODE_FONT_SIZE = 16
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
    "direct_badge": "#67DDF1",
    "world_badge": "#FFB34F",
    "chat_badge": "#D3B7FF",
    "passport_strip": "#FFFDF7",
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


def paginate(
    items: list[MeasuredBubble],
    width: int,
    max_height: int,
    title: str,
    subtitle: str,
    header: Any | None = None,
) -> list[LayoutPage]:
    # A single message can be taller than a whole page.  Split its already
    # wrapped lines before pagination so every fragment participates in the
    # normal page-flow logic and no message can paint beyond the frame.
    full_header_height = _header_height(compact=False, header=header, subtitle=subtitle)
    safe_item_height = max(
        LABEL_HEIGHT - LABEL_OVERLAP + BUBBLE_PAD_Y * 2 + LINE_HEIGHT + 10,
        max_height
        - full_header_height
        - BODY_TOP_GAP
        - PAGE_BOTTOM
        - TIME_ROW_HEIGHT
        - ITEM_GAP * 2,
    )
    items = _split_oversized_messages(items, safe_item_height)
    pages: list[list[MeasuredBubble]] = [[]]
    header_height = full_header_height
    used = header_height + BODY_TOP_GAP + PAGE_BOTTOM
    for idx, item in enumerate(items):
        item_h = item.height + ITEM_GAP
        needed_h = item_h
        if item.kind == "time" and idx + 1 < len(items):
            needed_h += items[idx + 1].height + ITEM_GAP
        if pages[-1] and used + needed_h > max_height:
            pages.append([])
            used = _header_height(compact=True, header=header, subtitle=subtitle) + BODY_TOP_GAP + PAGE_BOTTOM
        pages[-1].append(item)
        used += item_h

    rendered: list[LayoutPage] = []
    total = len(pages)
    for page_no, page_items in enumerate(pages, start=1):
        y = _header_height(
            compact=page_no > 1,
            header=header,
            subtitle=subtitle,
        ) + BODY_TOP_GAP
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
        page_kwargs: dict[str, Any] = {
            "page": page_no,
            "width": width,
            "height": height,
            "items": layout_items,
            "title": title,
            "subtitle": subtitle,
            "footer": footer,
        }
        # LayoutPage gained structured passport fields after the style API was
        # introduced.  Build against either shape so third-party/older callers
        # using the style directly continue to work.
        layout_fields = getattr(LayoutPage, "__dataclass_fields__", {})
        if "header" in layout_fields:
            page_kwargs["header"] = header
        if "page_count" in layout_fields:
            page_kwargs["page_count"] = total
        layout_page = LayoutPage(**page_kwargs)
        if "header" not in layout_fields:
            setattr(layout_page, "header", header)
        if "page_count" not in layout_fields:
            setattr(layout_page, "page_count", total)
        rendered.append(layout_page)
    return rendered


def _split_oversized_messages(
    items: list[MeasuredBubble],
    max_item_height: int,
) -> list[MeasuredBubble]:
    result: list[MeasuredBubble] = []
    base_height = LABEL_HEIGHT - LABEL_OVERLAP + BUBBLE_PAD_Y * 2 + 10
    for item in items:
        if item.kind != "message" or item.message is None or item.height <= max_item_height:
            result.append(item)
            continue

        remaining = list(item.lines) or [""]
        part = 1
        final_line_capacity = max(
            1,
            int((max_item_height - base_height - item.tag_height) // LINE_HEIGHT),
        )
        regular_line_capacity = max(
            1,
            int((max_item_height - base_height) // LINE_HEIGHT),
        )
        while remaining:
            is_final = len(remaining) <= final_line_capacity
            line_count = (
                len(remaining)
                if is_final
                else min(regular_line_capacity, max(1, len(remaining) - final_line_capacity))
            )
            fragment_lines = remaining[:line_count]
            del remaining[:line_count]
            fragment_tags = list(item.message.tags) if not remaining else []
            fragment_tag_height = TAG_HEIGHT if fragment_tags else 0
            fragment_message = replace(
                item.message,
                id=f"{item.message.id}:part-{part}",
                text="\n".join(fragment_lines),
                tags=fragment_tags,
            )
            text_height = len(fragment_lines) * LINE_HEIGHT
            result.append(
                MeasuredBubble(
                    kind="message",
                    message=fragment_message,
                    lines=fragment_lines,
                    width=item.width,
                    height=base_height + text_height + fragment_tag_height,
                    meta_height=item.meta_height,
                    tag_height=fragment_tag_height,
                    text_height=text_height,
                )
            )
            part += 1
    return result


def render_svg(page: LayoutPage) -> str:
    title_id = f"claworld-report-title-{page.page}"
    desc_id = f"claworld-report-desc-{page.page}"
    passport = _passport_data(page)
    desc_values = [
        passport["mode_label"],
        passport["topic"],
        passport["participants"],
        passport["context"],
        passport["meta"],
    ]
    desc = ". ".join(value for value in desc_values if value) + f". {len(page.items)} transcript rows."
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
    if page.page > 1:
        return _render_compact_header(page)
    return _render_full_header(page)


def _render_full_header(page: LayoutPage) -> str:
    x = CANVAS_MARGIN + 26
    y = HEADER_Y
    w = page.width - (CANVAS_MARGIN + 26) * 2
    data = _passport_data(page)
    h = _full_header_card_height(data["context_blocks"])
    mode_width = _mode_badge_width(data["mode_label"])
    page_label = _page_label(page)
    page_width = _small_badge_width(page_label, minimum=48)
    count_label = data["count_label"]
    count_width = _small_badge_width(count_label, minimum=48) if count_label else 0
    right_edge = x + w - 20
    page_x = right_edge - page_width
    count_x = page_x - count_width - (8 if count_label else 0)
    secondary_x = x + 20 + mode_width + 10
    secondary_right = count_x - 10 if count_label else page_x - 10
    secondary_width = max(0, secondary_right - secondary_x)

    topic_center_x = x + w / 2
    emblem_center_x = x + w - 47
    topic_safe_right = emblem_center_x - HEADER_TOPIC_EMBLEM_HALF_WIDTH - HEADER_TOPIC_EMBLEM_GAP
    topic_half_width = min(
        topic_center_x - (x + 24),
        topic_safe_right - topic_center_x,
    )
    topic_max_units = max(
        8.0,
        min(HEADER_TOPIC_MAX_UNITS, topic_half_width * 2 / TITLE_FONT_SIZE),
    )
    topic_lines = _topic_lines(data["topic"], max_units=topic_max_units)
    topic_y = y + (71 if len(topic_lines) > 1 else 84)

    parts = [
        '<g class="conversation-passport conversation-passport-full">',
        f'<rect x="{x + 11}" y="{y + 7}" width="{w + 2}" height="{h + 10}" rx="24" fill="{BLACK}"/>',
        f'<rect x="{x + 7}" y="{y + 6}" width="{w}" height="{h + 4}" rx="24" fill="url(#headerAccent)"/>',
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="24" fill="{THEME["header_fill"]}" stroke="{BLACK}" stroke-width="4"/>',
        _mode_badge_svg(x + 20, y + 14, data["mode"], data["mode_label"]),
    ]
    if secondary_width >= 54:
        secondary = data["world_name"] or "CLAWORLD CHAT"
        parts.append(_secondary_badge_svg(secondary_x, y + 16, secondary_width, secondary))
    if count_label:
        parts.append(
            _small_badge_svg(
                count_x,
                y + 15,
                count_width,
                count_label,
                "#FFFFFF",
                "message-count-badge",
                accessible_label=count_label,
            )
        )
    parts.append(_small_badge_svg(page_x, y + 15, page_width, page_label, "#F1E5FF", "page-badge"))
    for idx, line in enumerate(topic_lines):
        parts.append(
            _render_inline_text_svg(
                line,
                topic_center_x,
                topic_y + idx * HEADER_TOPIC_LINE_HEIGHT,
                font_size=TITLE_FONT_SIZE,
                font_weight=900,
                fill=BLACK,
                anchor="middle",
                class_name="conversation-topic",
            )
        )
    parts.extend(
        [
            _mode_emblem_svg(x + w - 47, y + 82, data["mode"]),
            _identity_route_svg(
                x + 18,
                y + 104,
                w - 36,
                data["peer_identity"],
                data["local_identity"],
                data["initiated_by"],
            ),
        ]
    )
    if data["context_blocks"]:
        parts.append(
            _render_context_cards(
                x + 18,
                y + 156,
                w - 36,
                data["context_blocks"],
            )
        )
    parts.append("</g>")
    return "\n".join(parts)


def _render_compact_header(page: LayoutPage) -> str:
    x = CANVAS_MARGIN + 26
    y = HEADER_Y
    w = page.width - (CANVAS_MARGIN + 26) * 2
    h = HEADER_CARD_HEIGHT_COMPACT
    data = _passport_data(page)
    mode_width = _mode_badge_width(data["mode_label"], compact=True)
    page_label = _page_label(page)
    page_width = _small_badge_width(page_label, minimum=48)
    topic_x = x + 18 + mode_width + 12
    topic_right = x + w - 18 - page_width - 12
    topic_units = max(9.0, min(HEADER_COMPACT_TOPIC_MAX_UNITS, (topic_right - topic_x) / 18.0))
    topic = ellipsize_text(data["topic"], topic_units, suffix="…")
    return "\n".join(
        [
            '<g class="conversation-passport conversation-passport-compact">',
            f'<rect x="{x + 9}" y="{y + 6}" width="{w + 1}" height="{h + 7}" rx="20" fill="{BLACK}"/>',
            f'<rect x="{x + 6}" y="{y + 5}" width="{w}" height="{h + 2}" rx="20" fill="url(#headerAccent)"/>',
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="20" fill="{THEME["header_fill"]}" stroke="{BLACK}" stroke-width="4"/>',
            _mode_badge_svg(x + 18, y + 12, data["mode"], data["mode_label"], compact=True),
            _render_inline_text_svg(
                topic,
                topic_x,
                y + 35,
                font_size=18,
                font_weight=900,
                fill=BLACK,
                class_name="conversation-topic",
            ),
            _small_badge_svg(x + w - 18 - page_width, y + 12, page_width, page_label, "#F1E5FF", "page-badge"),
            _identity_route_svg(
                x + 18,
                y + 50,
                w - 36,
                data["peer_identity"],
                data["local_identity"],
                data["initiated_by"],
                compact=True,
            ),
            "</g>",
        ]
    )


def _render_context_cards(
    x: float,
    y: float,
    width: float,
    blocks: list[dict[str, str]],
) -> str:
    visible = blocks[:2]
    return "\n".join(
        _render_context_card(
            x,
            y + index * (CONTEXT_CARD_HEIGHT + CONTEXT_CARD_GAP),
            width,
            block,
        )
        for index, block in enumerate(visible)
    )


def _render_context_card(
    x: float,
    y: float,
    width: float,
    block: dict[str, str],
) -> str:
    kind = str(block.get("kind") or "profile")
    label = str(block.get("label") or "Profile").strip().upper()
    text = " ".join(str(block.get("text") or "").split())
    label_width = 132.0
    divider_x = x + label_width
    content_x = divider_x + 14
    content_width = max(48.0, x + width - 14 - content_x)
    lines = _bounded_context_lines(text, content_width)
    label_lines = _context_field_label_lines(kind, label)
    class_kind = "".join(char.lower() if char.isalnum() else "-" for char in kind).strip("-") or "profile"
    accent = THEME["world_badge"] if kind == "worldContext" else THEME["left_label"]
    accessible_text = ellipsize_text(text, 120.0, suffix="…")
    accessible = f"{label}: {accessible_text}" if accessible_text else label

    parts = [
        f'<g class="passport-context-field context-{class_kind}" role="group" aria-label="{esc(accessible)}">',
        f"<title>{esc(accessible)}</title>",
        f'<rect x="{x + 3:.1f}" y="{y + 3:.1f}" width="{width:.1f}" height="{CONTEXT_CARD_HEIGHT}" rx="15" fill="{BLACK}"/>',
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{CONTEXT_CARD_HEIGHT}" rx="15" fill="{THEME["passport_strip"]}" stroke="{BLACK}" stroke-width="2.5"/>',
        f'<rect x="{x + 6:.1f}" y="{y + 13:.1f}" width="6" height="28" rx="3" fill="{accent}"/>',
        _context_field_icon_svg(x + 36, y + CONTEXT_CARD_HEIGHT / 2, kind),
        f'<line x1="{divider_x:.1f}" y1="{y + 8:.1f}" x2="{divider_x:.1f}" y2="{y + CONTEXT_CARD_HEIGHT - 8:.1f}" stroke="{BLACK}" stroke-width="2" stroke-dasharray="3 3" opacity="0.45"/>',
    ]
    label_max_units = max(4.0, (label_width - 48) / CONTEXT_LABEL_FONT_SIZE)
    label_start_y = y + (22 if len(label_lines) > 1 else 31)
    for index, label_line in enumerate(label_lines):
        parts.append(
            _render_inline_text_svg(
                ellipsize_text(label_line, label_max_units, suffix="…"),
                x + 84,
                label_start_y + index * 16,
                font_size=CONTEXT_LABEL_FONT_SIZE,
                font_weight=900,
                fill=BLACK,
                anchor="middle",
                class_name=(
                    "context-field-label context-field-label-primary"
                    if index == 0
                    else "context-field-label context-field-label-secondary"
                ),
            )
        )
    content_start_y = (
        y
        + CONTEXT_CARD_HEIGHT / 2
        + CONTEXT_TEXT_BASELINE_CENTER_OFFSET
        - max(0, len(lines) - 1) * CONTEXT_TEXT_LINE_HEIGHT / 2
    )
    for index, line in enumerate(lines):
        parts.append(
            _render_inline_text_svg(
                line,
                content_x,
                content_start_y + index * CONTEXT_TEXT_LINE_HEIGHT,
                font_size=CONTEXT_TEXT_FONT_SIZE,
                font_weight=800,
                fill=THEME["muted"],
                class_name="conversation-context context-field-text",
            )
        )
    parts.append("</g>")
    return "\n".join(parts)


def _context_field_label_lines(kind: str, label: str) -> list[str]:
    semantic_labels = {
        "peerGlobalProfile": ["PEER", "PROFILE"],
        "peerWorldMembershipProfile": ["PEER", "WORLD"],
        "worldContext": ["WORLD", "CONTEXT"],
    }
    if kind in semantic_labels:
        return semantic_labels[kind]
    words = [word for word in label.replace("·", " ").split() if word]
    if len(words) <= 1:
        return words or ["PROFILE"]
    return [words[0], " ".join(words[1:])]


def _context_field_icon_svg(cx: float, cy: float, kind: str) -> str:
    if kind == "worldContext":
        return _context_icon_svg(cx, cy, "world")
    fill = THEME["left_label"]
    return "\n".join(
        [
            '<g class="context-icon context-icon-profile">',
            f'<circle cx="{cx:.1f}" cy="{cy - 6.5:.1f}" r="4.7" fill="{fill}" stroke="{BLACK}" stroke-width="1.9"/>',
            f'<path d="M{cx - 8:.1f} {cy + 9:.1f} C{cx - 8:.1f} {cy + 2:.1f} {cx - 4:.1f} {cy - 0.5:.1f} {cx:.1f} {cy - 0.5:.1f} C{cx + 4:.1f} {cy - 0.5:.1f} {cx + 8:.1f} {cy + 2:.1f} {cx + 8:.1f} {cy + 9:.1f} Z" fill="{fill}" stroke="{BLACK}" stroke-width="1.9" stroke-linejoin="round"/>',
            "</g>",
        ]
    )


def _bounded_context_lines(text: str, content_width: float) -> list[str]:
    value = " ".join(str(text or "").split())
    if not value:
        return []
    max_units = max(4.0, content_width / CONTEXT_TEXT_FONT_SIZE)
    lines = wrap_text(value, max_units)
    visible = lines[:CONTEXT_TEXT_MAX_LINES]
    if len(lines) > CONTEXT_TEXT_MAX_LINES and visible:
        suffix = "…"
        last = ellipsize_text(
            visible[-1],
            max(0.0, max_units - text_units(suffix)),
            suffix="",
        ).rstrip()
        visible[-1] = f"{last}{suffix}" if last else suffix
    return visible


def _header_height(*, compact: bool, header: Any | None = None, subtitle: str = "") -> int:
    if compact:
        card_height = HEADER_CARD_HEIGHT_COMPACT
    else:
        card_height = _full_header_card_height(
            _header_context_blocks(header, fallback_text=subtitle if header is None else "")
        )
    return HEADER_Y + card_height + HEADER_BOTTOM_PAD


def _full_header_card_height(context_blocks: list[dict[str, str]]) -> int:
    count = min(2, len(context_blocks))
    if count == 0:
        return HEADER_CARD_HEIGHT_NO_CONTEXT
    content_height = count * CONTEXT_CARD_HEIGHT + (count - 1) * CONTEXT_CARD_GAP
    return min(HEADER_CARD_HEIGHT_FULL, 156 + content_height + 14)


def _passport_data(page: LayoutPage) -> dict[str, Any]:
    header = getattr(page, "header", None)
    mode = _header_value(header, "chat_mode", "chatMode", "mode").lower()
    if mode not in {"direct", "world"}:
        mode = "chat"
    mode_label = f"{mode.upper()} · 1:1"
    topic = _header_value(header, "topic") or _clean_header_title(page.title)
    world_name = _header_value(header, "world_name", "worldName")
    local_identity = _header_value(header, "local_identity", "localIdentity")
    peer_identity = _header_value(header, "peer_identity", "peerIdentity")
    initiated_by = _normalize_initiated_by(
        _header_value(
            header,
            "initiated_by",
            "initiatedBy",
            "initiator",
            "request_direction",
            "requestDirection",
        )
    )
    participants = _participants_accessible_text(peer_identity, local_identity, initiated_by)
    context_blocks = _header_context_blocks(
        header,
        fallback_text=str(page.subtitle or "").strip() if header is None else "",
    )
    context = " · ".join(
        f'{block["label"]}: {ellipsize_text(block["text"], 90.0, suffix="…")}'
        if block["label"]
        else ellipsize_text(block["text"], 90.0, suffix="…")
        for block in context_blocks
    )
    report_type = _header_value(header, "report_type", "reportType").lower()
    date_label = _header_value(header, "date_label", "dateLabel")
    message_count = _header_value(header, "message_count", "messageCount")
    count_label = _message_count_label(message_count)
    meta = " · ".join(value for value in (date_label, count_label) if value)
    return {
        "mode": mode,
        "mode_label": mode_label,
        "topic": topic or "Claworld conversation",
        "world_name": world_name,
        "participants": participants,
        "peer_identity": peer_identity or "UNKNOWN",
        "local_identity": local_identity or "UNKNOWN",
        "initiated_by": initiated_by,
        "context": context,
        "context_blocks": context_blocks,
        "report_type": report_type,
        "count_label": count_label,
        "meta": meta,
    }


def _header_context_blocks(header: Any, *, fallback_text: str = "") -> list[dict[str, str]]:
    raw_blocks: Any = None
    for name in ("context_blocks", "contextBlocks"):
        if isinstance(header, dict):
            raw_blocks = header.get(name)
        elif header is not None:
            raw_blocks = getattr(header, name, None)
        if raw_blocks:
            break

    blocks: list[dict[str, str]] = []
    if isinstance(raw_blocks, (list, tuple)):
        for item in raw_blocks:
            kind = _header_value(item, "kind")
            label = _header_value(item, "label").rstrip("：: ")
            text = _header_value(item, "text")
            source = _header_value(item, "source")
            if text:
                blocks.append({"kind": kind or "profile", "label": label, "text": text, "source": source})

    if not blocks:
        label = _header_value(header, "context_label", "contextLabel").rstrip("：: ")
        text = _header_value(header, "context_text", "contextText")
        source = _header_value(header, "context_source", "contextSource")
        if text:
            blocks.append({"kind": "profile", "label": label, "text": text, "source": source})
        world_context = _header_value(
            header,
            "world_context_text",
            "worldContextText",
            "worldContext",
        )
        if world_context:
            blocks.append(
                {
                    "kind": "worldContext",
                    "label": "World Context",
                    "text": world_context,
                    "source": _header_value(header, "world_context_source", "worldContextSource"),
                }
            )

    if not blocks and fallback_text:
        blocks.append({"kind": "profile", "label": "Profile", "text": fallback_text, "source": "fallback"})
    return blocks[:2]


def _header_value(header: Any, *names: str) -> str:
    for name in names:
        if isinstance(header, dict):
            value = header.get(name)
        else:
            value = getattr(header, name, None) if header is not None else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_initiated_by(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized in {"peer", "inbound", "remote", "from-peer"}:
        return "peer"
    if normalized in {"local", "me", "outbound", "from-local"}:
        return "local"
    return ""


def _message_count_label(value: str) -> str:
    """Return a compact English count badge, hiding malformed counts."""

    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if count < 0:
        return ""
    unit = "MSG" if count == 1 else "MSGS"
    return f"{count} {unit}"


def _participants_accessible_text(peer_identity: str, local_identity: str, initiated_by: str) -> str:
    peer = peer_identity or "Peer"
    local = local_identity or "Me"
    if initiated_by == "peer":
        return f"{peer} initiated a conversation with {local}"
    if initiated_by == "local":
        return f"{local} initiated a conversation with {peer}"
    return f"Conversation between {peer} and {local}; initiator unknown"


def _identity_route_svg(
    x: float,
    y: float,
    width: float,
    peer_identity: str,
    local_identity: str,
    initiated_by: str,
    *,
    compact: bool = False,
) -> str:
    """Render a fixed peer-left / local-right route without guessing initiator.

    The arrow is pinned to the route center. Each identity's text is centered
    independently in the remaining half, while its color dot follows the
    measured left edge of the (possibly truncated) text.
    """

    height = 36 if compact else 40
    gap = 8 if compact else 12
    center_width = 42 if compact else 46
    route_center = x + width / 2
    center_x = route_center - center_width / 2
    peer_width = max(48.0, center_x - gap - x)
    local_x = center_x + center_width + gap
    local_width = max(48.0, x + width - local_x)
    relation_label, accessible_relation = {
        "peer": ("→", "Peer initiated the conversation with Me"),
        "local": ("←", "Me initiated the conversation with Peer"),
    }.get(initiated_by, ("↔", "The conversation initiator is unknown"))
    accessible = f"{accessible_relation}. Peer: {peer_identity or 'Peer'}. Me: {local_identity or 'Me'}."

    relation_height = 24 if compact else 26
    relation_y = y + (height - relation_height) / 2
    return "\n".join(
        [
            f'<g class="conversation-participants identity-route" role="img" aria-label="{esc(accessible)}">',
            f"<title>{esc(accessible)}</title>",
            _identity_label_svg(
                x,
                y,
                peer_width,
                peer_identity or "UNKNOWN",
                dot_fill=THEME["left_label"],
                class_name="identity-peer",
                compact=compact,
            ),
            f'<rect x="{center_x + 2:.1f}" y="{relation_y + 3:.1f}" width="{center_width:.1f}" height="{relation_height}" rx="{relation_height / 2:.1f}" fill="{BLACK}"/>',
            f'<rect class="conversation-relation relation-{initiated_by or "unknown"}" x="{center_x:.1f}" y="{relation_y:.1f}" width="{center_width:.1f}" height="{relation_height}" rx="{relation_height / 2:.1f}" fill="#FFFFFF" stroke="{BLACK}" stroke-width="2"/>',
            _render_inline_text_svg(
                relation_label,
                center_x + center_width / 2,
                relation_y + (17 if compact else 19),
                font_size=15 if compact else 17,
                font_weight=900,
                fill=BLACK,
                anchor="middle",
                class_name="conversation-relation-label",
            ),
            _identity_label_svg(
                local_x,
                y,
                local_width,
                local_identity or "UNKNOWN",
                dot_fill=THEME["right_label"],
                class_name="identity-local",
                compact=compact,
            ),
            "</g>",
        ]
    )


def _identity_label_svg(
    x: float,
    y: float,
    width: float,
    identity: str,
    *,
    dot_fill: str,
    class_name: str,
    compact: bool,
) -> str:
    """Render a centered public identity with a dynamically positioned dot."""

    height = 36 if compact else 40
    dot_y = y + height / 2
    radius = 5 if compact else 6
    dot_gap = 3
    identity_font_size = IDENTITY_COMPACT_NAME_FONT_SIZE if compact else IDENTITY_NAME_FONT_SIZE
    code_font_size = IDENTITY_COMPACT_CODE_FONT_SIZE if compact else IDENTITY_CODE_FONT_SIZE
    identity_y = y + (28 if compact else 30)
    # The text itself stays centered in its half. Reserve the same amount on
    # both sides for the left-hand dot so the entire group remains in-bounds.
    dot_reserve = radius * 2 + dot_gap + 2
    available_px = max(18.0, width - dot_reserve * 2)
    _raw_name, raw_code = _split_identity(identity)
    code_gap = IDENTITY_CODE_GAP if raw_code else 0.0
    visible_name, visible_code = _ellipsize_identity_parts(
        identity,
        max(1.0, available_px / IDENTITY_WIDTH_SAFETY - code_gap),
        name_font_size=identity_font_size,
        code_font_size=code_font_size,
    )
    name_width = text_units(visible_name) * identity_font_size
    code_width = text_units(visible_code) * code_font_size
    visible_gap = IDENTITY_CODE_GAP if visible_code else 0.0
    text_center_x = x + width / 2
    if visible_code:
        combined_width = name_width + visible_gap + code_width
        divider_x = text_center_x - combined_width / 2 + name_width
        name_left_x = divider_x - name_width
    else:
        name_left_x = text_center_x - name_width / 2

    # Follow the display name itself, not the wider name + code run. SVG text
    # anchored at ``end`` uses the font's shaped width, which is a little wider
    # than ``text_units`` for Latin bold faces, so use a script-aware width for
    # the dot while keeping wide CJK/emoji runs close to their measured edge.
    conservative_name_width = _identity_name_render_width(
        visible_name,
        identity_font_size,
    )
    measured_name_left_x = name_left_x - (conservative_name_width - name_width)
    dot_x = measured_name_left_x - dot_gap - radius
    identity_svg = _render_identity_text_svg(
        visible_name,
        visible_code,
        text_center_x,
        identity_y,
        name_font_size=identity_font_size,
        code_font_size=code_font_size,
        class_name=class_name,
    )
    return "\n".join(
        [
            f'<g class="identity-label {class_name}">',
            f"<title>{esc(identity)}</title>",
            f'<circle cx="{dot_x:.1f}" cy="{dot_y:.1f}" r="{radius}" fill="{dot_fill}" stroke="{BLACK}" stroke-width="2"/>',
            identity_svg,
            "</g>",
        ]
    )


def _identity_name_render_width(name: str, font_size: int) -> float:
    """Conservatively approximate the shaped width of a bold identity name."""

    total_units = 0.0
    for run, script in text_runs(name):
        if script == "default":
            total_units += sum(_identity_default_glyph_units(char) for char in run)
        elif script in {"cjk", "japanese", "korean", "emoji"}:
            total_units += text_units(run) * 1.02
        else:
            total_units += text_units(run) * 1.16
    return total_units * font_size


def _identity_default_glyph_units(char: str) -> float:
    """Approximate the heavy UI face used by resvg more closely than 0.55em."""

    if char.isspace():
        return 0.25
    if char == "…":
        return 1.0
    if char == "M":
        return 0.90
    if char == "W":
        return 0.96
    if char == "m":
        return 1.0
    if char == "w":
        return 0.93
    if char in "Iilj":
        return 0.35
    if char in "ft":
        return 0.44
    if char == "r":
        return 0.48
    if char in "csyz":
        return 0.55
    if char in "OQHNUDG":
        return 0.80
    if char.isupper():
        return 0.72
    if char.islower():
        return 0.63
    if char.isdigit():
        return 0.62
    return max(0.5, text_units(char) * 1.08)


def _render_identity_text_svg(
    name: str,
    code: str,
    x: float,
    y: float,
    *,
    name_font_size: int,
    code_font_size: int,
    class_name: str,
) -> str:
    """Render identity parts around a shared divider without SVG tspans.

    The bold name ends at the divider and the smaller code begins just after
    it, so each side's real glyph shaping cannot overlap the other. The
    existing inline renderer keeps mixed scripts and emoji in independent
    text nodes, which is required by the supported usvg/resvg versions.
    """

    name_width = text_units(name) * name_font_size
    if not code:
        return _render_inline_text_svg(
            name,
            x,
            y,
            font_size=name_font_size,
            font_weight=900,
            fill=BLACK,
            anchor="middle",
            class_name=f"{class_name}-name identity-name identity-text",
        )

    code_width = text_units(code) * code_font_size
    code_gap = IDENTITY_CODE_GAP
    combined_width = name_width + code_gap + code_width
    divider_x = x - combined_width / 2 + name_width
    name_svg = _render_inline_text_svg(
        name,
        divider_x,
        y,
        font_size=name_font_size,
        font_weight=900,
        fill=BLACK,
        anchor="end",
        class_name=f"{class_name}-name identity-name identity-text",
    )
    code_svg = _render_inline_text_svg(
        code,
        divider_x + code_gap,
        y,
        font_size=code_font_size,
        font_weight=800,
        fill="#68645F",
        class_name=f"{class_name}-code identity-code identity-text",
    )
    return f"{name_svg}\n{code_svg}"


def _ellipsize_identity_parts(
    identity: str,
    max_width: float,
    *,
    name_font_size: int,
    code_font_size: int,
) -> tuple[str, str]:
    """Fit a public identity while preserving a valid trailing ``#CODE``.

    Width is measured in pixels because the name and code intentionally use
    different font sizes. In normal public identities the complete code is
    retained and truncation is applied to the display name first.
    """

    name, code = _split_identity(identity)
    code_width = text_units(code) * code_font_size
    name_width = text_units(name) * name_font_size
    if name_width + code_width <= max_width:
        return name, code

    if code:
        name_budget = max_width - code_width
        ellipsis_width = text_units("…") * name_font_size
        if name_budget >= ellipsis_width:
            visible_name = ellipsize_text(
                name,
                name_budget / name_font_size,
                suffix="…",
            )
            return visible_name, code

    # A pathological code can itself exceed the column. Fall back to one
    # safely bounded run rather than allowing it to cross the center route.
    value = str(identity or "").strip()
    return ellipsize_text(value, max_width / name_font_size, suffix="…"), ""


def _split_identity(identity: str) -> tuple[str, str]:
    """Split a trailing whitespace-free public identity code from its name."""

    value = str(identity or "").strip()
    name, marker, code = value.rpartition("#")
    if marker and name.strip() and code and not any(ch.isspace() for ch in code):
        return name.strip(), f"#{code}"
    return value, ""


def _ellipsize_identity(identity: str, max_units: float) -> str:
    """Truncate the display name first so a public ``#CODE`` stays visible."""

    value = str(identity or "").strip()
    if text_units(value) <= max_units:
        return value
    name, marker, code = value.rpartition("#")
    suffix = f"#{code}" if marker and name.strip() and code and not any(ch.isspace() for ch in code) else ""
    if not suffix or text_units(suffix) + text_units("…") >= max_units:
        return ellipsize_text(value, max_units, suffix="…")
    name_budget = max_units - text_units(suffix)
    return ellipsize_text(name.strip(), name_budget, suffix="…") + suffix


def _clean_header_title(title: str) -> str:
    clean = str(title or "").strip()
    return clean[1:] if clean.startswith("@") else clean


def _topic_lines(topic: str, *, max_units: float = HEADER_TOPIC_MAX_UNITS) -> list[str]:
    lines = _wrap_topic_text(str(topic or "").strip(), max_units)
    if len(lines) <= HEADER_TOPIC_MAX_LINES:
        return lines or ["Claworld conversation"]
    visible = lines[: HEADER_TOPIC_MAX_LINES - 1]
    remainder = " ".join(line.strip() for line in lines[HEADER_TOPIC_MAX_LINES - 1 :] if line.strip())
    visible.append(_ellipsize_topic_text(remainder, max_units, suffix="…"))
    return visible


def _wrap_topic_text(text: str, max_units: float) -> list[str]:
    """Wrap a heavy title using conservative shaped-width estimates."""

    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for token in wrap_tokens(paragraph):
            if token.isspace():
                if current and _topic_render_units(current + " ") <= max_units:
                    current += " "
                continue
            if current and _topic_render_units(current + token) > max_units:
                lines.append(current.rstrip())
                current = ""
            if _topic_render_units(token) > max_units:
                for cluster in grapheme_clusters(token):
                    if current and _topic_render_units(current + cluster) > max_units:
                        lines.append(current.rstrip())
                        current = ""
                    current += cluster
            else:
                current += token
        if current or not lines:
            lines.append(current.rstrip())
    return lines


def _ellipsize_topic_text(text: str, max_units: float, *, suffix: str) -> str:
    value = str(text or "")
    if _topic_render_units(value) <= max_units:
        return value
    allowed = max(0.0, max_units - _topic_render_units(suffix))
    kept = ""
    for cluster in grapheme_clusters(value):
        if _topic_render_units(kept + cluster) > allowed:
            break
        kept += cluster
    return kept.rstrip() + suffix


def _topic_render_units(text: str) -> float:
    return _identity_name_render_width(text, TITLE_FONT_SIZE) / TITLE_FONT_SIZE


def _page_label(page: LayoutPage) -> str:
    total = getattr(page, "page_count", 1) or 1
    return f"{page.page} / {total}"


def _mode_badge_width(label: str, *, compact: bool = False) -> int:
    font_size = 11 if compact else 12
    horizontal_pad = 20 if compact else 24
    return max(92 if compact else 104, int(text_units(label) * font_size + horizontal_pad))


def _small_badge_width(label: str, *, minimum: int) -> int:
    return max(minimum, int(text_units(label) * 12 + 24))


def _mode_badge_svg(x: float, y: float, mode: str, label: str, *, compact: bool = False) -> str:
    w = _mode_badge_width(label, compact=compact)
    h = 26 if compact else 28
    fill = {
        "direct": THEME["direct_badge"],
        "world": THEME["world_badge"],
    }.get(mode, THEME["chat_badge"])
    return "\n".join(
        [
            f'<g class="mode-badge mode-{esc(mode)}">',
            f'<rect x="{x + 3:.1f}" y="{y + 3:.1f}" width="{w}" height="{h}" rx="{h / 2:.1f}" fill="{BLACK}"/>',
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" rx="{h / 2:.1f}" fill="{fill}" stroke="{BLACK}" stroke-width="2.5"/>',
            _render_inline_text_svg(
                label,
                x + w / 2,
                y + (17.5 if compact else 19),
                font_size=11 if compact else 12,
                font_weight=900,
                fill=BLACK,
                anchor="middle",
            ),
            "</g>",
        ]
    )


def _secondary_badge_svg(x: float, y: float, width: float, label: str) -> str:
    clipped = _ellipsize_topic_text(label, max(4.0, (width - 22) / 12.0), suffix="…")
    return "\n".join(
        [
            '<g class="world-name-badge">',
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="25" rx="12.5" fill="#FFFFFF" fill-opacity="0.72" stroke="{BLACK}" stroke-width="2" stroke-dasharray="4 3"/>',
            _render_inline_text_svg(
                clipped,
                x + width / 2,
                y + 17,
                font_size=12,
                font_weight=800,
                fill=THEME["muted"],
                anchor="middle",
            ),
            "</g>",
        ]
    )


def _small_badge_svg(
    x: float,
    y: float,
    width: float,
    label: str,
    fill: str,
    class_name: str,
    *,
    accessible_label: str = "",
) -> str:
    aria = f' role="img" aria-label="{esc(accessible_label)}"' if accessible_label else ""
    title = f"<title>{esc(accessible_label)}</title>" if accessible_label else ""
    return "\n".join(
        [
            f'<g class="{class_name}"{aria}>',
            title,
            f'<rect x="{x + 2:.1f}" y="{y + 3:.1f}" width="{width:.1f}" height="26" rx="13" fill="{BLACK}"/>',
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="26" rx="13" fill="{fill}" stroke="{BLACK}" stroke-width="2"/>',
            _render_inline_text_svg(
                label,
                x + width / 2,
                y + 18,
                font_size=12,
                font_weight=900,
                fill=BLACK,
                anchor="middle",
            ),
            "</g>",
        ]
    )


def _connection_icon_svg(x: float, y: float, *, compact: bool = False) -> str:
    radius = 3.5 if compact else 4.5
    gap = 11 if compact else 14
    stroke = 2 if compact else 2.5
    return "\n".join(
        [
            '<g class="participants-icon">',
            f'<line x1="{x + radius:.1f}" y1="{y:.1f}" x2="{x + gap - radius:.1f}" y2="{y:.1f}" stroke="{BLACK}" stroke-width="{stroke}" stroke-linecap="round"/>',
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="#62E69D" stroke="{BLACK}" stroke-width="2"/>',
            f'<circle cx="{x + gap:.1f}" cy="{y:.1f}" r="{radius}" fill="#B785FF" stroke="{BLACK}" stroke-width="2"/>',
            "</g>",
        ]
    )


def _context_icon_svg(cx: float, cy: float, mode: str) -> str:
    fill = THEME["world_badge"] if mode == "world" else THEME["direct_badge"]
    if mode == "world":
        return "\n".join(
            [
                '<g class="context-icon context-icon-world">',
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="8.2" fill="{fill}" stroke="{BLACK}" stroke-width="2"/>',
                f'<path d="M{cx - 7.1:.1f} {cy:.1f} H{cx + 7.1:.1f} M{cx:.1f} {cy - 7.1:.1f} C{cx - 3.5:.1f} {cy - 2.4:.1f} {cx - 3.5:.1f} {cy + 2.4:.1f} {cx:.1f} {cy + 7.1:.1f} M{cx:.1f} {cy - 7.1:.1f} C{cx + 3.5:.1f} {cy - 2.4:.1f} {cx + 3.5:.1f} {cy + 2.4:.1f} {cx:.1f} {cy + 7.1:.1f}" fill="none" stroke="{BLACK}" stroke-width="1.3" stroke-linecap="round"/>',
                "</g>",
            ]
        )
    return "\n".join(
        [
            '<g class="context-icon context-icon-direct">',
            f'<path d="M{cx - 7:.1f} {cy - 5:.1f} H{cx + 7:.1f} V{cy + 4:.1f} H{cx + 1:.1f} L{cx - 3:.1f} {cy + 8:.1f} V{cy + 4:.1f} H{cx - 7:.1f} Z" fill="{fill}" stroke="{BLACK}" stroke-width="2" stroke-linejoin="round"/>',
            "</g>",
        ]
    )


def _mode_emblem_svg(cx: float, cy: float, mode: str) -> str:
    if mode == "world":
        return "\n".join(
            [
                '<g class="mode-emblem mode-emblem-world">',
                f'<circle class="mode-emblem-shadow mode-emblem-world-shadow" cx="{cx + 2:.1f}" cy="{cy + 2.5:.1f}" r="17" fill="{BLACK}"/>',
                f'<ellipse class="mode-emblem-orbit mode-emblem-orbit-back" cx="{cx:.1f}" cy="{cy:.1f}" rx="25" ry="8" fill="none" stroke="url(#modeOrbitGradient)" stroke-width="5" transform="rotate(-13 {cx:.1f} {cy:.1f})"/>',
                '<g class="mode-emblem-globe">',
                f'<circle class="mode-emblem-planet-shell" cx="{cx:.1f}" cy="{cy:.1f}" r="17" fill="#FFFFFF" stroke="{BLACK}" stroke-width="3"/>',
                f'<circle class="mode-emblem-planet-core" cx="{cx:.1f}" cy="{cy:.1f}" r="11" fill="{THEME["world_badge"]}" stroke="{BLACK}" stroke-width="2.5"/>',
                "</g>",
                f'<path class="mode-emblem-orbit mode-emblem-orbit-front" d="M{cx - 25:.1f} {cy:.1f} A25 8 0 0 0 {cx + 25:.1f} {cy:.1f}" fill="none" stroke="url(#modeOrbitGradient)" stroke-width="5" transform="rotate(-13 {cx:.1f} {cy:.1f})"/>',
                "</g>",
            ]
        )
    if mode == "direct":
        translate_x = cx - 32
        translate_y = cy - 26
        return "\n".join(
            [
                f'<g class="mode-emblem mode-emblem-direct" transform="translate({translate_x:.1f} {translate_y:.1f})">',
                '<g class="mode-emblem-shadow mode-emblem-direct-shadow">',
                f'<path class="mode-emblem-direct-shadow-back" d="M8 15.7883L8.5 14.5L36 15.7883V38.5H23L15 46.5V38.5H8V15.7883Z" fill="{BLACK}"/>',
                f'<path class="mode-emblem-direct-shadow-front" d="M32 23H43.5L59.5 23.5L60 25V44.8772H52V52L44 44.8772H35L32 42V23Z" fill="{BLACK}"/>',
                "</g>",
                f'<path class="mode-emblem-chat-bubble mode-emblem-chat-bubble-back" d="M10 15H38V34H25L17 42V34H10V15Z" fill="#FFFFFF" stroke="{BLACK}" stroke-width="3" stroke-linejoin="round"/>',
                f'<path class="mode-emblem-chat-bubble mode-emblem-chat-bubble-front" d="M33 24H58V41H50V48L42 41H33V24Z" fill="{THEME["direct_badge"]}" stroke="{BLACK}" stroke-width="3" stroke-linejoin="round"/>',
                "</g>",
            ]
        )
    return _decorative_star_svg(cx, cy, 17, "#FFFFFF", "url(#headerAccent)")


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
    # Kept for callers that imported the old helper.  Topics and World names
    # are not handles, so the passport must never invent a leading @.
    return _clean_header_title(title) or "Claworld conversation"


def _label_text(label: str) -> str:
    return clip_display(_body_participant_name(label).upper(), LABEL_MAX_COLS)


def _body_participant_name(label: str) -> str:
    """Hide a trailing public identity code in bubbles; the header keeps it."""

    value = str(label or "AGENT").strip()
    name, marker, code = value.rpartition("#")
    if marker and name.strip() and code and not any(ch.isspace() for ch in code):
        return name.strip()
    return value


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
            '<linearGradient id="modeOrbitGradient" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" stop-color="#8F72FF"/><stop offset="52%" stop-color="#FF4EB4"/><stop offset="100%" stop-color="#FF963D"/></linearGradient>',
            '<linearGradient id="leftAccent" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#58E58F"/><stop offset="100%" stop-color="#47B6FF"/></linearGradient>',
            '<linearGradient id="rightAccent" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#A871FF"/><stop offset="100%" stop-color="#FF4EB4"/></linearGradient>',
            '<filter id="comicLift" x="-6%" y="-16%" width="112%" height="132%"><feDropShadow dx="0" dy="2" stdDeviation="1.2" flood-color="#000000" flood-opacity="0.14"/></filter>',
            "</defs>",
        ]
    )


def _page_text_values(page: LayoutPage) -> list[str]:
    values = [page.title, page.subtitle, page.footer]
    passport = _passport_data(page)
    values.extend(str(value) for value in passport.values() if isinstance(value, str))
    for block in passport["context_blocks"]:
        values.extend((block["label"], ellipsize_text(block["text"], 120.0, suffix="…")))
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
