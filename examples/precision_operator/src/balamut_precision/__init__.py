"""Example third-party operator kind for balamut: the precision of ``round()``.

The contract is one tuple of operator facts per kind; a project wires the kind in its config
file by importing it:

    # balamut_conf.py
    from balamut.config import init
    from balamut_precision import precision_kind

    init(register={"precision": precision_kind})

Most kinds are pure data: compose the generic pieces of ``balamut.operators.kit`` (a matcher,
a site finder, an operator name). The engine's fold runs the facts over one AST walk per file
and computes every byte span itself; the author only names the site and the replacement.
Anything the kit does not cover is one small frozen class with a ``sites(ctx)`` method — no
base class, no registration.
"""

from __future__ import annotations

from balamut.operators.facts import OperatorKind
from balamut.operators.kit import DropArgument, Library, Rewrite, ScaleLiteral, call_of, operator_name

precision_kind: OperatorKind = (
    Rewrite(
        match=call_of(func="round"),
        transform=ScaleLiteral(scale=10, index=1),
        operator=operator_name(Library.STD, "builtins", "ndigits-widen"),
    ),
    Rewrite(
        match=call_of(func="round"),
        transform=DropArgument(index=1),
        operator=operator_name(Library.STD, "builtins", "ndigits-drop"),
    ),
)
