"""Shared transcript report data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranscriptMessage:
    id: str
    side: str
    participant_id: str
    participant_label: str
    text: str
    created_at: str = ""
    tags: list[str] = field(default_factory=list)
    source_index: int = 0


@dataclass
class MeasuredBubble:
    kind: str
    message: TranscriptMessage | None
    lines: list[str]
    width: int
    height: int
    meta_height: int = 18
    tag_height: int = 0
    text_height: int = 0
    omitted_count: int = 0
    label: str = ""


@dataclass
class LayoutPage:
    page: int
    width: int
    height: int
    items: list[dict[str, Any]]
    title: str
    subtitle: str
    footer: str
