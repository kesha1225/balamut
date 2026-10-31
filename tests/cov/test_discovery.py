"""Re-export discovery (balamut.coverage.discovery).

pytest collects a ``test_*`` function re-exported into ``x_test.py`` from a helper module under the node id of
the wrapper, not of the defining file. The index maps (defining file, name) to that wrapper.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from balamut.config import Layout
from balamut.coverage import discovery
from balamut.coverage.discovery import reexport_index, reexport_map
from bm_support import make_layout, write_file


def _seed_reexport_tree(root: Path) -> None:
    write_file(root / "tests/__init__.py", "")
    write_file(root / "tests/pkg/__init__.py", "")
    write_file(root / "tests/pkg/scenarios.py", "def test_a():\n    assert True\n")
    write_file(root / "tests/pkg/pkg_test.py", "from tests.pkg.scenarios import test_a\n")
    write_file(root / "tests/pkg/helpers.py", "def make_thing():\n    return 1\n")


def test_reexport_map_maps_def_file_to_wrapper(tmp_path: Path) -> None:
    _seed_reexport_tree(tmp_path)
    assert reexport_map(make_layout(tmp_path)) == {"tests/pkg/scenarios.py": "tests/pkg/pkg_test.py"}


def test_reexport_map_sees_an_import_split_by_a_line_continuation(tmp_path: Path) -> None:
    # `from tests \` + newline + `.pkg import test_a` is legal Python; the text prefilter in front of the parser
    # may only err towards parsing more, never drop such a wrapper.
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/scenarios.py", "def test_a():\n    assert True\n")
    write_file(tmp_path / "tests/pkg/pkg_test.py", "from tests \\\n    .pkg.scenarios import test_a\n")
    assert reexport_map(make_layout(tmp_path)) == {"tests/pkg/scenarios.py": "tests/pkg/pkg_test.py"}


def test_reexport_map_sees_a_test_declared_across_a_line_continuation(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/scenarios.py", "def \\\n    test_a():\n    assert True\n")
    write_file(tmp_path / "tests/pkg/pkg_test.py", "from tests.pkg.scenarios import test_a\n")
    assert reexport_map(make_layout(tmp_path)) == {"tests/pkg/scenarios.py": "tests/pkg/pkg_test.py"}


def test_reexport_map_falls_back_to_ast_when_an_import_is_not_recognised(tmp_path: Path) -> None:
    # Two imports: one into a helper without tests, one from a module with a non-ASCII name the byte prefilter
    # cannot read. Counting recognized occurrences leaves the decision to the parser.
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/café/__init__.py", "")
    write_file(tmp_path / "tests/café/impl.py", "def test_real():\n    assert True\n")
    write_file(tmp_path / "tests/café/support.py", "def make_thing():\n    return 1\n")
    write_file(
        tmp_path / "tests/café/pkg_test.py",
        "from tests.café.support import make_thing\nfrom tests.café.impl import test_real\n",
    )
    assert reexport_map(make_layout(tmp_path)) == {"tests/café/impl.py": "tests/café/pkg_test.py"}


def test_reexport_map_sees_a_test_declared_after_a_form_feed(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/scenarios.py", "\fdef test_a():\n    assert True\n")
    write_file(tmp_path / "tests/pkg/pkg_test.py", "from tests.pkg.scenarios import test_a\n")
    assert reexport_map(make_layout(tmp_path)) == {"tests/pkg/scenarios.py": "tests/pkg/pkg_test.py"}


def test_reexport_index_only_maps_imported_names(tmp_path: Path) -> None:
    # The wrapper imports only test_a: test_b is collected nowhere and must not be indexed.
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/impl.py", "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n")
    write_file(tmp_path / "tests/pkg/wrap_test.py", "from tests.pkg.impl import test_a\n")
    assert reexport_index(make_layout(tmp_path)) == {("tests/pkg/impl.py", "test_a"): "tests/pkg/wrap_test.py"}


def test_reexport_index_star_maps_all_test_names(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(
        tmp_path / "tests/pkg/impl.py",
        "def test_a():\n    assert True\n\n\nclass TestB:\n    def test_m(self):\n        assert True\n",
    )
    write_file(tmp_path / "tests/pkg/wrap_test.py", "from tests.pkg.impl import *\n")
    assert reexport_index(make_layout(tmp_path)) == {
        ("tests/pkg/impl.py", "test_a"): "tests/pkg/wrap_test.py",
        ("tests/pkg/impl.py", "TestB"): "tests/pkg/wrap_test.py",
    }


def test_reexport_map_ignores_plain_support_import(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/pkg/__init__.py", "")
    write_file(tmp_path / "tests/pkg/helpers.py", "def make_thing():\n    return 1\n")
    write_file(tmp_path / "tests/pkg/thing_test.py", "from tests.pkg.helpers import make_thing\n")
    assert reexport_map(make_layout(tmp_path)) == {}


def test_reexport_map_uses_the_configured_test_root(tmp_path: Path) -> None:
    write_file(tmp_path / "checks/__init__.py", "")
    write_file(tmp_path / "checks/scenarios.py", "def test_a():\n    assert True\n")
    write_file(tmp_path / "checks/wrap_test.py", "from checks.scenarios import test_a\n")
    assert reexport_map(make_layout(tmp_path, tests=("checks",))) == {"checks/scenarios.py": "checks/wrap_test.py"}


def test_reexport_consumers_share_one_snapshot_build(tmp_path: Path) -> None:
    _seed_reexport_tree(tmp_path)
    layout = make_layout(tmp_path)
    discovery.reexport_cache_clear()
    try:
        snapshot = discovery.reexport_snapshot(layout)
        assert reexport_index(layout)
        assert reexport_map(layout)
        assert discovery.reexport_snapshot(layout) is snapshot
    finally:
        discovery.reexport_cache_clear()


def test_reexport_snapshot_has_explicit_invalidation_boundary(tmp_path: Path) -> None:
    _seed_reexport_tree(tmp_path)
    layout = make_layout(tmp_path)
    discovery.reexport_cache_clear()
    try:
        assert reexport_map(layout) == {"tests/pkg/scenarios.py": "tests/pkg/pkg_test.py"}
        write_file(tmp_path / "tests/pkg/second_test.py", "from tests.pkg.scenarios import test_a\n")
        assert reexport_map(layout) == {"tests/pkg/scenarios.py": "tests/pkg/pkg_test.py"}
        discovery.reexport_cache_clear()
        assert reexport_map(layout) == {"tests/pkg/scenarios.py": "tests/pkg/second_test.py"}
    finally:
        discovery.reexport_cache_clear()


def _may_reexport(layout: Layout, source: bytes) -> bool:
    module_roots = tuple(root.strip("/").replace("/", ".") for root in layout.tests if root not in {".", ""})
    return discovery._wrapper_may_reexport(layout, source, discovery._prefilter(module_roots), {})


# --- _wrapper_may_reexport (the text prefilter) ---


def test_wrapper_without_import_from_a_test_root_is_false(tmp_path: Path) -> None:
    assert _may_reexport(make_layout(tmp_path), b"def test_plain():\n    assert True\n") is False


def test_wrapper_importing_only_support_is_false(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/helpers.py", "def make_thing():\n    return 1\n")
    layout = make_layout(tmp_path)
    assert _may_reexport(layout, b"from tests.helpers import make_thing\n") is False


def test_wrapper_skips_imports_from_test_files(tmp_path: Path) -> None:
    write_file(tmp_path / "tests/__init__.py", "")
    write_file(tmp_path / "tests/a_test.py", "def test_a():\n    assert True\n")
    write_file(tmp_path / "tests/impl.py", "def test_b():\n    assert True\n")
    layout = make_layout(tmp_path)
    source = b"from tests.a_test import test_a\nfrom tests.impl import test_b\n"
    assert _may_reexport(layout, source) is True


def test_defines_collectable_tests_handles_a_missing_file(tmp_path: Path) -> None:
    assert discovery._defines_collectable_tests(tmp_path / "missing.py", {}) is False


# --- _snapshot_build ---


def test_reexport_map_with_no_test_roots_is_empty(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        assert reexport_map(make_layout(tmp_path, tests=())) == {}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_skips_missing_test_roots(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.impl import test_a\n")
        layout = make_layout(tmp_path, tests=("missing", "tests"))
        assert reexport_map(layout) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_ignores_an_unreadable_test_wrapper(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.impl import test_a\n")
        (tmp_path / "tests/aaa_test.py").mkdir()  # a directory matching the test pattern cannot be read
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_skips_non_reexporting_wrappers(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/aaa_test.py", "def test_plain():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.impl import test_a\n")
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_ignores_a_syntax_error_wrapper(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/aaa_test.py", "from tests.impl import (\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.impl import test_a\n")
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_ignores_non_import_nodes(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "x = 1\nfrom tests.impl import test_a\n")
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_with_multiple_test_roots_matches_any_root(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "checks/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.impl import test_a\n")
        assert reexport_map(make_layout(tmp_path, tests=("tests", "checks"))) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_skips_non_module_imports(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "from other.thing import foo\nfrom tests.impl import test_a\n")
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_ignores_imports_from_test_files(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/other_test.py", "def test_b():\n    assert True\n")
        write_file(
            tmp_path / "tests/wrap_test.py", "from tests.impl import test_a\nfrom tests.other_test import test_b\n"
        )
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_skips_missing_defining_files(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.missing import foo\nfrom tests.impl import test_a\n")
        assert reexport_map(make_layout(tmp_path)) == {"tests/impl.py": "tests/wrap_test.py"}
    finally:
        discovery.reexport_cache_clear()


def test_reexport_map_ignores_a_syntax_error_defining_file(tmp_path: Path) -> None:
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "tests/__init__.py", "")
        write_file(tmp_path / "tests/impl.py", "def test_a(:\n    return 1\n")
        write_file(tmp_path / "tests/wrap_test.py", "from tests.impl import *\n")
        assert reexport_map(make_layout(tmp_path)) == {}
    finally:
        discovery.reexport_cache_clear()


def test_test_name_re_reexecutes_and_matches_multiline() -> None:
    # The import-time regex must be compiled with MULTILINE and its bytes pattern kept: `^` then matches a test
    # declared on any line, not only at the very start of the file.
    importlib.reload(discovery)
    pattern = discovery._TEST_NAME_RE
    assert pattern.flags & re.MULTILINE
    assert pattern.search(b"import os\ndef test_one():\n    pass\n") is not None
    assert pattern.search(b"x = 1\nclass TestThing:\n    pass\n") is not None


def test_reexport_map_munges_a_nested_test_root(tmp_path: Path) -> None:
    # `checks/inner` becomes the module path `checks.inner`; the slash→dot conversion maps the import in the wrapper.
    discovery.reexport_cache_clear()
    try:
        write_file(tmp_path / "checks/inner/__init__.py", "")
        write_file(tmp_path / "checks/inner/scenarios.py", "def test_a():\n    assert True\n")
        write_file(tmp_path / "checks/inner/wrap_test.py", "from checks.inner.scenarios import test_a\n")
        assert reexport_map(make_layout(tmp_path, tests=("checks/inner",))) == {
            "checks/inner/scenarios.py": "checks/inner/wrap_test.py"
        }
    finally:
        discovery.reexport_cache_clear()
