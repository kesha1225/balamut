"""PyMutBench: a reproducible benchmark of Python mutation-testing tools on CPython 3.14.

The harness is stdlib-only. Everything that executes third-party code (subject projects, their test suites
and the mutation tools themselves) runs inside the pinned container image described by ``bench/Dockerfile``.
"""

__version__ = "0.1.0"
