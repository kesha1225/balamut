from cf_import_time.table import lookup


def test_lookup():
    assert lookup(3) == 6
