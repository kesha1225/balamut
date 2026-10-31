"""Exit codes of ``balamut mutate``: the gate reads them from its campaign subprocess."""

from __future__ import annotations

from typing import Final

EXIT_USAGE: Final = 2
EXIT_REFRESH_BASELINE_FAILURE: Final = 4
EXIT_SCOPE_CHANGED: Final = 5
