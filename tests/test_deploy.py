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


def test_public_service_is_public_and_sandboxed():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(DEPLOY / "breathwatch-public.service")
    s = cp["Service"]
    assert "PUBLIC_DEMO=1" in (DEPLOY / "breathwatch-public.service").read_text()
    assert s["ExecStart"] == "/opt/breathwatch/.venv/bin/python -m server.app"
    assert s["User"] == "breathwatch" and s["ReadWritePaths"] == "/opt/breathwatch/data"
    assert s["ProtectHome"] == "yes" and s["ProtectSystem"] == "strict"


def test_caddy_example_proxies_to_localhost():
    text = (DEPLOY / "Caddyfile.example").read_text()
    assert "reverse_proxy 127.0.0.1:8000" in text
