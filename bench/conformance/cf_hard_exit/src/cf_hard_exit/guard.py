import os


def checked(value):
    if value < 0:
        os._exit(3)
    return value
