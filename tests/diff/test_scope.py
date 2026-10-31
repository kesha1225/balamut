"""The immutable git scope of one diff run (balamut.diff.scope)."""

from __future__ import annotations

import hashlib
import importlib
import subprocess
from collections.abc import Callable
from dataclasses import fields
from functools import partial
from pathlib import Path

import pytest

from balamut.diff import scope as diff_scope
from balamut.diff.scope import BaseMode, DiffScope, Git
from bm_support.scopes import pinned, scope

_DIFF = "+++ b/src/x.py\n@@ -9,0 +10,2 @@\n+first\n+second\n"
_DELETE_ONLY_DIFF = "+++ /dev/null\n@@ -1,2 +0,0 @@\n-first\n-second\n"


def _use_git(monkeypatch: pytest.MonkeyPatch, fake: Callable[..., str]) -> None:
    """Route every git call of the scope module through ``fake`` (already bound to the repository root)."""

    def git_at(_root: Path) -> Git:
        return Git(output=fake, raw=fake)

    monkeypatch.setattr(diff_scope, "git_at", git_at)


_scope = partial(scope, {"src/x.py": (10, 11)})


# ``_git_unquote``: decoding git's C-quoted paths (byte-precise escapes and octal runs).


def test_git_unquote_returns_unquoted_paths_unchanged() -> None:
    assert diff_scope._git_unquote("src/x.py") == "src/x.py"
    assert diff_scope._git_unquote("") == ""


def test_git_unquote_requires_both_quote_markers() -> None:
    # A lone leading or trailing double quote is not a quoted path: the guard must not unquote it.
    assert diff_scope._git_unquote('"a') == '"a'
    assert diff_scope._git_unquote('a"') == 'a"'


def test_git_unquote_strips_plain_quotes() -> None:
    assert diff_scope._git_unquote('"abc"') == "abc"


def test_git_unquote_decodes_every_c_escape() -> None:
    assert diff_scope._git_unquote(r'"\a\b\t\n\v\f\r\"\\"') == "\x07\x08\x09\x0a\x0b\x0c\x0d\x22\x5c"


def test_git_unquote_decodes_a_trailing_escape_byte() -> None:
    # A two-character inner (`\a`) hits the `index + 1 >= len` boundary exactly for the non-strict comparison.
    assert diff_scope._git_unquote(r'"\a"') == "\x07"


def test_git_unquote_keeps_a_trailing_backslash() -> None:
    assert diff_scope._git_unquote('"abc\\"') == "abc\\"


def test_git_unquote_decodes_octal_escapes() -> None:
    assert diff_scope._git_unquote('"\\101"') == "A"
    # Chars after the octal run: the loop continues past the escape.
    assert diff_scope._git_unquote('"\\101x"') == "Ax"


def test_git_unquote_decodes_a_two_digit_octal_run_at_the_end() -> None:
    assert diff_scope._git_unquote('"\\10"') == "\x08"


def test_git_unquote_limits_an_octal_run_to_three_digits() -> None:
    # "777" is the run (511 & 0xFF == 0xFF, which is not valid UTF-8 -> the replacement char); the fourth
    # digit is a plain '7' appended after the escape.
    assert diff_scope._git_unquote('"\\7777"') == "\ufffd" + "7"


def test_git_unquote_decodes_an_octal_run_followed_by_plain_chars() -> None:
    assert diff_scope._git_unquote('"\\1a1"') == "\x01a1"


def test_git_unquote_keeps_an_unknown_escape_literal() -> None:
    assert diff_scope._git_unquote('"\\q"') == "\\q"
    # The unknown escape is two chars wide: the char after it is still processed.
    assert diff_scope._git_unquote('"\\qa"') == "\\qa"


def test_git_diff_output_pins_the_diff_config() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_call(*args: str) -> str:
        calls.append(args)
        return ""

    assert diff_scope._git_diff_output(fake_call, "merge-base-sha", "--unified=0") == ""
    assert calls == [
        (
            "-c",
            "diff.mnemonicPrefix=false",
            "-c",
            "diff.noprefix=false",
            "-c",
            "core.quotepath=false",
            "diff",
            "--unified=0",
            "merge-base-sha",
        )
    ]


