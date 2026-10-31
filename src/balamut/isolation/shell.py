"""Isolation without a provider (shared environment) and isolation delegated to project commands."""

from __future__ import annotations

import json
import subprocess

from balamut.isolation.base import Environ, EnvMap, EnvOverlays, IsolationError

type _JsonValue = bool | int | float | str | list[_JsonValue] | dict[str, _JsonValue] | None

SLOTS_ENV = "BALAMUT_SLOTS"
SLOT_PREFIX_ENV = "BALAMUT_SLOT_PREFIX"


class NoIsolation:
    """Every slot shares the caller's environment."""

    @property
    def isolated(self) -> bool:
        return False

    def prepare(self, count: int, _prefix: str, /) -> EnvOverlays:
        return tuple({} for _ in range(count))

    def cleanup(self, _count: int, _prefix: str, /) -> None:
        return None

    def probe(self) -> None:
        return None

    def fingerprint(self) -> str:
        return "none"


class ShellIsolation:
    """Delegate slot management to project commands.

    ``prepare`` runs with ``BALAMUT_SLOTS`` and ``BALAMUT_SLOT_PREFIX`` set and must print a JSON array of exactly
    that many objects mapping variable names to string values; ``cleanup`` runs with the same variables."""

    def __init__(self, prepare: tuple[str, ...], cleanup: tuple[str, ...], *, environ: Environ) -> None:
        super().__init__()
        self._prepare = prepare
        self._cleanup = cleanup
        self._environ = dict(environ)

    @property
    def isolated(self) -> bool:
        return True

    def _run(self, command: tuple[str, ...], count: int, prefix: str) -> str:
        env = {**self._environ, SLOTS_ENV: str(count), SLOT_PREFIX_ENV: prefix}
        try:
            result = subprocess.run(list(command), env=env, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise IsolationError(f"could not run {command[0]}: {exc}") from exc
        if result.returncode != 0:
            joined = " ".join(command)
            raise IsolationError(f"{joined} exited with {result.returncode}:\n{result.stdout}{result.stderr}")
        return result.stdout

    def prepare(self, count: int, prefix: str, /) -> EnvOverlays:
        output = self._run(self._prepare, count, prefix)
        try:
            parsed: _JsonValue = json.loads(output)
        except json.JSONDecodeError as exc:
            raise IsolationError(f"the prepare command must print a JSON array of {count} objects") from exc
        if not isinstance(parsed, list) or len(parsed) != count:
            raise IsolationError(f"the prepare command must print a JSON array of {count} objects")
        overlays: list[EnvMap] = []
        for entry in parsed:
            if not isinstance(entry, dict) or not all(isinstance(value, str) for value in entry.values()):
                raise IsolationError("every overlay must map variable names to string values")
            overlays.append({name: str(value) for name, value in entry.items()})
        return tuple(overlays)

    def cleanup(self, count: int, prefix: str, /) -> None:
        if self._cleanup:
            self._run(self._cleanup, count, prefix)

    def probe(self) -> None:
        return None

    def fingerprint(self) -> str:
        return "shell:" + "\0".join((*self._prepare, "|", *self._cleanup))
