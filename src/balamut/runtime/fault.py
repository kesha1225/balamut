"""Runtime for fault mutants: an injected failure of an awaited dependency.

Fault operators rewrite a dependency call site:

- ``fault:raise-at:<method>`` — ``await s.commit()`` becomes ``await balamut_fault_raise("commit")``;
- ``fault:return-swap:<method>`` — ``await gw.refund(...)`` becomes ``not (await gw.refund(...))``: the
  provider reported "not done", so the recheck/compensation path runs without any exception.

Honest semantics of raise-at: the expected exception type is not guessed (the handler usually lives in a
caller and hard-coding a domain type is not an option). A *killed* raise-at mutant proves the covering tests
are sensitive to that dependency failing, not that a specific compensation is asserted. A *survived* one is
the valuable signal: the failure was swallowed by a broad ``except`` (a masking bug), or the path is not
covered. Whether a compensation is asserted is what return-swap measures.
"""

from __future__ import annotations

from typing import NoReturn

type FaultGlobals = dict[str, object]

FAULT_RAISE_NAME = "balamut_fault_raise"


class InjectedFaultError(Exception):
    """An injected dependency failure; a dedicated type so it is never mistaken for a real crash."""


def fault_raise(dep: str) -> NoReturn:
    """Stands in for the dependency call: raises when called, before the outer ``await`` runs."""
    raise InjectedFaultError(dep)


def fault_globals() -> FaultGlobals:
    """Names the plugin injects into the mutated module's globals for fault mutants."""
    return {FAULT_RAISE_NAME: fault_raise}