def test_resolve_scope_freezes_merge_base_and_changed_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "src/x.py"
    source.parent.mkdir()
    source.write_text("original", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    calls: list[tuple[str, ...]] = []

    def fake_git_output(*args: str) -> str:
        calls.append(args)
        outputs: dict[tuple[str, ...], str] = {
            ("rev-parse", "origin/main"): "base-sha",
            ("rev-parse", "HEAD"): "head-sha",
            ("merge-base", "base-sha", "head-sha"): "merge-base-sha",
            (
                "-c",
                "diff.mnemonicPrefix=false",
                "-c",
                "diff.noprefix=false",
                "-c",
                "core.quotepath=false",
                "diff",
                "--unified=0",
                "merge-base-sha",
            ): _DIFF,
            (
                "-c",
                "diff.mnemonicPrefix=false",
                "-c",
                "diff.noprefix=false",
                "-c",
                "core.quotepath=false",
                "diff",
                "--name-only",
                "-z",
                "merge-base-sha",
            ): "src/x.py\0",
            ("ls-files", "-z", "--others", "--exclude-standard"): "",
        }
        return outputs[args]

    _use_git(monkeypatch, fake_git_output)

    scope = diff_scope.resolve_scope("origin/main", repo_root=tmp_path, base_mode=BaseMode.MERGE_BASE)

    assert scope.base_sha == "base-sha"
    assert scope.diff_base_sha == "merge-base-sha"
    assert scope.head_sha == "head-sha"
    assert scope.changed_lines == {"src/x.py": (10, 11)}
    assert scope.file_hashes == {"src/x.py": hashlib.sha256(b"original").hexdigest()}
    assert calls.count(("rev-parse", "origin/main")) == 1
    assert calls.count(("rev-parse", "HEAD")) == 1
    assert calls.count(("merge-base", "base-sha", "head-sha")) == 1


def test_resolve_scope_direct_uses_resolved_base_and_marks_untracked_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracked = tmp_path / "src/x.py"
    untracked = tmp_path / "src/new.py"
    tracked.parent.mkdir()
    tracked.write_text("tracked", encoding="utf-8")
    untracked.write_text("untracked", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "origin/main"):
            return "base-sha"
        if args == ("rev-parse", "HEAD"):
            return "head-sha"
        if args[-2:] == ("--unified=0", "base-sha"):
            return _DIFF
        if "--name-only" in args:
            return "src/x.py\0"
        if args == ("ls-files", "-z", "--others", "--exclude-standard"):
            return "src/new.py\0"
        raise AssertionError(f"unexpected git call: {args}")

    _use_git(monkeypatch, fake_git_output)

    scope = diff_scope.resolve_scope("origin/main", repo_root=tmp_path, base_mode=BaseMode.DIRECT)

    assert scope.diff_base_sha == "base-sha"
    assert scope.changed_lines == {"src/x.py": (10, 11), "src/new.py": ()}
    assert scope.file_hashes == {
        "src/x.py": hashlib.sha256(b"tracked").hexdigest(),
        "src/new.py": hashlib.sha256(b"untracked").hexdigest(),
    }


def test_resolve_scope_hashes_deletion_only_tracked_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "origin/main"):
            return "base-sha"
        if args == ("rev-parse", "HEAD"):
            return "head-sha"
        if "--name-only" in args:
            return "src/deleted.py\0"
        if args[-2:] == ("--unified=0", "base-sha"):
            return _DELETE_ONLY_DIFF
        if args == ("ls-files", "-z", "--others", "--exclude-standard"):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    _use_git(monkeypatch, fake_git_output)

    scope = diff_scope.resolve_scope("origin/main", repo_root=tmp_path, base_mode=BaseMode.DIRECT)

    assert scope.changed_lines == {}
    assert scope.file_hashes == {"src/deleted.py": "missing"}


def test_resolve_scope_preserves_unusual_untracked_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unusual_path = "src/é\nname.py"
    source = tmp_path / unusual_path
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_git_output(*args: str) -> str:
        if args == ("rev-parse", "origin/main"):
            return "base-sha"
        if args == ("rev-parse", "HEAD"):
            return "head-sha"
        if "--name-only" in args:
            return ""
        if args[-2:] == ("--unified=0", "base-sha"):
            return ""
        if args == ("ls-files", "-z", "--others", "--exclude-standard"):
            return f"{unusual_path}\0"
        raise AssertionError(f"unexpected git call: {args}")

    _use_git(monkeypatch, fake_git_output)

    scope = diff_scope.resolve_scope("origin/main", repo_root=tmp_path, base_mode=BaseMode.DIRECT)

    assert scope.changed_lines == {unusual_path: ()}
    assert scope.file_hashes == {unusual_path: hashlib.sha256(b"content").hexdigest()}


