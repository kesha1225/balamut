"""Which test files an edit of test-support code reaches (balamut.coverage.support_deps).

The substrate measures coverage of the source tree, so it cannot tell which support code a test executed.
The dependency is derived statically and generously (an extra re-measurement is cheaper than a missed one):
everything a test file imports directly, transitively, relatively or by a module-name string, plus the
conftest of every directory on its path and the ``__init__`` of its packages. In a directory without
``__init__.py`` a test imports a sibling by its bare name: pytest puts that directory on sys.path.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from balamut.config import Layout
from balamut.coverage import support_deps
from balamut.coverage.support_deps import file_references, support_dependents
from bm_support import make_layout, write_file


@pytest.fixture
def tree(tmp_path: Path) -> Layout:
    files = {
        "tests/__init__.py": "",
        "tests/conftest.py": "from tests.support.rooted import ROOTED\n",
        "tests/support/__init__.py": "",
        "tests/support/rooted.py": "ROOTED = 1\n",
        "tests/support/chain.py": "CHAIN = 1\n",
        "tests/support/local.py": "from tests.support.chain import CHAIN\nLOCAL = CHAIN\n",
        "tests/support/fixtures_a.py": "FIXTURE = 1\n",
        "tests/support/plugin_mod.py": "PLUGIN = 1\n",
        "tests/support/unused.py": "UNUSED = 1\n",
        "tests/a/__init__.py": "",
        "tests/a/conftest.py": "from tests.support.fixtures_a import FIXTURE\n",
        "tests/a/a_test.py": "from tests.support.local import LOCAL\n\ndef test_a():\n    assert LOCAL\n",
        "tests/a/neighbour.py": "NEIGHBOUR = 1\n",
        "tests/a/rel_test.py": "from .neighbour import NEIGHBOUR\n\ndef test_rel():\n    assert NEIGHBOUR\n",
        "tests/a/deep/__init__.py": "",
        "tests/a/deep/deep_test.py": "def test_deep():\n    assert True\n",
        "tests/b/__init__.py": "",
        "tests/b/b_test.py": (
            "import importlib\n\ndef test_b():\n    assert importlib.import_module('tests.support.plugin_mod')\n"
        ),
        "tests/tools/_helpers.py": "HELPER = 1\n",
        "tests/tools/tool_test.py": "from _helpers import HELPER\n\ndef test_tool():\n    assert HELPER\n",
        "tests/tools/pkg/__init__.py": "",
        "tests/tools/pkg/sub.py": "SUB = 1\n",
        "tests/tools/pkg_test.py": "import pkg.sub\n\ndef test_pkg():\n    assert pkg.sub.SUB\n",
    }
    for relative, content in files.items():
        write_file(tmp_path / relative, content)
    return make_layout(tmp_path)


_ALL_TESTS = (
    "tests/a/a_test.py",
    "tests/a/deep/deep_test.py",
    "tests/a/rel_test.py",
    "tests/b/b_test.py",
    "tests/tools/pkg_test.py",
    "tests/tools/tool_test.py",
)


def test_a_helper_reaches_only_the_tests_that_import_it(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/local.py"]) == ("tests/a/a_test.py",)


def test_dependence_is_transitive(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/chain.py"]) == ("tests/a/a_test.py",)


def test_a_conftest_reaches_every_test_below_it_and_nothing_beside(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/a/conftest.py"]) == (
        "tests/a/a_test.py",
        "tests/a/deep/deep_test.py",
        "tests/a/rel_test.py",
    )


def test_a_helper_of_a_conftest_reaches_the_whole_conftest_scope(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/fixtures_a.py"]) == (
        "tests/a/a_test.py",
        "tests/a/deep/deep_test.py",
        "tests/a/rel_test.py",
    )


def test_what_the_root_conftest_imports_reaches_the_whole_suite(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/rooted.py"]) == _ALL_TESTS


def test_a_module_named_by_a_string_counts_as_imported(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/plugin_mod.py"]) == ("tests/b/b_test.py",)


def test_a_relative_import_counts(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/a/neighbour.py"]) == ("tests/a/rel_test.py",)


def test_a_sibling_imported_by_its_bare_name_counts(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/tools/_helpers.py"]) == ("tests/tools/tool_test.py",)


def test_a_package_under_a_directory_without_init_counts_by_its_bare_name(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/tools/pkg/sub.py"]) == ("tests/tools/pkg_test.py",)


def test_a_repeated_question_parses_only_what_changed(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/local.py"]) == ("tests/a/a_test.py",)
    parsed = file_references.cache_info().misses
    assert support_dependents(tree, ["tests/support/local.py"]) == ("tests/a/a_test.py",)
    assert file_references.cache_info().misses == parsed
    # The cache is keyed by content: an edit this process has never seen is unique.
    edited = f"from tests.support.local import LOCAL\n\ndef test_b():\n    assert LOCAL  # {uuid.uuid4().hex}\n"
    write_file(tree.root / "tests/b/b_test.py", edited)
    assert support_dependents(tree, ["tests/support/local.py"]) == ("tests/a/a_test.py", "tests/b/b_test.py")
    assert file_references.cache_info().misses == parsed + 1


def test_a_package_init_reaches_every_module_of_the_package(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/a/__init__.py"]) == (
        "tests/a/a_test.py",
        "tests/a/deep/deep_test.py",
        "tests/a/rel_test.py",
    )


def test_a_helper_nobody_imports_reaches_nobody(tree: Layout) -> None:
    assert support_dependents(tree, ["tests/support/unused.py"]) == ()


def test_a_deleted_conftest_still_reaches_its_directory(tree: Layout) -> None:
    write_file(tree.root / "tests/b/conftest.py", "")
    (tree.root / "tests/b/conftest.py").unlink()
    assert support_dependents(tree, ["tests/b/conftest.py"]) == ("tests/b/b_test.py",)


def test_a_file_that_does_not_parse_is_assumed_to_depend_on_anything(tree: Layout) -> None:
    write_file(tree.root / "tests/b/broken_test.py", "def test_broken(:\n")
    assert support_dependents(tree, ["tests/support/unused.py"]) == ("tests/b/broken_test.py",)


def test_a_custom_test_root_is_followed(tmp_path: Path) -> None:
    write_file(tmp_path / "checks/__init__.py", "")
    write_file(tmp_path / "checks/helpers.py", "H = 1\n")
    write_file(tmp_path / "checks/a_test.py", "from checks.helpers import H\n\ndef test_a():\n    assert H\n")
    write_file(tmp_path / "checks/b_test.py", "def test_b():\n    assert True\n")
    layout = make_layout(tmp_path, tests=("checks",))
    assert support_dependents(layout, ["checks/helpers.py"]) == ("checks/a_test.py",)


def test_ancestors_yield_each_package_level() -> None:
    assert list(support_deps._ancestors("tests.a.b")) == ["tests.a", "tests"]


def test_relative_import_reaching_the_repository_root_resolves() -> None:
    node = ast.parse("from ..x import y").body[0]
    assert list(support_deps.imported_modules(node, "a/b.py")) == ["x", "x.y"]


def test_relative_import_beyond_the_repository_root_is_ignored() -> None:
    node = ast.parse("from ...x import y").body[0]
    assert list(support_deps.imported_modules(node, "a/b.py")) == []


def test_file_references_include_in_test_module_name_strings() -> None:
    refs = support_deps.file_references(
        "tests/unique_test.py",
        'import importlib\n\nimportlib.import_module("tests.support.unique_plugin")\n',
        ("tests",),
    )
    assert refs is not None
    assert "tests.support.unique_plugin" in refs


def test_in_tests_matches_any_root() -> None:
    assert support_deps._in_tests("tests.x", ("tests", "checks"))
    assert not support_deps._in_tests("other.x", ("tests", "checks"))


def test_bare_roots_excludes_package_directories_with_init() -> None:
    paths = {"tests/pkg/__init__.py", "tests/pkg/mod.py"}
    assert support_deps._bare_roots(paths, ["tests.pkg.mod"], ("tests",)) == {}


def test_bare_roots_takes_a_directory_under_any_root() -> None:
    paths = {"tests/tools/_helper.py"}
    assert support_deps._bare_roots(paths, ["tests.tools._helper"], ("tests", "checks")) == {
        "_helper": ("tests.tools",)
    }


def test_modules_keeps_parsing_after_an_unparsable_file(tree: Layout) -> None:
    write_file(tree.root / "tests/00_broken.py", "def broken(:\n")
    modules = support_deps._modules(tree, ("tests",))
    assert "tests.b.b_test" in modules


def test_support_dependents_with_no_changes_returns_empty(tree: Layout) -> None:
    assert support_dependents(tree, []) == ()


def test_parsed_cache_size_is_pinned() -> None:
    assert support_deps._PARSED_CACHE_SIZE == 8192
    assert file_references.cache_info().maxsize == 8192


def test_support_dependents_munges_a_nested_test_root(tmp_path: Path) -> None:
    # `checks/inner` must become the module path `checks.inner`; the slash→dot conversion is what maps it.
    write_file(tmp_path / "checks/inner/__init__.py", "")
    write_file(tmp_path / "checks/inner/helper.py", "X = 1\n")
    write_file(tmp_path / "checks/inner/a_test.py", "from checks.inner.helper import X\n")
    layout = make_layout(tmp_path, tests=("checks/inner",))
    assert support_dependents(layout, ["checks/inner/helper.py"]) == ("checks/inner/a_test.py",)
