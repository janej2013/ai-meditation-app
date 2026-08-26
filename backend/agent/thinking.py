"""Nova's ``<thinking>`` tags, kept out of what the listener sees.

Nova's tool-use template makes the model narrate its reasoning inside these
tags before answering. It is not user-facing text and must not reach the
client or the checkpoint; Claude never emits them. Both engines stream
text through the same filter, so the two transcripts agree.
"""

from __future__ import annotations

import re

THINKING_OPEN = "<thinking>"
THINKING_CLOSE = "</thinking>"
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)


class ThinkingFilter:
    """Drops ``<thinking>...</thinking>`` from streamed text, safely across
    delta boundaries: a tag may arrive split over several chunks, so the
    filter holds back any suffix that could be the start of one."""

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""
        self._emitted_visible = False

    def delta(self, chunk: str) -> str:
        buf = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        while buf:
            if self._inside:
                end = buf.find(THINKING_CLOSE)
                if end == -1:
                    self._pending = _partial_suffix(buf, THINKING_CLOSE)
                    break
                buf = buf[end + len(THINKING_CLOSE) :]
                self._inside = False
                continue
            start = buf.find(THINKING_OPEN)
            if start == -1:
                self._pending = _partial_suffix(buf, THINKING_OPEN)
                out.append(buf[: len(buf) - len(self._pending)])
                break
            out.append(buf[:start])
            buf = buf[start + len(THINKING_OPEN) :]
            self._inside = True
        text = "".join(out)
        if not self._emitted_visible:
            # The reply proper starts after the tags; drop the whitespace
            # that separated them from it.
            text = text.lstrip()
            self._emitted_visible = bool(text)
        return text

    @staticmethod
    def clean(text: str) -> str:
        return _THINKING_RE.sub("", text).strip()


def strip_thinking(text: str) -> str:
    return ThinkingFilter.clean(text)


def _partial_suffix(buf: str, tag: str) -> str:
    """The longest suffix of ``buf`` that is a proper prefix of ``tag``."""
    for size in range(min(len(tag) - 1, len(buf)), 0, -1):
        if tag.startswith(buf[-size:]):
            return buf[-size:]
    return ""
