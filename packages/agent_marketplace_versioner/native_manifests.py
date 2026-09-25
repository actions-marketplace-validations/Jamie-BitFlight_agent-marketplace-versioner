"""Discovery and source resolution for conventional native manifests."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_HARNESS_DIRECTORY = re.compile(r"\.[A-Za-z0-9_-]+-plugin")
_GIT_PATH = shutil.which("git")


class NativeManifestError(ValueError):
    """Raised when a native manifest cannot be interpreted safely."""


@dataclass(frozen=True)
class NativeManifest:
    """A discovered conventional manifest with its declared version location."""

    path: Path
    kind: Literal["plugin", "marketplace"]
    version_key_path: tuple[str, ...] | None


def manifest_kind(path: Path) -> Literal["plugin", "marketplace"] | None:
    """Classify a conventional native manifest path.

    Returns:
        The manifest kind, or None when the path is not conventional.
    """
    if path.name.endswith((".plugin.json", "-plugin.json")):
        return "plugin"
    if path.name == "marketplace.json" and path.parent.name == "plugins" and path.parent.parent.name == ".agents":
        return "marketplace"
    if not _HARNESS_DIRECTORY.fullmatch(path.parent.name):
        return None
    if path.name == "plugin.json":
        return "plugin"
    if path.name == "marketplace.json":
        return "marketplace"
    return None


def _git_visible_paths(root: Path) -> list[Path]:
    if _GIT_PATH is None:
        raise NativeManifestError("git executable not found in PATH")
    result = subprocess.run(
        [_GIT_PATH, "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() or "not a Git repository"
        raise NativeManifestError(message)
    paths = [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]
    return [path for path in paths if is_git_visible(root, path)]


def _is_gitignored(root: Path, path: Path) -> bool:
    if _GIT_PATH is None:
        raise NativeManifestError("git executable not found in PATH")
    result = subprocess.run(
        [_GIT_PATH, "-C", str(root), "check-ignore", "-q", "--", path.as_posix()], check=False, capture_output=True
    )
    return result.returncode == 0


def is_git_visible(root: Path, path: Path) -> bool:
    """Return whether a path is not ignored under a repository root."""
    return not _is_gitignored(root, path)


def marketplace_root(manifest: NativeManifest) -> Path:
    """Return the directory from which a marketplace's local sources resolve.

    Returns:
        The repository-relative catalog root.
    """
    if manifest.path.parent.name == "plugins" and manifest.path.parent.parent.name == ".agents":
        return manifest.path.parent.parent.parent
    return manifest.path.parent.parent


def _version_key_path(root: Path, path: Path, kind: Literal["plugin", "marketplace"]) -> tuple[str, ...] | None:
    if kind != "marketplace":
        return ("version",)
    try:
        data = json.loads((root / path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("version"), str):
        return ("version",)
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if isinstance(metadata, dict) and isinstance(metadata.get("version"), str):
        return ("metadata", "version")
    return None


def discover_manifests(root: Path = Path()) -> list[NativeManifest]:
    """Find every Git-visible conventional manifest below *root*.

    Returns:
        Native manifests sorted by repository-relative path.
    """
    manifests: list[NativeManifest] = []
    for path in _git_visible_paths(root):
        kind = manifest_kind(path)
        if kind is None:
            continue
        manifests.append(NativeManifest(path=path, kind=kind, version_key_path=_version_key_path(root, path, kind)))
    return sorted(manifests, key=lambda manifest: manifest.path.as_posix())


def marketplace_sources(manifest: NativeManifest, root: Path = Path()) -> list[Path]:
    """Return repository-relative local source roots declared by one marketplace.

    Returns:
        Existing local source roots in the marketplace order.
    """
    if manifest.kind != "marketplace":
        raise NativeManifestError(f"not a marketplace manifest: {manifest.path}")
    try:
        data = json.loads((root / manifest.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NativeManifestError(f"cannot read {manifest.path}") from error
    if not isinstance(data, dict) or not isinstance(data.get("plugins", []), list):
        raise NativeManifestError(f"invalid marketplace plugins list: {manifest.path}")

    source_roots: list[Path] = []
    repository_root = root.resolve()
    for entry in data["plugins"]:
        if not isinstance(entry, dict):
            raise NativeManifestError(f"invalid marketplace plugin entry: {manifest.path}")
        source = entry.get("source")
        if isinstance(source, dict):
            continue
        if not isinstance(source, str):
            raise NativeManifestError(f"invalid marketplace plugin source: {manifest.path}")
        if not source.startswith("."):
            continue
        source_path = (root / marketplace_root(manifest) / source).resolve()
        try:
            relative = source_path.relative_to(repository_root)
        except ValueError as error:
            raise NativeManifestError(f"marketplace source escapes repository: {source}") from error
        if relative not in source_roots:
            source_roots.append(relative)
    return source_roots


def manifest_root(manifest: NativeManifest) -> Path:
    """Return the source root that owns a conventional plugin manifest.

    Returns:
        The directory containing a loose manifest or enclosing a harness manifest.
    """
    if manifest.kind != "plugin":
        raise NativeManifestError(f"not a plugin manifest: {manifest.path}")
    loose = manifest.path.name.endswith((".plugin.json", "-plugin.json"))
    return manifest.path.parent if loose else manifest.path.parent.parent


def manifests_for_source(manifests: list[NativeManifest], source_root: Path) -> list[NativeManifest]:
    """Return all plugin manifests directly owned by one source root.

    Returns:
        Plugin manifests ordered by their repository-relative paths.
    """
    return [manifest for manifest in manifests if manifest.kind == "plugin" and manifest_root(manifest) == source_root]


def source_for_path(manifests: list[NativeManifest], path: Path) -> Path | None:
    """Find the most-specific conventional plugin source root containing *path*.

    Returns:
        The owning source root, or None when no native plugin owns the path.
    """
    roots = {manifest_root(manifest) for manifest in manifests if manifest.kind == "plugin"}
    containing = [root for root in roots if path == root or root in path.parents]
    if not containing:
        return None
    return max(containing, key=lambda root: len(root.parts))
