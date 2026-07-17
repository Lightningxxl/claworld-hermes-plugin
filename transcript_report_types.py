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


@dataclass(frozen=True)
class TranscriptContextBlock:
    """One public context fact shown in the transcript passport."""

    kind: str
    label: str
    text: str
    source: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "label": self.label,
            "text": self.text,
            "source": self.source,
        }


@dataclass(frozen=True)
class TranscriptHeader:
    """Public, renderer-ready metadata for a transcript header.

    Empty strings intentionally mean "unknown / unavailable".  In particular,
    callers and renderers must not turn an unknown manual transcript into a
    direct chat or an excerpt unless that fact was supplied explicitly.
    """

    chat_mode: str = ""
    report_type: str = ""
    initiated_by: str = ""
    topic: str = ""
    world_name: str = ""
    local_identity: str = ""
    peer_identity: str = ""
    context_label: str = ""
    context_text: str = ""
    context_source: str = ""
    context_blocks: tuple[TranscriptContextBlock, ...] = ()
    date_label: str = ""
    message_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the stable camelCase representation used by BubbleSpec."""

        return {
            "chatMode": self.chat_mode,
            "reportType": self.report_type,
            "initiatedBy": self.initiated_by,
            "topic": self.topic,
            "worldName": self.world_name,
            "localIdentity": self.local_identity,
            "peerIdentity": self.peer_identity,
            "contextLabel": self.context_label,
            "contextText": self.context_text,
            "contextSource": self.context_source,
            "contextBlocks": [block.as_dict() for block in self.context_blocks],
            "dateLabel": self.date_label,
            "messageCount": self.message_count,
        }


@dataclass
class LayoutPage:
    page: int
    width: int
    height: int
    items: list[dict[str, Any]]
    title: str
    subtitle: str
    footer: str
    header: TranscriptHeader | None = None
    page_count: int = 1
