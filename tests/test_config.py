from __future__ import annotations

import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from balamut import config
from balamut.config import (
    DEFAULT_DIFF_KINDS,
    DEFAULT_KINDS,
    IsolationKind,
    IsolationSettings,
    Layout,
    Profile,
    ProfileError,
    _load_config_module,
    _under,
    _validate_profile,
    default_profile,
    load_kind_overrides,
    load_profile,
)
from balamut.engine import enumeration
from balamut.operators.src import src_kind


def _write(root: Path, body: str) -> Path:
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    return root


def _write_config(root: Path, body: str) -> Path:
    (root / "pyproject.toml").write_text('[tool.balamut]\nconfig = "balamut_conf.py"\n', encoding="utf-8")
    (root / "balamut_conf.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def test_defaults_without_tool_table(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    profile = load_profile(_write(tmp_path, '[project]\nname = "x"\n'))
    assert profile.layout.source == ("src",)
    assert profile.layout.import_roots == ("src",)
    assert profile.layout.tests == ("tests",)
    assert profile.kinds == DEFAULT_KINDS
    assert profile.diff_kinds == DEFAULT_DIFF_KINDS
    assert profile.base == "origin/main"
    assert profile.isolation.kind is IsolationKind.NONE
    assert profile.mutant_timeout == 8.0
    assert profile.daemon_ttl is None


def test_flat_layout_via_the_config_file(tmp_path: Path) -> None:
    profile = load_profile(
        _write_config(
            tmp_path,
            """
            from balamut.config import init

            init(source=("mypkg",), import_roots=(".",))
            """,
        )
    )
    assert profile.layout.import_roots == (".",)
    assert profile.layout.module_name("mypkg/sub/mod.py") == "mypkg.sub.mod"
    assert profile.layout.module_name("mypkg/__init__.py") == "mypkg"


def test_missing_pyproject_uses_defaults(tmp_path: Path) -> None:
    profile = load_profile(tmp_path)
    assert profile.layout.root == tmp_path.resolve()
    assert profile.layout.source == ("src",)


def test_full_profile_round_trip(tmp_path: Path) -> None:
    profile = load_profile(
        _write_config(
            tmp_path,
            """
            from balamut.config import (
                DatabaseEnv,
                IsolationKind,
                IsolationSettings,
                init,
            )

            init(
                source=("src/app",),
                import_roots=("src",),
                tests=("tests", "it"),
                test_patterns=("check_*.py",),
                base="origin/dev",
                kinds=("src", "fault"),
                diff_kinds=("src",),
                schedule_fuzz=False,
                env_prefixes=("APP_",),
                substrate_inputs=("pyproject.toml", "conftest.py"),
                mutant_timeout=12.5,
                timeout_retries=2,
                daemon_ttl=90,
                state_dir=".mut",
                preflight=(("python", "-c", "pass"),),
                coverage_concurrency=("thread", "greenlet"),
                coverage_workers_cap=2,
                isolation=IsolationSettings(
                    kind=IsolationKind.POSTGRES_TEMPLATE,
                    migrate=("alembic", "upgrade", "heads"),
                    schema_inputs=("alembic/**",),
                    env=DatabaseEnv(
                        host="DB_HOST",
                        port="DB_PORT",
                        database="DB_NAME",
                        user="DB_USER",
                        password="DB_PASSWORD",
                    ),
                ),
            )
            """,
        )
    )
    layout = profile.layout
    assert layout.module_name("src/app/x.py") == "app.x"
    assert layout.tests == ("tests", "it")
    assert layout.is_test_file("check_thing.py")
    assert not layout.is_test_file("test_thing.py")
    assert profile.base == "origin/dev"
    assert profile.kinds == ("src", "fault")
    assert profile.diff_kinds == ("src",)
    assert profile.schedule_fuzz is False
    assert profile.env_prefixes == ("APP_",)
    assert profile.mutant_timeout == 12.5
    assert profile.timeout_retries == 2
    assert profile.daemon_ttl == 90
    assert profile.state_dir == tmp_path.resolve() / ".mut"
    assert profile.preflight == (("python", "-c", "pass"),)
    assert profile.coverage.concurrency == ("thread", "greenlet")
    assert profile.coverage.workers_cap == 2
    assert profile.isolation.kind is IsolationKind.POSTGRES_TEMPLATE
    assert profile.isolation.migrate == ("alembic", "upgrade", "heads")
    assert profile.isolation.env is not None
    assert profile.isolation.env.database == "DB_NAME"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('[tool.balamut]\nsource = "src"\n', "only `config` belongs"),
        ('[tool.balamut]\nconfig = "nope.py"\n', "does not exist"),
        ("[tool.balamut]\nconfig = 5\n", "must name the config file"),
        ("[tool.balamut]\nunknown-key = 1\n", "only `config` belongs"),
    ],
)
def test_invalid_pyproject_is_rejected(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(ProfileError, match=fragment.replace(".", r"\.")):
        load_profile(_write(tmp_path, body))


@pytest.mark.parametrize(
    ("config_body", "fragment"),
    [
        ("", "must call init"),
        (
            """
            from balamut.config import init

            init(kinds=("nope",))
            """,
            "kinds: unknown kind",
        ),
        (
            """
            from balamut.config import init

            init(mutant_timeout=-1)
            """,
            "mutant_timeout must be positive",
        ),
        (
            """
            from balamut.config import init

            init(daemon_ttl=0)
            """,
            "daemon_ttl must be positive",
        ),
        (
            """
            from balamut.config import IsolationKind, IsolationSettings, init

            init(isolation=IsolationSettings(kind=IsolationKind.POSTGRES_TEMPLATE))
            """,
            "isolation.env is required",
        ),
    ],
)
def test_invalid_config_file_is_rejected(tmp_path: Path, config_body: str, fragment: str) -> None:
    with pytest.raises(ProfileError, match=fragment.replace(".", r"\.")):
        load_profile(_write_config(tmp_path, config_body))


def test_kind_registry_overrides_and_removes(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path,
        """
        from balamut.config import init
        from balamut.operators.src import src_kind

        init(register={"custom": (src_kind[0],)}, remove=frozenset({"fault"}))
        """,
    )
    overrides = load_kind_overrides(root)
    assert set(overrides.kinds) == {"custom"}
    assert overrides.removed == frozenset({"fault"})
    resolved = enumeration.resolve_kinds(["custom"], overrides)
    assert resolved["custom"] == (src_kind[0],)
    with pytest.raises(enumeration.UnknownKindError, match="removes it"):
        enumeration.resolve_kinds(["fault"], overrides)


def test_kind_registry_rejects_non_tuple_values(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "from balamut.config import init\n\ninit(register={'broken': 'not-a-kind'})\n")
    with pytest.raises(enumeration.UnknownKindError, match="tuple of operator facts"):
        enumeration.resolve_kinds(["broken"], load_kind_overrides(root))


def test_layout_path_predicates(tmp_path: Path) -> None:
    layout = Layout(
        root=tmp_path,
        source=("src",),
        import_roots=("src",),
        tests=("tests",),
        test_patterns=("test_*.py", "*_test.py"),
    )
    assert layout.is_source_path("src/pkg/a.py")
    assert not layout.is_source_path("srcx/a.py")
    assert not layout.is_source_path("tests/test_a.py")
    assert layout.is_test_path("tests/sub/x.py")
    assert layout.is_test_file("thing_test.py")
    assert layout.is_test_file("test_thing.py")
    assert not layout.is_test_file("conftest.py")
    assert layout.module_name("src/pkg/__init__.py") == "pkg"
    assert layout.watch_dirs() == ("src", "tests")


def test_module_name_prefers_longest_import_root(tmp_path: Path) -> None:
    layout = Layout(
        root=tmp_path,
        source=("src", "src/vendor"),
        import_roots=("src", "src/vendor"),
        tests=("tests",),
        test_patterns=("test_*.py",),
    )
    assert layout.module_name("src/vendor/lib/x.py") == "lib.x"
    assert layout.module_name("src/app/x.py") == "app.x"


def test_module_name_keeps_the_path_when_only_the_dot_root_ties(tmp_path: Path) -> None:
    # "." matches everything and ties in length with a one-character root; the stable length sort
    # keeps the declared order (dot first), while a lexicographic sort would surface the "a" root.
    layout = Layout(
        root=tmp_path,
        source=("a",),
        import_roots=(".", "a"),
        tests=("tests",),
        test_patterns=("test_*.py",),
    )
    assert layout.module_name("a/mod.py") == "a.mod"


def test_module_name_handles_trailing_slash_import_root(tmp_path: Path) -> None:
    layout = Layout(
        root=tmp_path,
        source=("src",),
        import_roots=("src/",),
        tests=("tests",),
        test_patterns=("test_*.py",),
    )
    assert layout.module_name("src/app/x.py") == "app.x"


def test_under_ignores_trailing_slash_in_root() -> None:
    assert _under("src/a.py", "src/") is True
    assert _under("src", "src/") is True
    assert _under("srcx/a.py", "src/") is False


def test_under_treats_dot_and_empty_root_as_everything() -> None:
    assert _under("anything.py", ".") is True
    assert _under("anything.py", "") is True


def test_is_test_path_matches_any_test_root(tmp_path: Path) -> None:
    layout = Layout(
        root=tmp_path,
        source=("src",),
        import_roots=("src",),
        tests=("unit", "integration"),
        test_patterns=("test_*.py",),
    )
    assert layout.is_test_path("unit/test_a.py") is True
    assert layout.is_test_path("integration/test_b.py") is True
    assert layout.is_test_path("src/app.py") is False


def test_is_source_path_matches_any_source_root(tmp_path: Path) -> None:
    layout = Layout(
        root=tmp_path,
        source=("src", "app"),
        import_roots=("src",),
        tests=("tests",),
        test_patterns=("test_*.py",),
    )
    assert layout.is_source_path("src/pkg/a.py") is True
    assert layout.is_source_path("app/x.py") is True
    assert layout.is_source_path("srcx/a.py") is False


def test_custom_semantic_patterns_are_used(tmp_path: Path) -> None:
    root = _write_config(tmp_path, "from balamut.config import init\n\ninit(semantic=(('billing', ('invoice',)),))\n")
    profile = load_profile(root)
    assert profile.operators.semantic == (("billing", ("invoice",)),)


def test_coverage_and_profile_defaults(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)
    assert profile.coverage.workers_cap == 4
    assert profile.coverage.concurrency == ("thread",)
    assert profile.schedule_fuzz is True
    assert profile.mutant_timeout == 8.0
    assert profile.timeout_retries == 1


def test_load_spec_clears_the_pending_root(tmp_path: Path) -> None:
    _write_config(tmp_path, "from balamut.config import init\n\ninit()\n")
    load_profile(tmp_path)
    assert config._PENDING_ROOT.get("root") is None


def test_config_load_preserves_preexisting_sys_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, "from balamut.config import init\n\ninit()\n")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    load_profile(tmp_path)
    assert str(tmp_path) in sys.path


