"""The version is written in two places; health reports one, the release tag checks the other."""

import tomllib
from pathlib import Path

from mail_dispatch import __version__


def test_package_version_matches_pyproject() -> None:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    assert __version__ == tomllib.loads(pyproject.read_text())["project"]["version"]
