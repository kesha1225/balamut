import pytest

from cf_session_fixture.config import load_config


@pytest.fixture(scope="session")
def config():
    return load_config()
