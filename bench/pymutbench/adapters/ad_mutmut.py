"""mutmut 3: every mutant of a file, as the module mutmut's trampoline executes for it.

``mutate_file_contents`` is mutmut's own generator (pragmas and its default settings apply); each mutant name
selects one generated function variant, which replaces the original function (or the method of the named
class) in the unmutated module, exactly as ``mutmut apply`` composes it: the variant is read through mutmut's
line-span index and parsed on its own, never the whole generated module (which, for a large file, holds
thousands of variants).
"""

from __future__ import annotations

import ast
import os
import tempfile

import libcst as cst
from pathlib import Path

from _common import emit, parse_args  # type: ignore[import-not-found]
from mutmut.mutation.diff_apply import parse_generated_function
from mutmut.mutation.file_mutation import mutate_file_contents
from mutmut.utils.format_utils import orig_function_and_class_names_from_key


def _original(module: cst.Module, function: str, class_name: str | None) -> cst.FunctionDef | None:
    """The function a mutant's trampoline stands for: a module-level function, or the method of the named class
    (never a same-named method for a module-level function, as a name-only lookup would find first)."""
    if class_name is None:
        return next((stmt for stmt in module.body if isinstance(stmt, cst.FunctionDef) and stmt.name.value == function),
                    None)
    for stmt in module.body:
        if isinstance(stmt, cst.ClassDef) and stmt.name.value == class_name and isinstance(stmt.body, cst.IndentedBlock):
            for method in stmt.body.body:
                if isinstance(method, cst.FunctionDef) and method.name.value == function:
                    return method
    return None


def _spans(code: str) -> dict[tuple[str | None, str], tuple[int, int]]:
    spans: dict[tuple[str | None, str], tuple[int, int]] = {}
    for node in ast.parse(code).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            spans[(None, node.name)] = (node.lineno, node.end_lineno or node.lineno)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    spans[(node.name, item.name)] = (item.lineno, item.end_lineno or item.lineno)
    return spans


def main() -> None:
    root, files, out, scope = parse_args()
    # mutmut reads its configuration from the working directory; an explicit, otherwise default one.
    config_dir = tempfile.mkdtemp()
    (Path(config_dir) / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["."]\n', encoding="utf-8")
    os.chdir(config_dir)
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            code = (root / rel).read_text(encoding="utf-8")
            spans = _spans(code)
            mutated_file = mutate_file_contents(rel, code)
            generated_lines = mutated_file.code.split("\n")  # the line-span index counts newline characters
            original_module = cst.parse_module(code)
            for name in mutated_file.mutant_names:
                function, class_name = orig_function_and_class_names_from_key(name)
                span = spans.get((class_name, function))
                if span is not None and not scope.overlaps(rel, *span):
                    continue
                original = _original(original_module, function, class_name)
                if original is None:
                    emit(fh, rel, name, "", None, status="original_function_not_found")
                    continue
                generated_name = name.rpartition(".")[-1]
                lines = mutated_file.line_span_by_function_name[generated_name]
                variant = parse_generated_function("\n".join(generated_lines[lines.start - 1 : lines.end]) + "\n",
                                                   name=generated_name, is_method=class_name is not None)
                if variant is None:
                    raise RuntimeError(f"{name}: the line-span index does not hold the variant")
                module = original_module.deep_replace(original, variant.with_changes(name=cst.Name(function)))
                emit(fh, rel, name, "", module.code)  # type: ignore[union-attr]


if __name__ == "__main__":
    main()
