def _build():
    return {key: key * 2 for key in range(5)}


TABLE = _build()


def lookup(key):
    return TABLE[key]
