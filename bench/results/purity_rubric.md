Each candidate is a validated bug-fix commit of a Python library: its fail-to-pass tests (the keys of
fail_to_pass_on_buggy_code) pass on the fixed code and fail on the code before the commit. Some commits fix a
fault and also add a feature or change other behaviour in the same change.

For each candidate decide:
- pure_fix: true when the commit only repairs the fault (refactoring, documentation and test-only changes do not
  count against it); false when it also adds capability or changes other behaviour.
- fault_tests: exactly the fail-to-pass test ids (copied verbatim from the packet) that check the FAULT. For a
  pure fix this is every fail-to-pass test. For a mixed commit, leave out the tests that only exercise the new
  capability or the other behaviour change. Never invent ids. If no test checks the fault, return an empty list.
- note: one sentence explaining the split (empty for a pure fix).
