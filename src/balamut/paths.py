"""Where balamut keeps its state for a project: the substrate, campaign scratch files and daemon sockets."""

from __future__ import annotations

import tempfile
from pathlib import Path

from balamut.config import Profile
from balamut.core.hashing import sha


def ensure_state_dir(profile: Profile) -> Path:
    """Create the state directory; it ignores itself in git, so its artifacts never show up as untracked files."""
    profile.state_dir.mkdir(parents=True, exist_ok=True)
    ignore = profile.state_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return profile.state_dir


def substrate_path(profile: Profile) -> Path:
    """The coverage substrate of the project (with its metadata, costs and tree sidecars next to it)."""
    return profile.state_dir / "substrate" / "coverage.substrate"


def campaign_scratch(profile: Profile) -> Path:
    """Mutant specs, daemon logs and per-run artifacts."""
    return profile.state_dir / "run"


def default_address(profile: Profile) -> str:
    """The socket path prefix of the project's campaign daemons, in the per-user temporary directory.

    AF_UNIX paths are short (about a hundred bytes): the name stays compact so every derived daemon address
    (run suffix, lane, index, respawn) still fits and pytest-fast never has to bind through a working-directory
    change."""
    return str(Path(tempfile.gettempdir()) / f"bm-{sha(str(profile.root))[:8]}")
