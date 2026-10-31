"""Oracle-kind parity corpus: pytest-style tests with asserts."""


def test_math_holds() -> None:
    assert 2 + 2 == 4
    assert min(3, 4) == 3


def test_bounds() -> None:
    values = [1, 2, 3]
    assert len(values) == 3
    assert values[0] < values[2]


def test_raises() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        pass
    assert True
