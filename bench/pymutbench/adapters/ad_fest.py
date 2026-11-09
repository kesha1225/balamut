"""fest: every mutant ``fest run`` generates for a file, read from its JSON report.

With an empty coverage file fest runs no test and reports every generated mutant as ``NoCoverage`` together
with its byte offset, byte length and replacement text, which are spliced into the original bytes. The mutants
whose replaced lines lie outside the bug's scope are skipped before splicing.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _common import emit, parse_args, run_tool  # type: ignore[import-not-found]

EMPTY_COVERAGE = {"meta": {"version": "7.0", "show_contexts": True}, "files": {}}


def main() -> None:
    root, files, out, scope = parse_args()
    fest = str(Path(sys.executable).parent / "fest")
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                (project / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, project / rel)
                coverage = project / "empty.coverage.json"
                coverage.write_text(json.dumps(EMPTY_COVERAGE), encoding="utf-8")
                proc = run_tool([fest, "run", "-s", rel, "-o", "json", "--coverage-from", str(coverage)],
                                cwd=project, text=True)
                try:
                    report = json.loads(proc.stdout)
                except json.JSONDecodeError as exc:  # the tool failed on the file: the adapter fails, visibly
                    raise RuntimeError(f"fest wrote no report for {rel}: rc={proc.returncode} "
                                       f"{proc.stderr[-500:]}") from exc
            data = (root / rel).read_bytes()
            for index, result in enumerate(report.get("results", [])):
                mutant = result["mutant"]
                start, length = mutant["byte_offset"], mutant["byte_length"]
                # The replaced bytes bound the edit: a mutant whose replaced lines miss the scope cannot be in
                # scope, and is not written out (each one is a complete copy of the file).
                first = data.count(b"\n", 0, start) + 1
                if not scope.overlaps(rel, first, first + data.count(b"\n", start, start + length)):
                    continue
                if data[start : start + length].decode("utf-8", "replace") != mutant["original_text"]:
                    emit(fh, rel, f"{mutant['mutator_name']}#{index}", mutant["mutator_name"], None,
                         status="offset_mismatch", line=mutant["line"])
                    continue
                mutated = data[:start] + mutant["mutated_text"].encode("utf-8") + data[start + length :]
                emit(fh, rel, f"{mutant['mutator_name']}@{mutant['line']}:{mutant['column']}#{index}",
                     mutant["mutator_name"], mutated.decode("utf-8"), line=mutant["line"])


if __name__ == "__main__":
    main()
