"""Milestone 11 deployment files: syntax and consistency (no Pi needed)."""

import configparser
import subprocess

from server.config import ROOT

DEPLOY = ROOT / "deploy"
APP_DIR = "/home/audreybolyard/hacktx"


def unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(DEPLOY / "breathwatch.service")
    return cp


def test_service_runs_the_app_from_the_venv_as_the_user():
    s = unit()["Service"]
    assert s["User"] == "audreybolyard" and s["WorkingDirectory"] == APP_DIR
    assert s["ExecStart"] == f"{APP_DIR}/.venv/bin/python -m server.app"
    assert s["ReadWritePaths"] == f"{APP_DIR}/data" and s["ProtectSystem"] == "strict"
    assert s["SupplementaryGroups"] == "dialout"
    assert unit()["Install"]["WantedBy"] == "multi-user.target"


def test_scripts_parse_and_agree_on_paths():
    for name in ("pi_setup.sh", "push_to_pi.sh"):
        subprocess.run(["bash", "-n", str(DEPLOY / name)], check=True)
    setup = (DEPLOY / "pi_setup.sh").read_text()
    assert f'APP_DIR="{APP_DIR}"' in setup and "deploy/breathwatch.service" in setup
    push = (DEPLOY / "push_to_pi.sh").read_text()
    assert "audreybolyard.local" in push and "--exclude '.env'" in push   # key only with --env