def test_config_load_removes_config_dir_from_sys_path_after(tmp_path: Path) -> None:
    _write_config(tmp_path, "from balamut.config import init\n\ninit()\n")
    load_profile(tmp_path)
    assert str(tmp_path) not in sys.path


def test_config_load_temporarily_adds_config_dir_to_sys_path(tmp_path: Path) -> None:
    (tmp_path / "balamut_helper_mod.py").write_text("value = 7\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[tool.balamut]\nconfig = "balamut_conf.py"\n', encoding="utf-8")
    (tmp_path / "balamut_conf.py").write_text(
        "from balamut_helper_mod import value\nfrom balamut.config import init\n\ninit()\n", encoding="utf-8"
    )
    profile = load_profile(tmp_path)
    assert profile.layout.root == tmp_path.resolve()


def test_config_load_puts_config_dir_at_front_of_sys_path(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.balamut]\nconfig = "balamut_conf.py"\n', encoding="utf-8")
    (tmp_path / "balamut_conf.py").write_text(
        "import os\nimport sys\nfrom balamut.config import init\n"
        "assert os.path.abspath(sys.path[0]) == os.path.dirname(os.path.abspath(__file__))\n"
        "init()\n",
        encoding="utf-8",
    )
    profile = load_profile(tmp_path)
    assert profile.layout.root == tmp_path.resolve()


def test_load_config_module_returns_module_with_synthesized_name(tmp_path: Path) -> None:
    path = tmp_path / "balamut_conf.py"
    path.write_text("marker = 42\n", encoding="utf-8")
    module = _load_config_module(path)
    assert module.marker == 42
    expected = f"_balamut_config_{abs(hash(str(path.resolve()))) % (10**9)}"
    assert module.__name__ == expected


def test_load_config_module_caches_and_returns_module(tmp_path: Path) -> None:
    path = tmp_path / "balamut_conf.py"
    path.write_text("marker = 42\n", encoding="utf-8")
    first = _load_config_module(path)
    second = _load_config_module(path)
    assert first is second
    assert second.marker == 42


def test_load_config_module_rejects_non_importable_file(tmp_path: Path) -> None:
    (tmp_path / "cfg.txt").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="cannot be imported"):
        _load_config_module(tmp_path / "cfg.txt")


