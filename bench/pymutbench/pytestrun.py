"""Run pytest under the reference plugin in a fresh interpreter and read back structured outcomes.

This is the only way the harness executes a subject's tests. Every run is a new process group with a fixed
environment (hash seed, no bytecode writes, UTC, an isolated HOME and TMPDIR); the plugin's JSON lines are the
outcome, the exit code is kept only as evidence. A run that hangs past its budget is killed as a group and the
test it was executing is recorded as ``timeout``; a process that dies without its final record is ``crashed``
at the test it was executing.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_PATH = str(Path(__file__).parent / "plugin_path")
GREEN = frozenset({"passed", "skipped", "xfailed", "xpassed"})
RED = frozenset({"failed", "error", "timeout", "crashed"})


@dataclass(frozen=True, slots=True)
class TestOutcome:
    outcome: str
    duration: float = 0.0
    when: str = ""
    exc_type: str = ""
    exc_msg: str = ""


@dataclass(slots=True)
class RunResult:
    tests: dict[str, TestOutcome] = field(default_factory=dict)
    collect_errors: dict[str, str] = field(default_factory=dict)
    exitstatus: int | None = None  # pytest's own status from the final record; None when the run never finished
    returncode: int = 0
    collected: int = 0
    origins: dict[str, str] = field(default_factory=dict)
    hung: str = ""  # node id that was executing when the run was stopped for a hang
    crashed: str = ""  # node id that was executing when the process died
    # SIGKILL from outside the harness (the container VM's out-of-memory killer): the run says nothing about
    # the code under test, and no test is blamed for it
    external_kill: bool = False
    killed_after_end: bool = False  # the run timeout stopped a process that had already written its end record
    wall: float = 0.0
    stderr_tail: str = ""
    lines: dict[str, dict[str, list[int]]] = field(default_factory=dict)  # test -> file -> executed lines

    @property
    def finished(self) -> bool:
        return self.exitstatus is not None

    def red(self) -> set[str]:
        return {nodeid for nodeid, test in self.tests.items() if test.outcome in RED}

    def green(self) -> set[str]:
        return {nodeid for nodeid, test in self.tests.items() if test.outcome in GREEN}


@dataclass(frozen=True, slots=True)
class PytestEnv:
    python: str  # the subject environment's interpreter
    pythonpath: tuple[str, ...]  # import roots of the tree being judged, first
    origin_packages: tuple[str, ...]
    extra_args: tuple[str, ...] = ()
    per_test_timeout: float = 60.0


def run_pytest(
    env: PytestEnv,
    cwd: Path,
    targets: list[str],
    *,
    run_timeout: float,
    deselect: list[str] | None = None,
    exitfirst: bool = False,
    cover_files: list[str] | None = None,
) -> RunResult:
    scratch = Path(tempfile.mkdtemp(prefix="pmb-run-"))
    out, current = scratch / "out.jsonl", scratch / "current"
    home, tmp = scratch / "home", scratch / "tmp"
    home.mkdir()
    tmp.mkdir()
    # Plugin switches must be real argv: pytest loads `-p` plugins before it expands an `@file`.
    fixed = ["-p", "pmb_pytest_plugin", "-p", "no:cacheprovider", "-p", "no:randomly", "-q", "--no-header", "-rN"]
    fixed += ["--tb=no", "--color=no", *env.extra_args]
    args: list[str] = ["-x"] if exitfirst else []
    args += targets
    argfile = scratch / "args.txt"
    # One line per argument and none without one: an empty line reaches pytest as the path "", which collects the
    # whole rootdir instead of the configured test paths.
    argfile.write_text("".join(f"{arg}\n" for arg in args), encoding="utf-8")
    proc_env = {
        "PATH": f"{Path(env.python).parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": ":".join((*env.pythonpath, PLUGIN_PATH)),
        "PMB_OUT": str(out),
        "PMB_CURRENT": str(current),
        "PMB_TEST_TIMEOUT": str(env.per_test_timeout),
        "PMB_ORIGIN_PACKAGES": ",".join(env.origin_packages),
        "COLUMNS": "200",
    }
    if cover_files:
        proc_env["PMB_COVER_FILES"] = os.pathsep.join(cover_files)
    if deselect:
        deselect_file = scratch / "deselect.txt"
        deselect_file.write_text("".join(f"{nodeid}\n" for nodeid in deselect), encoding="utf-8")
        proc_env["PMB_DESELECT_FILE"] = str(deselect_file)
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [env.python, "-m", "pytest", *fixed, f"@{argfile}"],
        cwd=cwd,
        env=proc_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    hard_timeout = False
    try:
        _stdout, stderr = proc.communicate(timeout=run_timeout)
    except subprocess.TimeoutExpired:
        hard_timeout = True
        os.killpg(proc.pid, signal.SIGKILL)
        _stdout, stderr = proc.communicate()
    result = RunResult(returncode=proc.returncode, wall=round(time.monotonic() - started, 3))
    full_stderr = stderr.decode("utf-8", "replace")
    result.stderr_tail = full_stderr[-1500:]
    _read_records(out, result)
    result.killed_after_end = hard_timeout and result.finished
    result.external_kill = proc.returncode == -signal.SIGKILL and not hard_timeout
    if not result.finished and not result.external_kill:
        active = current.read_text(encoding="utf-8").strip() if current.exists() else ""
        if active and active not in result.tests:
            # faulthandler opens its dump with "Timeout (H:MM:SS)!"; the tracebacks after it can be long.
            hung = hard_timeout or "Timeout (0:" in full_stderr
            result.tests[active] = TestOutcome("timeout" if hung else "crashed")
            if hung:
                result.hung = active
            else:
                result.crashed = active
    shutil.rmtree(scratch, ignore_errors=True)
    return result


def _read_records(path: Path, result: RunResult) -> None:
    if not path.exists():
        return
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                continue  # the last line, cut by a dying process
            raise RuntimeError(f"{path}: record {index + 1} of {len(lines)} is not JSON") from None
        match record.get("kind"):
            case "test":
                result.tests[record["nodeid"]] = TestOutcome(
                    record["outcome"],
                    record.get("duration", 0.0),
                    record.get("when", ""),
                    record.get("exc_type", ""),
                    record.get("exc_msg", ""),
                )
            case "collect_error":
                result.collect_errors[record["nodeid"]] = record.get("msg", "")
            case "lines":
                result.lines = record.get("by_test", {})
            case "end":
                result.exitstatus = record["exitstatus"]
                result.collected = record.get("collected", 0)
                result.origins = record.get("origins", {})
            case _:
                pass
