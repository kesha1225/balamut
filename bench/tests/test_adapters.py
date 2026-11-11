"""Adapter logic that runs without the tools: irradiate's splice of a function variant into the original module."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pymutbench" / "adapters"))

from ad_irradiate import _physical_lines, _target, splice  # noqa: E402

TABS = "def add(a, b):\n\tif a > b:\n\t\treturn a - b\n\treturn a + b\n\n\nclass Box:\n\tdef scaled(self, factor):\n" \
       "\t\ttotal = self.value * factor\n\t\treturn total\n"
SPACES = TABS.replace("\t", "    ")


def variant_text(source: str, cls: str | None, name: str, old: str, new: str) -> str:
    """A variant as irradiate writes it: the function at module level, its body one level in."""
    node = _target(ast.parse(source), cls, name)
    assert node is not None
    return ast.unparse(node).replace(old, new, 1)


class SpliceTest(unittest.TestCase):
    def check(self, source: str, cls: str | None, name: str, old: str, new: str) -> None:
        lines = _physical_lines(source)
        target = _target(ast.parse(source), cls, name)
        assert target is not None
        spliced = splice(lines, target, 0, variant_text(source, cls, name, old, new))
        mutated = _target(ast.parse(spliced), cls, name)
        assert mutated is not None
        expected = ast.parse(variant_text(source, cls, name, old, new)).body[0]
        self.assertEqual(ast.dump(mutated, include_attributes=False), ast.dump(expected, include_attributes=False))
        # Every other statement of the module is kept as it was.
        others = [ast.dump(n) for n in ast.parse(spliced).body if getattr(n, "name", None) not in {name, cls}]
        self.assertEqual(others, [ast.dump(n) for n in ast.parse(source).body if getattr(n, "name", None) not in {name, cls}])

    def test_method_of_a_tab_indented_class(self) -> None:
        self.check(TABS, "Box", "scaled", "total = self.value * factor", "total = self.value / factor")

    def test_module_function_in_a_tab_indented_file(self) -> None:
        self.check(TABS, None, "add", "a + b", "a - b")

    def test_method_of_a_space_indented_class(self) -> None:
        self.check(SPACES, "Box", "scaled", "total = self.value * factor", "total = self.value / factor")

    def test_the_def_line_keeps_the_original_indentation_characters(self) -> None:
        lines = _physical_lines(TABS)
        target = _target(ast.parse(TABS), "Box", "scaled")
        assert target is not None
        spliced = splice(lines, target, 0, variant_text(TABS, "Box", "scaled", "* factor", "/ factor"))
        self.assertIn("\n\tdef scaled(self, factor):\n", spliced)


if __name__ == "__main__":
    unittest.main()
