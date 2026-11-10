import random

from cf_flaky.core import double


def test_double_of_two():
    # 2 * 2 == 2 ** 2 == 2 + 2: this check cannot tell the operator mutants of `x * 2` apart.
    assert double(2) == 4


def test_unrelated_flaky():
    # Fails about one run in three whatever the code under test does.
    double(2)
    assert random.random() > 0.33