def test_resolve_scope_preserves_leading_whitespace_in_top_level_untracked_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    seed = tmp_path / "seed.py"
    seed.write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "seed.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    unusual_path = " leading.py"
    (tmp_path / unusual_path).write_text("content", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    scope = diff_scope.resolve_scope("HEAD", repo_root=tmp_path, base_mode=BaseMode.DIRECT)

    assert scope.changed_lines == {unusual_path: ()}
    assert scope.file_hashes == {unusual_path: hashlib.sha256(b"content").hexdigest()}


def test_untracked_files_other_than_python_sources_are_not_in_the_scope(tmp_path: Path) -> None:
    """Running the suite writes bytecode caches, logs and coverage files: untracked artifacts that would otherwise
    change the pinned scope mid-run. Only Python sources can be mutated, so only they belong to it."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.py").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "seed.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    (tmp_path / "src/__pycache__").mkdir(parents=True)
    (tmp_path / "src/new.py").write_text("NEW = 1\n", encoding="utf-8")
    (tmp_path / "src/__pycache__/new.cpython-314.pyc").write_bytes(b"bytecode")
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")

    pinned = diff_scope.resolve_scope("HEAD", repo_root=tmp_path, base_mode=BaseMode.DIRECT)
    (tmp_path / "run.log").write_text("written while the gate ran", encoding="utf-8")

    assert pinned.changed_lines == {"src/new.py": ()}
    assert set(pinned.file_hashes) == {"src/new.py"}
    assert diff_scope.scope_changes(pinned, repo_root=tmp_path).current


def test_scope_round_trip_preserves_tuple_lines(tmp_path: Path) -> None:
    path = tmp_path / "scope.json"
    scope = _scope(file_hash="hash")

    diff_scope.write_scope(scope, path)

    assert diff_scope.read_scope(path) == scope


def test_write_scope_creates_parent_directories(tmp_path: Path) -> None:
    # Two missing levels: ``parents=True`` creates both, ``parents=False`` cannot.
    path = tmp_path / "nested" / "deeper" / "scope.json"
    scope = _scope(file_hash="hash")

    diff_scope.write_scope(scope, path)

    assert path.exists()
    assert diff_scope.read_scope(path) == scope


def test_write_scope_overwrites_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "scope.json"
    path.write_text("old", encoding="utf-8")
    scope = _scope(file_hash="hash")

    diff_scope.write_scope(scope, path)

    assert diff_scope.read_scope(path) == scope


def test_write_scope_writes_readable_sorted_indented_json(tmp_path: Path) -> None:
    path = tmp_path / "scope.json"
    scope = _scope(file_hash="hash", base_ref="réf")

    diff_scope.write_scope(scope, path)

    text = path.read_text(encoding="utf-8")
    assert "réf" in text  # ensure_ascii=False keeps non-ASCII readable
    assert '\n  "base_ref"' in text  # indent=2
    assert text.index('"file_hashes"') < text.index('"head_sha"')  # sort_keys=True


def test_write_scope_pins_utf8_regardless_of_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unusual = "src/é.py"
    scope = DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="diff-base-sha",
        head_sha="head-sha",
        changed_lines={unusual: (1,)},
        file_hashes={unusual: "hash"},
    )
    path = tmp_path / "scope.json"
    encodings: list[str | None] = []
    real_write_text = Path.write_text

    def spy(self: Path, data: str, *, encoding: str | None = None, errors: str | None = None) -> int:
        encodings.append(encoding)
        return real_write_text(self, data, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "write_text", spy)
    diff_scope.write_scope(scope, path)
    assert encodings == ["utf-8"]
    assert diff_scope.read_scope(path) == scope


def test_scope_is_current_detects_changed_file_content(tmp_path: Path) -> None:
    source = tmp_path / "src/x.py"
    source.parent.mkdir()
    source.write_text("original", encoding="utf-8")
    scope = _scope(file_hash=hashlib.sha256(b"original").hexdigest())
    source.write_text("changed", encoding="utf-8")

    assert not diff_scope.scope_is_current(scope, repo_root=tmp_path)


def test_scope_is_current_detects_deletion_only_path_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="base-sha",
        head_sha="head-sha",
        changed_lines={},
        file_hashes={},
    )

    def fake_git_output(*args: str) -> str:
        command = args
        if command == ("rev-parse", "origin/main"):
            return "base-sha"
        if command == ("rev-parse", "HEAD"):
            return "head-sha"
        if "--name-only" in command:
            return "src/deleted.py\0"
        if command[-2:] == ("--unified=0", "base-sha"):
            return _DELETE_ONLY_DIFF
        if command == ("ls-files", "-z", "--others", "--exclude-standard"):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    _use_git(monkeypatch, fake_git_output)

    assert not diff_scope.scope_is_current(scope, repo_root=tmp_path)


def test_scope_is_current_accepts_unchanged_deletion_only_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="base-sha",
        head_sha="head-sha",
        changed_lines={},
        file_hashes={"src/deleted.py": "missing"},
    )

    def fake_git_output(*args: str) -> str:
        command = args
        if command == ("rev-parse", "origin/main"):
            return "base-sha"
        if command == ("rev-parse", "HEAD"):
            return "head-sha"
        if "--name-only" in command:
            return "src/deleted.py\0"
        if command[-2:] == ("--unified=0", "base-sha"):
            return _DELETE_ONLY_DIFF
        if command == ("ls-files", "-z", "--others", "--exclude-standard"):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    _use_git(monkeypatch, fake_git_output)

    assert diff_scope.scope_is_current(scope, repo_root=tmp_path)


_Y_DIFF = "+++ b/src/y.py\n@@ -0,0 +1 @@\n+third\n"


def _pinned_scope(tmp_path: Path) -> DiffScope:
    """A scope with a pinned merge base and a file whose content on disk matches the scope hash."""
    source = tmp_path / "src/x.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("original", encoding="utf-8")
    return DiffScope(
        base_ref="origin/main",
        base_sha="base-sha",
        diff_base_sha="merge-base-sha",
        head_sha="head-sha",
        changed_lines={"src/x.py": (10, 11)},
        file_hashes={"src/x.py": hashlib.sha256(b"original").hexdigest()},
    )


def _live_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    base_sha: str = "base-sha",
    head_sha: str = "head-sha",
    diff: str = _DIFF,
    names: str = "src/x.py\0",
) -> list[tuple[str, ...]]:
    """git of the current tree: refs may have moved, the diff is computed against the pinned merge base."""
    calls: list[tuple[str, ...]] = []

    def fake_git_output(*args: str) -> str:
        command = args
        calls.append(command)
        if command == ("rev-parse", "origin/main"):
            return base_sha
        if command == ("rev-parse", "HEAD"):
            return head_sha
        if "--name-only" in command:
            assert command[-1] == "merge-base-sha"
            return names
        if command[-2:] == ("--unified=0", "merge-base-sha"):
            return diff
        if command == ("ls-files", "-z", "--others", "--exclude-standard"):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    _use_git(monkeypatch, fake_git_output)
    return calls


def test_scope_changes_keeps_scope_current_when_only_base_ref_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `git fetch` in any worktree moves the shared base ref mid-run. The checked diff is pinned against the
    scope's merge base and does not change: the verdict is still about the same content."""
    scope = _pinned_scope(tmp_path)
    _live_git(monkeypatch, tmp_path, base_sha="fetched-base-sha")

    changes = diff_scope.scope_changes(scope, repo_root=tmp_path)

    assert changes == diff_scope.ScopeChanges(current=True, paths=(), moved_refs=("base_ref",))
    assert diff_scope.scope_is_current(scope, repo_root=tmp_path)


def test_scope_changes_keeps_scope_current_when_head_moved_to_commit_of_same_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Committing the edits under check moves HEAD, but the diff against the pinned merge base is the same."""
    scope = _pinned_scope(tmp_path)
    _live_git(monkeypatch, tmp_path, head_sha="committed-head-sha")

    changes = diff_scope.scope_changes(scope, repo_root=tmp_path)

    assert changes == diff_scope.ScopeChanges(current=True, paths=(), moved_refs=("head",))


def test_scope_changes_reports_both_moved_refs_in_stable_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _pinned_scope(tmp_path)
    _live_git(monkeypatch, tmp_path, base_sha="fetched-base-sha", head_sha="committed-head-sha")

    assert diff_scope.scope_changes(scope, repo_root=tmp_path).moved_refs == ("base_ref", "head")


def test_scope_changes_rejects_rebase_that_changes_diff_against_pinned_merge_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A HEAD move that changes the diff against the pinned merge base is different content."""
    scope = _pinned_scope(tmp_path)
    (tmp_path / "src/y.py").write_text("third", encoding="utf-8")
    _live_git(
        monkeypatch,
        tmp_path,
        head_sha="rebased-head-sha",
        diff=_DIFF + _Y_DIFF,
        names="src/x.py\0src/y.py\0",
    )

    changes = diff_scope.scope_changes(scope, repo_root=tmp_path)

    assert changes == diff_scope.ScopeChanges(current=False, paths=("src/y.py",), moved_refs=("head",))
    assert not diff_scope.scope_is_current(scope, repo_root=tmp_path)


def test_scope_changes_names_file_edited_during_run_without_asking_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _pinned_scope(tmp_path)
    calls = _live_git(monkeypatch, tmp_path)
    (tmp_path / "src/x.py").write_text("edited during run", encoding="utf-8")

    changes = diff_scope.scope_changes(scope, repo_root=tmp_path)

    assert changes == diff_scope.ScopeChanges(current=False, paths=("src/x.py",), moved_refs=())
    assert calls == []


def test_scope_changes_names_changed_lines_when_hashes_still_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same file and content, but other changed lines against the base (the base moved under the file)."""
    scope = _pinned_scope(tmp_path)
    _live_git(monkeypatch, tmp_path, diff="+++ b/src/x.py\n@@ -9,0 +10 @@\n+first\n")

    changes = diff_scope.scope_changes(scope, repo_root=tmp_path)

    assert changes == diff_scope.ScopeChanges(current=False, paths=("src/x.py",), moved_refs=())


def test_scope_changes_fails_closed_when_git_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scope = _pinned_scope(tmp_path)

    def broken_git(*_args: str) -> str:
        raise OSError("git unavailable")

    _use_git(monkeypatch, broken_git)

    changes = diff_scope.scope_changes(scope, repo_root=tmp_path)

    assert changes == diff_scope.ScopeChanges(current=False, paths=(), moved_refs=())


def test_mutant_in_scope_uses_full_span_and_whole_file_sentinel() -> None:
    changed = {"src/x.py": (12,), "src/new.py": ()}

    assert diff_scope.mutant_in_scope("src/x.py", 10, 12, changed)
    assert not diff_scope.mutant_in_scope("src/x.py", 8, 11, changed)
    assert diff_scope.mutant_in_scope("src/new.py", 500, 500, changed)
    # ``return False`` must stay a real bool: callers rely on the exact value, not its truthiness.
    assert diff_scope.mutant_in_scope("src/other.py", 12, 12, changed) is False


# the scope watch: an edit of a scope file stops a campaign at the next mutant boundary


def _source(root: Path, relative: str = "src/x.py", text: str = "def f(a, b):\n    return a < b\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _watch_over_edit(tmp_path: Path, *, advance: float) -> diff_scope.ScopeWatch:
    """A watch with a 5 s interval: one check before the edit, then the edit and a clock move of ``advance``."""
    edited = _source(tmp_path)
    now = [0.0]
    watch = diff_scope.ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path, interval=5.0, clock=lambda: now[0])
    assert watch.changed() == ()
    edited.write_text("X = 0\n", encoding="utf-8")
    now[0] = advance
    return watch


def test_files_changed_names_only_edited_files(tmp_path: Path) -> None:
    edited = _source(tmp_path, "src/a.py", "A = 1\n")
    _source(tmp_path, "src/b.py", "B = 1\n")
    pinned_scope = pinned(tmp_path, "src/a.py", "src/b.py")
    assert diff_scope.scope_files_changed(pinned_scope, repo_root=tmp_path) == ()
    edited.write_text("A = 2\n", encoding="utf-8")
    assert diff_scope.scope_files_changed(pinned_scope, repo_root=tmp_path) == ("src/a.py",)


def test_files_changed_tracks_deletion_and_keeps_pinned_deletion_quiet(tmp_path: Path) -> None:
    kept = _source(tmp_path, "src/kept.py", "K = 1\n")
    pinned_scope = pinned(tmp_path, "src/kept.py", "src/removed_in_diff.py")
    assert diff_scope.scope_files_changed(pinned_scope, repo_root=tmp_path) == ()
    kept.unlink()
    assert diff_scope.scope_files_changed(pinned_scope, repo_root=tmp_path) == ("src/kept.py",)


def test_watch_checks_at_most_once_per_interval_unless_forced(tmp_path: Path) -> None:
    watch = _watch_over_edit(tmp_path, advance=1.0)
    assert watch.changed() == ()
    assert watch.changed(force=True) == ("src/x.py",)


def test_watch_rechecks_once_the_interval_passes(tmp_path: Path) -> None:
    watch = _watch_over_edit(tmp_path, advance=5.0)
    assert watch.changed() == ("src/x.py",)


def test_watch_keeps_the_change_for_every_worker_even_after_a_restore(tmp_path: Path) -> None:
    # The campaign already ran on mixed revisions: restoring the file does not save it; every worker must stop.
    original = "def f(a, b):\n    return a < b\n"
    edited = _source(tmp_path, text=original)
    watch = diff_scope.ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path)
    edited.write_text("X = 0\n", encoding="utf-8")
    assert watch.changed(force=True) == ("src/x.py",)
    edited.write_text(original, encoding="utf-8")
    assert watch.changed() == ("src/x.py",)


def test_watch_default_interval_rechecks_after_two_seconds(tmp_path: Path) -> None:
    # The default interval is 2.0 s: a clock moving 2.5 s crosses it (and 3.0 s would not).
    edited = _source(tmp_path)
    now = [0.0]
    watch = diff_scope.ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path, clock=lambda: now[0])
    assert watch.changed() == ()
    edited.write_text("X = 0\n", encoding="utf-8")
    now[0] = 2.5
    assert watch.changed() == ("src/x.py",)


def test_watch_throttles_by_elapsed_time_not_absolute_clock(tmp_path: Path) -> None:
    edited = _source(tmp_path)
    now = [100.0]
    watch = diff_scope.ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path, clock=lambda: now[0])
    assert watch.changed() == ()  # pins _checked_at = 100.0
    edited.write_text("X = 0\n", encoding="utf-8")
    now[0] = 101.0  # 1.0 s elapsed < the 2.0 s interval: still throttled
    assert watch.changed() == ()


def test_watch_holds_its_lock_during_a_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _source(tmp_path)
    watch = diff_scope.ScopeWatch(pinned(tmp_path, "src/x.py"), repo_root=tmp_path)
    held: list[bool] = []

    def probe(scope: DiffScope, *, repo_root: Path) -> tuple[str, ...]:
        held.append(watch._lock.locked())
        return ()

    monkeypatch.setattr(diff_scope, "scope_files_changed", probe)
    watch.changed(force=True)
    assert held == [True]


def test_watch_internal_fields_are_not_init_params() -> None:
    init_names = {field.name for field in fields(diff_scope.ScopeWatch) if field.init}
    assert {"_lock", "_checked_at", "_changed"}.isdisjoint(init_names)


def test_module_level_constants_and_class_defaults() -> None:
    # Module- and class-level lines execute only at import; a reload inside the test body re-runs them so the
    # coverage substrate attributes them to this test (a COLD mutant then reads as unsupported, not a gap).
    importlib.reload(diff_scope)
    assert diff_scope._C_ESCAPE_BYTES == {
        "a": 0x07,
        "b": 0x08,
        "t": 0x09,
        "n": 0x0A,
        "v": 0x0B,
        "f": 0x0C,
        "r": 0x0D,
        '"': 0x22,
        "\\": 0x5C,
    }
    watch_fields = {field.name: field for field in fields(diff_scope.ScopeWatch)}
    assert watch_fields["interval"].default == 2.0
    assert all(field.init is False for field in fields(diff_scope.ScopeWatch) if field.name.startswith("_"))
