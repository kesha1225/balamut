from cf_cached.rates import base_rate, price


def test_warm_cache():
    # Executes base_rate's body (and only this test does), but checks nothing about it.
    base_rate()


def test_price():
    # Reads the cached rate: base_rate's body never runs during this test.
    assert price(2) == 20
