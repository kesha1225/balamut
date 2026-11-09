import asyncio

from cf_shapes.shapes import evens, fetch_total, make_adder


def test_closure():
    assert make_adder(3)(4) == 7


def test_generator():
    assert list(evens(6)) == [0, 2, 4]


def test_async():
    assert asyncio.run(fetch_total([1, 2, 3])) == 6
