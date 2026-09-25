from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Final

# Copyright (c) 2026 Jamie Nelson

ROOT: Final = Path(__file__).resolve().parents[1]


def test_package_imports() -> None:
    import agent_marketplace_versioner

    assert agent_marketplace_versioner.__name__ == "agent_marketplace_versioner"


def test_version_comes_from_the_release_tag_not_the_moving_major_tag(tmp_path: Path) -> None:
    from setuptools_scm import get_version

    raw_options = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["version"][
        "raw-options"
    ]
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "--initial-branch", "main")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "release")
    git("tag", "--annotate", "v1.0.0", "--message", "v1.0.0")
    env["GIT_COMMITTER_DATE"] = "2026-01-02T00:00:00Z"
    git("tag", "--annotate", "v1", "--message", "v1 -> v1.0.0")

    assert get_version(root=str(tmp_path), **raw_options) == "1.0.0"
