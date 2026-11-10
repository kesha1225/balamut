"""universalmutator: the mutant files ``mutate FILE python`` writes with its default rules and checks.

The tool's own validity check (compiling each candidate) and its redundancy pruning are part of its generation
policy and stay enabled; every file it keeps is one mutant.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from _common import emit, parse_args, run_tool  # type: ignore[import-not-found]

_INDEX = re.compile(r"\.mutant\.(\d+)\.py$")


def main() -> None:
    root, files, out, scope = parse_args()
    mutate = str(Path(sys.executable).parent / "mutate")
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            with tempfile.TemporaryDirectory() as tmp:
                argv = [mutate, str(root / rel), "python", "--mutantDir", tmp]
                if scope.ranges:
                    # The tool's own line restriction: a file of line numbers.
                    lines_file = Path(tmp) / "lines.txt"
                    lines_file.write_text(" ".join(map(str, scope.lines(rel))), encoding="utf-8")
                    argv += ["--lines", str(lines_file)]
                proc = run_tool(argv, cwd=tmp, text=True)
                if proc.returncode != 0:  # the tool failed on the file: the adapter fails, visibly
                    raise RuntimeError(f"mutate failed on {rel}: rc={proc.returncode} {proc.stdout[-300:]} "
                                       f"{proc.stderr[-300:]}")
                for path in sorted(Path(tmp).glob("*.mutant.*.py"), key=lambda p: int(_INDEX.search(p.name).group(1))):  # type: ignore[union-attr]
                    emit(fh, rel, path.name, "", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
