from functools import lru_cache


@lru_cache(maxsize=None)
def base_rate():
    return 10


def price(quantity):
    return quantity * base_rate()
