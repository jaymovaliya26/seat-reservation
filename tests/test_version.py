import tomllib
from pathlib import Path

from app import __version__


def test_app_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert __version__ == pyproject["project"]["version"]
