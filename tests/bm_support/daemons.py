"""Stand-ins for daemon processes and pools in engine tests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from balamut.engine.daemon import Daemon


class FakeProcess:
    """A daemon process that never ran: enough for code that only signals or waits on it."""

    pid = 0
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self.returncode or 0


def fake_daemon(
    tmp_path: Path, *, address: str = "sock", index: int = 0, workers: int = 1, persist_workers: bool = True
) -> Daemon:
    mutant_file = tmp_path / f"mutant-{index}.json"
    mutant_file.write_text("", encoding="utf-8")
    return Daemon(
        address=address,
        mutant_file=mutant_file,
        proc=FakeProcess(),
        log=BytesIO(),
        workers=workers,
        env={},
        persist_workers=persist_workers,
    )
