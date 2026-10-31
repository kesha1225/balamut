"""Project state locations (balamut.paths)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from balamut import paths
from balamut.config import default_profile
from balamut.core.hashing import sha


def test_the_state_dir_ignores_itself_in_git(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)

    paths.ensure_state_dir(profile)
    paths.ensure_state_dir(profile)

    assert (profile.state_dir / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_ensure_state_dir_returns_the_created_directory(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)

    result = paths.ensure_state_dir(profile)

    assert result == profile.state_dir
    assert result.is_dir()


def test_ensure_state_dir_creates_missing_parents(tmp_path: Path) -> None:
    profile = default_profile(tmp_path / "nested" / "deep")

    result = paths.ensure_state_dir(profile)

    assert result == profile.state_dir
    assert result.is_dir()


def test_campaign_scratch_and_substrate_paths(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)

    assert paths.campaign_scratch(profile) == profile.state_dir / "run"
    assert paths.substrate_path(profile) == profile.state_dir / "substrate" / "coverage.substrate"


def test_default_address_prefix_is_exact(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)

    expected = str(Path(tempfile.gettempdir()) / f"bm-{sha(str(profile.root))[:8]}")
    assert paths.default_address(profile) == expected


def test_an_existing_ignore_file_is_kept(tmp_path: Path) -> None:
    profile = default_profile(tmp_path)
    profile.state_dir.mkdir()
    (profile.state_dir / ".gitignore").write_text("custom\n", encoding="utf-8")

    paths.ensure_state_dir(profile)

    assert (profile.state_dir / ".gitignore").read_text(encoding="utf-8") == "custom\n"


def test_socket_addresses_stay_short_and_differ_per_project(tmp_path: Path) -> None:
    first = paths.default_address(default_profile(tmp_path / "a"))
    second = paths.default_address(default_profile(tmp_path / "b"))

    assert first != second
    # The longest derived daemon address: run suffix, calibration lane, index and a respawn.
    assert len(f"{first}.run-12345678.calibration.0.r1".encode()) <= 100
