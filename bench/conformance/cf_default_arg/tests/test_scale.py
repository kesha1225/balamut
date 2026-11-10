from cf_default_arg.scale import scale


def test_default_factor():
    assert scale(3) == 6
