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
    """Config safe for tests: ephemeral UDP port, in-memory database, login off."""
    return with_overrides(cfg, network={"udp_port": 0}, store={"path": ":memory:"},
                          auth={"enabled": False}, ward={"backfill_s": 0})


@pytest.fixture
def auth_cfg(test_cfg) -> Config:
    """test_cfg with login enforced (§19 auth tests)."""
    return with_overrides(test_cfg, auth={"enabled": True})
