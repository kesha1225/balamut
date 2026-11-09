"""Subject projects of the real-fault corpus: where they come from and how their suites run.

A subject is data: the repository, the import root of its package, the packages whose import origin is checked,
how its test environment is installed and any pytest arguments its configuration needs to run under the
reference plugin. The install line installs the dependencies and the project itself (non-editable, for its
distribution metadata); every judged tree is imported from its own checkout through ``PYTHONPATH``, which
precedes ``site-packages``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Subject:
    name: str
    repo: str  # GitHub owner/name
    packages: tuple[str, ...]  # top-level import names (the import-origin check and the mutation scope)
    install: tuple[str, ...]  # arguments of `uv pip install`, run in the repository root at HEAD
    pytest_args: tuple[str, ...] = ()  # appended to every pytest invocation
    tests: tuple[str, ...] = ("tests",)  # the suite's roots, as the project's own test command passes them
    since: str = "2021-01-01"  # the mining window starts here (author date of the fix)
    # Source files that are never mutation targets (generated, vendored or compatibility shims).
    exclude_src: tuple[str, ...] = ()
    # Files written into every checkout before it runs (e.g. a version module generated at build time).
    generated: tuple[tuple[str, str], ...] = field(default=())


SUBJECTS: dict[str, Subject] = {
    subject.name: subject
    for subject in (
        Subject("attrs", "python-attrs/attrs", ("attr", "attrs"), ("--group", "tests", ".")),
        # The preconf converters' optional serialization libraries are part of the suite cattrs runs.
        Subject("cattrs", "python-attrs/cattrs", ("cattrs", "cattr"),
                (".", "hypothesis", "pytest", "immutables", "pytest-benchmark", "msgspec", "orjson", "ujson",
                 "msgpack", "PyYAML", "tomlkit", "cbor2", "pymongo")),
        Subject("click", "pallets/click", ("click",), (".", "pytest")),
        Subject("marshmallow", "marshmallow-code/marshmallow", ("marshmallow",), (".", "pytest", "simplejson", "pytz")),
        Subject("more-itertools", "more-itertools/more-itertools", ("more_itertools",), (".", "pytest")),
        Subject("packaging", "pypa/packaging", ("packaging",),
                (".", "pytest", "pretend", "hypothesis", "tomli_w", "pip")),
        # tests/test_toml_tests.py reads the toml-test git submodule, which a plain checkout does not contain.
        Subject("tomlkit", "python-poetry/tomlkit", ("tomlkit",), (".", "pytest", "PyYAML"),
                pytest_args=("--ignore=tests/test_toml_tests.py",)),
        Subject("python-pathspec", "cpburnz/python-pathspec", ("pathspec",), (".", "pytest", "typing-extensions")),
        Subject("sqlparse", "andialbrecht/sqlparse", ("sqlparse",), (".", "pytest")),
        Subject("python-tabulate", "astanin/python-tabulate", ("tabulate",), (".", "pytest", "wcwidth"),
                pytest_args=("-o", "addopts="), tests=("test",)),
        Subject("humanize", "python-humanize/humanize", ("humanize",), (".", "freezegun", "pytest", "coverage")),
        Subject("python-dotenv", "theskumar/python-dotenv", ("dotenv",), (".", "pytest", "click", "sh")),
        Subject("boltons", "mahmoud/boltons", ("boltons",), (".", "pytest")),
        Subject("cachetools", "tkem/cachetools", ("cachetools",), (".", "pytest")),
        Subject("inflect", "jaraco/inflect", ("inflect",), (".", "pytest", "pygments")),
        Subject("arrow", "arrow-py/arrow", ("arrow",),
                (".", "pytest", "pytest-mock", "dateparser", "pytz", "simplejson"), pytest_args=("-o", "addopts=")),
        Subject("pyparsing", "pyparsing/pyparsing", ("pyparsing",), (".", "pytest", "railroad-diagrams", "jinja2")),
        Subject("pyflakes", "PyCQA/pyflakes", ("pyflakes",), (".", "pytest"), tests=("pyflakes/test",)),
        # Older rich tests import pkg_resources, which setuptools 81 removed.
        Subject("rich", "Textualize/rich", ("rich",), (".", "pytest", "attrs", "setuptools<81")),
        Subject("mistune", "lepture/mistune", ("mistune",), (".", "pytest")),
        Subject("python-semver", "python-semver/python-semver", ("semver",), (".", "pytest"),
                pytest_args=("-o", "addopts=")),
        Subject("parse", "r1chardj0n3s/parse", ("parse",), (".", "pytest"), pytest_args=("-o", "addopts="),
                tests=("tests", "test_parse.py")),
        Subject("jinja2", "pallets/jinja2", ("jinja2",), (".", "pytest", "trio")),
        Subject("dateutil", "dateutil/dateutil", ("dateutil",), (".", "pytest", "freezegun", "hypothesis", "attrs"),
                generated=(("src/dateutil/_version.py", "__version__ = version = '0+pymutbench'\n"),)),
        Subject("pyupgrade", "asottile/pyupgrade", ("pyupgrade",), (".", "pytest")),
        Subject("portion", "AlexandreDecan/portion", ("portion",), (".", "pytest", "pytest-benchmark"),
                pytest_args=("-o", "addopts=")),
        Subject("toolz", "pytoolz/toolz", ("toolz", "tlz"), (".", "pytest"), tests=("toolz/tests",)),
    )
}
