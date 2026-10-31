"""Heavy mutation operators from the literature, assembled from the kit's Lego pieces.

Every operator is a composition of frozen kit values — matchers (``&``/``|``/``~``), site
finders and replacement strategies — plus, for the one genuinely novel shape, a tiny region
rewriter. The sources:

- PIT (pitest.org/quickstart/mutators): NON_VOID_METHOD_CALLS, EXPERIMENTAL_ARGUMENT_PROPAGATION.
- Stryker (stryker-mutator.io/docs/mutation-testing-elements/supported-mutators): String Literal,
  Array Declaration, Object literal, Regex (a level-1 subset of weapon-regex).
- Bradbury, Cordy, Dingel, "Mutation Operators for Concurrent Java (J2SE 5.0)", Tech Report
  2006-520 (research.cs.queensu.ca): RTXC (remove thread calls), RNA (notify_all -> notify),
  MXT (modify sleep time), and the critical-region family (shift a statement out of the lock).
"""

from __future__ import annotations

import ast

from balamut.operators.facts import OperatorKind
from balamut.operators.kit import (
    AttrSwap,
    CallToArgument,
    EmptyCollection,
    HoistFirstSuiteStatement,
    Library,
    RegexRuleSite,
    RegionRewrite,
    Replace,
    Rewrite,
    SetFirstLiteralArg,
    call_of,
    const_of,
    expr_value,
    node_is,
    non_empty_collection,
    operator_name,
    result_is_used,
)

non_void_call = Replace(
    match=call_of() & result_is_used(),
    text="None",
    operator=operator_name(Library.LANG, "call", "nonvoid-default"),
)

arg_propagate = Rewrite(
    match=call_of() & result_is_used(),
    transform=CallToArgument(),
    operator=operator_name(Library.LANG, "call", "arg-propagate"),
)

string_to_empty = Replace(
    match=const_of(str, non_empty=True),
    text='""',
    operator=operator_name(Library.LANG, "str", "string-to-empty"),
)

collection_empty = Rewrite(
    match=non_empty_collection(),
    transform=EmptyCollection(),
    operator=operator_name(Library.LANG, "collection", "empty"),
)

regex_rewrite = Rewrite(
    match=call_of(
        attr=frozenset({"match", "search", "fullmatch", "sub", "split", "findall", "compile"}), receiver="re"
    ),
    transform=RegexRuleSite(
        rules=((r"(?<!\\)\^", ""), (r"(?<!\\)\$", ""), (r"\d", r"\D"), (r"\D", r"\d"), (r"(?<!\\)\+", "*"))
    ),
    operator=operator_name(Library.STD, "re", "regex-rewrite"),
)

sleep_removal = Replace(
    match=expr_value(call_of(attr="sleep")),
    text="pass",
    operator=operator_name(Library.STD, "time", "sleep-removal"),
)

thread_call_removal = Replace(
    match=expr_value(call_of(attr=frozenset({"join", "wait", "notify", "notify_all"}))),
    text="pass",
    operator=operator_name(Library.STD, "threading", "call-removal"),
)

notify_swap = Rewrite(
    match=call_of(attr="notify_all"),
    transform=AttrSwap(swaps=(("notify_all", "notify"),)),
    operator=operator_name(Library.STD, "threading", "notify-all-to-notify"),
)

sleep_time_zero = Rewrite(
    match=call_of(attr="sleep"),
    transform=SetFirstLiteralArg(text="0"),
    operator=operator_name(Library.STD, "time", "sleep-zero"),
)

wait_time_zero = Rewrite(
    match=call_of(attr=frozenset({"wait", "join"})),
    transform=SetFirstLiteralArg(text="0"),
    operator=operator_name(Library.STD, "threading", "wait-zero"),
)


critical_region_shift = RegionRewrite(
    match=node_is(ast.With, ast.AsyncWith),
    transform=HoistFirstSuiteStatement(),
    operator=operator_name(Library.LANG, "with", "critical-region-shift"),
)

complex_kind: OperatorKind = (
    non_void_call,
    arg_propagate,
    string_to_empty,
    collection_empty,
    regex_rewrite,
    sleep_removal,
    thread_call_removal,
    notify_swap,
    sleep_time_zero,
    wait_time_zero,
    critical_region_shift,
)
