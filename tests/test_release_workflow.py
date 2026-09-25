from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_release_workflow_advances_v1_without_rewriting_release_tags(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    release_steps = workflow["jobs"]["release"]["steps"]
    advance_v1 = next(step for step in release_steps if step.get("name") == "Advance v1 compatibility tag")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--initial-branch", "main", repo], check=True, capture_output=True)
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("first release\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "first release")
    first_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "--quiet", "-u", "origin", "main")
    git(repo, "tag", "--annotate", "v1.0.0", "--message", "v1.0.0")
    git(repo, "push", "--quiet", "origin", "v1.0.0")
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")

    env = {**os.environ, "RELEASE_TAG": "v1.0.0"}
    subprocess.run(["bash", "-eo", "pipefail", "-c", advance_v1["run"]], cwd=repo, env=env, check=True)
    assert git(repo, "ls-remote", "origin", "refs/tags/v1^{}").split()[0] == first_commit

    (repo / "README.md").write_text("second release\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "second release")
    second_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "--quiet", "origin", "main")
    git(repo, "tag", "--annotate", "v1.0.1", "--message", "v1.0.1")
    git(repo, "push", "--quiet", "origin", "v1.0.1")

    env["RELEASE_TAG"] = "v1.0.1"
    subprocess.run(["bash", "-eo", "pipefail", "-c", advance_v1["run"]], cwd=repo, env=env, check=True)
    assert git(repo, "ls-remote", "origin", "refs/tags/v1^{}").split()[0] == second_commit

    git(repo, "tag", "--annotate", "v1.not-semver", "--message", "v1.not-semver")
    git(repo, "push", "--quiet", "origin", "v1.not-semver")
    env["RELEASE_TAG"] = "v1.not-semver"
    subprocess.run(["bash", "-eo", "pipefail", "-c", advance_v1["run"]], cwd=repo, env=env, check=True)
    assert git(repo, "ls-remote", "origin", "refs/tags/v1^{}").split()[0] == second_commit
    assert git(repo, "ls-remote", "origin", "refs/tags/v1.0.0^{}").split()[0] == first_commit

    git(repo, "tag", "--annotate", "v2.0.0", "--message", "v2.0.0")
    git(repo, "push", "--quiet", "origin", "v2.0.0")
    env["RELEASE_TAG"] = "v2.0.0"
    subprocess.run(["bash", "-eo", "pipefail", "-c", advance_v1["run"]], cwd=repo, env=env, check=True)
    assert git(repo, "ls-remote", "origin", "refs/tags/v1^{}").split()[0] == second_commit


def test_release_workflow_derives_breaking_and_explicit_release_versions(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    bump = next(step for step in workflow["jobs"]["release"]["steps"] if step.get("id") == "bump")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--initial-branch", "main", repo], check=True, capture_output=True)
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    output = tmp_path / "github-output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}

    def commit_and_bump(message: str) -> str:
        git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
        output.write_text("", encoding="utf-8")
        subprocess.run(["bash", "-eo", "pipefail", "-c", bump["run"]], cwd=repo, env=env, check=True)
        return output.read_text(encoding="utf-8")

    assert commit_and_bump("feat: untagged history") == ""
    git(repo, "tag", "--annotate", "v0.1.2", "--message", "v0.1.2")
    assert commit_and_bump("fix: patch release") == "custom_tag=\n"
    assert commit_and_bump("feat(versioner)!: stabilize contract") == "custom_tag=1.0.0\n"
    git(repo, "tag", "--annotate", "v1.0.0", "--message", "v1.0.0")
    git(repo, "tag", "--annotate", "v1", "--message", "v1 -> v1.0.0")
    assert commit_and_bump("docs: explicit release\n\nRelease-As: v3.0.0") == "custom_tag=3.0.0\n"
