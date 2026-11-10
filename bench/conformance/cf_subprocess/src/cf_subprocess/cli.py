import sys


def describe(args):
    if len(args) > 1:
        return "many"
    return "one"


if __name__ == "__main__":
    print(describe(sys.argv[1:]))
