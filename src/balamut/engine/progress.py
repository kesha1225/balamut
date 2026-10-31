"""Progress of a campaign over the terminal verdicts of its executed work."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import IO

from balamut.engine.results import MutantStatus


class ProgressMode(Enum):
    AUTO = "auto"
    TTY = "tty"
    COMPACT = "compact"
    NONE = "none"


def _stderr() -> IO[str]:
    return sys.stderr


@dataclass(slots=True)
class CampaignProgress:
    """Thread-safe progress over the terminal verdicts of the executed work."""

    total: int
    mode: ProgressMode
    stream: IO[str] = field(default_factory=_stderr)
    on_advance: Callable[[MutantStatus, str], None] | None = None
    on_hang: Callable[[str], None] | None = None
    compact_interval: float = 5.0
    clock: Callable[[], float] = time.monotonic
    done: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    _effective_mode: ProgressMode = field(init=False)
    _last_compact_emit: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("the progress total cannot be negative")
        if self.compact_interval <= 0:
            raise ValueError("the compact progress interval must be positive")
        if self.mode is ProgressMode.AUTO:
            self._effective_mode = ProgressMode.TTY if self.stream.isatty() else ProgressMode.COMPACT
        else:
            self._effective_mode = self.mode

    @property
    def effective_mode(self) -> ProgressMode:
        """The mode after resolving ``auto`` for the current stream."""
        return self._effective_mode

    def advance(self, label: str, status: MutantStatus, key: str = "") -> None:
        """Record exactly one terminal result and print progress when due; ``key`` weighs the work for the ETA."""
        with self.lock:
            if self.done >= self.total:
                raise RuntimeError("the progress received more terminal results than planned")
            next_done = self.done + 1
            if self._effective_mode is ProgressMode.TTY:
                suffix = " (timeout)" if status is MutantStatus.TIMEOUT else ""
                self.stream.write(f"\r  {next_done}/{self.total}  {label}{suffix}    ")
                self.stream.flush()
            elif self._effective_mode is ProgressMode.COMPACT:
                now = self.clock()
                if self._last_compact_emit is None or now - self._last_compact_emit >= self.compact_interval:
                    self.stream.write(f"progress: {next_done}/{self.total} {label} {status.value}\n")
                    self.stream.flush()
                    self._last_compact_emit = now
            if self.on_advance is not None:
                self.on_advance(status, key)
            self.done = next_done

    def report_hang(self, key: str) -> None:
        """A mutant hangs and goes to an isolated retry: tell the observer (not a terminal result)."""
        if self.on_hang is not None:
            self.on_hang(key)
