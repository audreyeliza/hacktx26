import pytest

from server.config import Config, load_config


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config()
