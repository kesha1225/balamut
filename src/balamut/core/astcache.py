"""Memoized ``ast.parse`` for the hot key-derivation path.

Deriving a mutant's identity parses the same module text several times (enclosing function, function
source, start line, reachability). On an all-cache-hit rerun that parsing dominates, because no tests run.
Every consumer treats the tree as read-only, so one object can be shared; the bounded LRU keeps memory flat.
"""

from __future__ import annotations

import ast
from functools import lru_cache


@lru_cache(maxsize=512)
def parse_module(source: str) -> ast.Module:
    return ast.parse(source)
