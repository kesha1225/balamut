def greet(name):
    return "привет, " + name if name else "пусто"


def shout(word):
    return "«" + word.upper() + "»" if len(word) > 2 else word
