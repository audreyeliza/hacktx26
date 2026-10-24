import dataclasses

import pytest

from server.config import Config, load_config


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config()


def with_overrides(cfg: Config, **sections: dict) -> Config:
    """Copy of cfg with some fields replaced, e.g. with_overrides(cfg, network={"udp_port": 0})."""
    changes = {name: dataclasses.replace(getattr(cfg, name), **fields) for name, fields in sections.items()}
    return dataclasses.replace(cfg, **changes)


@pytest.fixture
def test_cfg(cfg) -> Config:
    """Config safe for tests: ephemeral UDP port so nothing clashes with a running server."""
    return with_overrides(cfg, network={"udp_port": 0})