def test_default_profile_validates_its_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(profile: Profile, *, registered: frozenset[str] = frozenset()) -> None:
        raise ProfileError("validation ran")

    monkeypatch.setattr(config, "_validate_profile", boom)
    with pytest.raises(ProfileError, match="validation ran"):
        config.default_profile(tmp_path)


def test_validate_rejects_nonpositive_mutant_timeout(tmp_path: Path) -> None:
    profile = replace(default_profile(tmp_path), mutant_timeout=0.0)
    with pytest.raises(ProfileError, match="mutant_timeout must be positive"):
        _validate_profile(profile)


def test_validate_allows_positive_mutant_timeout(tmp_path: Path) -> None:
    profile = replace(default_profile(tmp_path), mutant_timeout=1.0)
    _validate_profile(profile)


def test_validate_allows_zero_timeout_retries(tmp_path: Path) -> None:
    profile = replace(default_profile(tmp_path), timeout_retries=0)
    _validate_profile(profile)


def test_validate_rejects_nonpositive_daemon_ttl(tmp_path: Path) -> None:
    profile = replace(default_profile(tmp_path), daemon_ttl=0)
    with pytest.raises(ProfileError, match="daemon_ttl must be positive"):
        _validate_profile(profile)


def test_validate_allows_positive_or_unset_daemon_ttl(tmp_path: Path) -> None:
    _validate_profile(default_profile(tmp_path))  # None: the engine's built-in TTL
    _validate_profile(replace(default_profile(tmp_path), daemon_ttl=90))


def test_validate_allows_workers_cap_of_one(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)
    profile = replace(profile, coverage=replace(profile.coverage, workers_cap=1))
    _validate_profile(profile)


def test_validate_shell_isolation_requires_prepare(tmp_path: Path) -> None:
    profile = replace(default_profile(tmp_path), isolation=IsolationSettings(kind=IsolationKind.SHELL))
    with pytest.raises(ProfileError, match=r"isolation\.prepare is required"):
        _validate_profile(profile)


def test_validate_shell_isolation_with_prepare_is_allowed(tmp_path: Path) -> None:
    iso = IsolationSettings(kind=IsolationKind.SHELL, prepare=("python", "-c", "pass"))
    profile = replace(default_profile(tmp_path), isolation=iso)
    _validate_profile(profile)
