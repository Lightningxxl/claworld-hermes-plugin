"""Transcript report style registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..transcript_report_types import LayoutPage, MeasuredBubble


@dataclass(frozen=True)
class TranscriptReportStyle:
    name: str
    measure_item: Callable[[dict, int], MeasuredBubble]
    paginate: Callable[[list[MeasuredBubble], int, int, str, str], list[LayoutPage]]
    render_svg: Callable[[LayoutPage], str]
    write_png: Callable[[Path, Path, LayoutPage], dict]


def _styles() -> dict[str, TranscriptReportStyle]:
    from . import im_light, terminal_crt

    return {
        terminal_crt.STYLE.name: terminal_crt.STYLE,
        im_light.STYLE.name: im_light.STYLE,
    }


def available_style_names() -> list[str]:
    return ["claworld-terminal-crt", "claworld-im-light"]


def resolve_report_style(name: str | None) -> TranscriptReportStyle:
    requested = str(name or "claworld-terminal-crt").strip() or "claworld-terminal-crt"
    styles = _styles()
    if requested not in styles:
        allowed = ", ".join(available_style_names())
        raise ValueError(f"unsupported transcript report style: {requested}; expected one of {allowed}")
    return styles[requested]
