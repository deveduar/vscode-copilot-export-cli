"""Small stderr progress reporter for long-running exports."""

from __future__ import annotations

import sys
from typing import Optional, TextIO


class Progress:
    def __init__(
        self,
        enabled: bool = False,
        stream: Optional[TextIO] = None,
    ) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._label = ""
        self._total = 0
        self._current = 0
        self._last_width = 0

    def _is_tty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except (AttributeError, OSError):
            return False

    def start(self, label: str, total: Optional[int] = None) -> None:
        self._label = label
        self._total = total or 0
        self._current = 0
        self._last_width = 0
        self._render()

    def step(self, detail: str = "") -> None:
        self._current += 1
        self._render(detail)

    def _render(self, detail: str = "") -> None:
        if not self.enabled:
            return
        text = self._label
        if self._total:
            text += f" {self._current}/{self._total}"
        elif self._current:
            text += f" {self._current}"
        if detail:
            text += f" {detail}"
        if self._is_tty():
            padding = " " * max(0, self._last_width - len(text))
            self.stream.write("\r" + text + padding)
            self.stream.flush()
            self._last_width = len(text)
            return
        if self._current <= 1 or self._current == self._total or self._current % 10 == 0:
            self.stream.write(text + "\n")
            self.stream.flush()

    def done(self) -> None:
        if not self.enabled:
            return
        if self._is_tty() and self._last_width:
            self.stream.write("\r" + " " * self._last_width + "\r")
            self.stream.flush()
        self._last_width = 0
