from cf_unicode.greet import greet, shout


def test_greet():
    assert greet("Bob") == "привет, Bob"
    assert greet("") == "пусто"


def test_shout():
    assert shout("abc") == "«ABC»"
    assert shout("ab") == "ab"
