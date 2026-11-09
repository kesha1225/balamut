from cf_hard_exit.guard import checked


def test_checked():
    assert checked(5) == 5
