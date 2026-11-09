import asyncio


def make_adder(step):
    def add(value):
        return value + step

    return add


def evens(limit):
    for number in range(limit):
        if number % 2 == 0:
            yield number


async def fetch_total(values):
    await asyncio.sleep(0)
    return sum(values)
