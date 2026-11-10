"""cosmic-ray: the work items ``cosmic-ray init`` would store, each materialized by ``mutate_code``.

The default configuration has no operator arguments, so exactly the argument-free operators apply, as in a
session initialized from ``cosmic-ray new-config``. A work item whose operator raises while mutating is what
cosmic-ray's own worker reports as an ``EXCEPTION`` outcome: it is emitted without text, with the exception.
"""

from __future__ import annotations

from pathlib import Path

from _common import emit, parse_args  # type: ignore[import-not-found]
from cosmic_ray.commands.init import _all_work_items
from cosmic_ray.mutating import mutate_code
from cosmic_ray.plugins import get_operator


def main() -> None:
    root, files, out, scope = parse_args()
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            path = root / rel
            source = path.read_text(encoding="utf-8")
            for item in _all_work_items([Path(path)], {}):
                spec = item.mutations[0]
                if not scope.admits(rel, spec.start_pos[0], spec.end_pos[0]):
                    continue
                operator = get_operator(spec.operator_name)(**spec.operator_args)
                native = f"{spec.operator_name}#{spec.occurrence}"
                try:
                    mutated = mutate_code(source, operator, spec.occurrence)
                except Exception as exc:  # noqa: BLE001 - the tool's worker catches any error of a work item alike
                    emit(fh, rel, native, spec.operator_name, None, status=f"tool_exception {type(exc).__name__}",
                         line=spec.start_pos[0])
                    continue
                if mutated is None:
                    emit(fh, rel, native, spec.operator_name, None, status="not_applied")
                else:
                    emit(fh, rel, native, spec.operator_name, mutated, line=spec.start_pos[0])


if __name__ == "__main__":
    main()
