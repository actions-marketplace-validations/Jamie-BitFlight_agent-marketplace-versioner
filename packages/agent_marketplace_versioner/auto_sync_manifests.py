#!/usr/bin/env python3
"""Automatically sync plugin.json and marketplace.json based on git changes.

This script has three modes:

1. **Pre-commit mode** (default): Detects CRUD operations on plugins and their
   components during pre-commit, then updates manifests and bumps versions.

2. **Reconcile mode** (``--reconcile``): Full directory scan that compares
   filesystem state against plugin.json and marketplace.json entries.  Adds
   missing components, removes stale references, and reports drift.

3. **Sync-marketplace mode** (``--sync-marketplace``): Post-merge CI mode.
   Reconciles the marketplace.json plugin list against disk, then bumps
   the marketplace version. Called by CI after push to main so that
   marketplace.json version bumps do not appear in PR branches.

CRUD Detection (pre-commit mode):
- Plugin created: New plugins/ directory with .claude-plugin/plugin.json
- Plugin deleted: Removed plugins/ directory
- Component created: New skill/agent/command/hook/mcp file
- Component deleted: Removed skill/agent/command/hook/mcp file

Version Bumping:
- New plugin: Bump marketplace minor version
- Deleted plugin: Bump marketplace major version
- New component in plugin: Bump plugin minor version
- Deleted component: Bump plugin major version
- Modified component: Bump plugin patch version
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from io import TextIOWrapper

# Ensure UTF-8 output on Windows (cp1252 default cannot encode emoji/spinner chars).
# reconfigure() is available on Python 3.7+ when stdout is a TextIOWrapper.
if isinstance(sys.stdout, TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from collections import defaultdict
from pathlib import Path
from typing import Literal, TypedDict, TypeGuard

from agent_marketplace_versioner.native_manifests import (
    NativeManifest,
    discover_manifests,
    is_git_visible,
    manifest_kind,
    manifest_root,
    manifests_for_source,
    marketplace_root,
    source_for_path,
)

# Git status parsing constants
_MIN_PLUGIN_PATH_PARTS = 2
_MIN_COMPONENT_PATH_PARTS = 3
_MIN_SKILL_PATH_PARTS = 4
_MIN_DIRECT_COMPONENT_PATH_PARTS = 2

# Resolve git binary once at module load
_GIT_PATH: str | None = shutil.which("git")


class PluginPathInfo(TypedDict):
    """Parsed plugin path information."""

    plugin: str
    component_type: str | None
    component_path: str | None


class ComponentChange(TypedDict):
    """Component change information."""

    component_type: str
    component_path: str


class ComponentChanges(TypedDict):
    """Changes for a plugin's components."""

    added: list[ComponentChange]
    deleted: list[ComponentChange]
    modified: list[ComponentChange]


class _GitStatus(TypedDict):
    added: list[str]
    deleted: list[str]
    modified: list[str]
    relocated_manifests: dict[str, str]


class MarketplaceChanges(TypedDict):
    """Changes for marketplace plugins."""

    added: set[str]
    deleted: set[str]
    modified: list[tuple[str, str]]


class _MarketplaceMetadata(TypedDict, total=False):
    """Typed metadata sub-object in marketplace.json."""

    version: str


class _MarketplacePluginEntry(TypedDict):
    """Typed plugin entry in the marketplace.json plugins list."""

    name: str
    source: str | dict[str, object]


class _MarketplaceJsonData(TypedDict, total=False):
    """Typed structure of marketplace.json."""

    metadata: _MarketplaceMetadata
    plugins: list[_MarketplacePluginEntry]
    version: str


def _is_str_dict(obj: object) -> TypeGuard[dict[str, object]]:
    """Return True when obj is a dict with exclusively string keys.

    Args:
        obj: Any Python object to test.

    Returns:
        True if obj is a dict and every key is a str.
    """
    return isinstance(obj, dict) and all(isinstance(k, str) for k in obj)


def run_git_command(args: list[str]) -> str:
    """Run git command and return output.

    Args:
        args: List of git command arguments (e.g., ['status', '--short'])

    Returns:
        Git command stdout output stripped of whitespace

    Raises:
        FileNotFoundError: If git binary is not found in PATH.
    """
    if _GIT_PATH is None:
        msg = "git executable not found in PATH"
        raise FileNotFoundError(msg)

    result = subprocess.run([_GIT_PATH, *args], capture_output=True, text=True, check=False)
    if result.returncode != 0 and result.stderr:
        sys.stderr.write(f"git {' '.join(args)}: {result.stderr.strip()}\n")
    return result.stdout.strip()


def _run_git_bytes(args: list[str]) -> bytes:
    if _GIT_PATH is None:
        msg = "git executable not found in PATH"
        raise FileNotFoundError(msg)
    result = subprocess.run([_GIT_PATH, *args], capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git {' '.join(args)} failed")
    return result.stdout


def _native_manifest_root(path: Path) -> Path:
    return path.parent if path.name.endswith((".plugin.json", "-plugin.json")) else path.parent.parent


def _is_native_manifest_relocation(source: Path, destination: Path) -> bool:
    if manifest_kind(source) != "plugin" or manifest_kind(destination) != "plugin":
        return False
    source_data = read_ref_json("HEAD", source)
    destination_data = _read_staged_json(destination)
    return (
        isinstance(source_data, dict)
        and isinstance(destination_data, dict)
        and isinstance(source_data.get("name"), str)
        and source_data.get("name") == destination_data.get("name")
    )


def _staged_name_status_entries() -> list[tuple[str, Path, Path | None]]:
    fields = (
        _run_git_bytes(["diff", "--cached", "-M", "--name-status", "-z"])
        .decode("utf-8", errors="surrogateescape")
        .split("\0")
    )
    entries: list[tuple[str, Path, Path | None]] = []
    index = 0
    while index < len(fields):
        operation = fields[index]
        index += 1
        if not operation:
            continue
        if index >= len(fields):
            continue
        if operation.startswith("R") and index + 1 < len(fields):
            entries.append((operation, Path(fields[index]), Path(fields[index + 1])))
            index += 1
        else:
            entries.append((operation, Path(fields[index]), None))
        index += 1
    return entries


def _relocated_manifest_pairs(entries: list[tuple[str, Path, Path | None]]) -> dict[Path, Path]:
    deleted = [source for operation, source, destination in entries if operation == "D" or destination is not None]
    added = [
        destination if destination is not None else source
        for operation, source, destination in entries
        if operation == "A" or destination is not None
    ]
    relocated: dict[Path, Path] = {}
    for source in deleted:
        source_relative = source.relative_to(_native_manifest_root(source))
        matches = [
            destination
            for destination in added
            if destination.relative_to(_native_manifest_root(destination)) == source_relative
            and _is_native_manifest_relocation(source, destination)
        ]
        if len(matches) == 1:
            relocated[matches[0]] = source
    return relocated


def _relocated_file_pairs(
    entries: list[tuple[str, Path, Path | None]], relocated_manifests: dict[Path, Path]
) -> dict[Path, Path]:
    deleted = {source for operation, source, destination in entries if operation == "D" or destination is not None}
    added = {
        destination if destination is not None else source
        for operation, source, destination in entries
        if operation == "A" or destination is not None
    }
    relocated_paths: dict[Path, Path] = {}
    for destination, source in relocated_manifests.items():
        old_root = _native_manifest_root(source)
        new_root = _native_manifest_root(destination)
        for path in deleted:
            if path == old_root or old_root not in path.parents:
                continue
            new_path = new_root / path.relative_to(old_root)
            if new_path in added:
                relocated_paths[new_path] = path
    return relocated_paths


def _relocated_deletions(
    entries: list[tuple[str, Path, Path | None]], relocated_manifests: dict[Path, Path]
) -> dict[Path, Path]:
    deleted = {source for operation, source, destination in entries if operation == "D" or destination is not None}
    added = {
        destination if destination is not None else source
        for operation, source, destination in entries
        if operation == "A" or destination is not None
    }
    relocated: dict[Path, Path] = {}
    for destination, source in relocated_manifests.items():
        old_root = _native_manifest_root(source)
        new_root = _native_manifest_root(destination)
        for path in deleted:
            if path != old_root and old_root not in path.parents:
                continue
            new_path = new_root / path.relative_to(old_root)
            if new_path not in added:
                relocated[path] = new_path
    return relocated


def _categorize_staged_entries(
    entries: list[tuple[str, Path, Path | None]],
    relocated_paths: dict[Path, Path],
    relocated_deletions: dict[Path, Path],
) -> _GitStatus:
    status: _GitStatus = {"added": [], "deleted": [], "modified": [], "relocated_manifests": {}}
    for operation, source, destination in entries:
        if operation == "A" and source in relocated_paths:
            status["modified"].append(source.as_posix())
        elif operation == "D" and source in relocated_deletions:
            status["deleted"].append(relocated_deletions[source].as_posix())
        elif destination is not None and relocated_paths.get(destination) == source:
            status["modified"].append(destination.as_posix())
        elif destination is None:
            if operation == "A":
                status["added"].append(source.as_posix())
            elif operation == "D":
                status["deleted"].append(source.as_posix())
            elif operation == "M":
                status["modified"].append(source.as_posix())
        else:
            status["deleted"].append(source.as_posix())
            status["added"].append(destination.as_posix())
    return status


def get_git_status() -> _GitStatus:
    """Get staged file changes categorized by operation.

    Returns:
        Paths grouped by operation and paired native manifest relocations.
    """
    entries = _staged_name_status_entries()
    relocated_manifests = _relocated_manifest_pairs(entries)
    status = _categorize_staged_entries(
        entries, _relocated_file_pairs(entries, relocated_manifests), _relocated_deletions(entries, relocated_manifests)
    )
    status["relocated_manifests"] = {
        destination.as_posix(): source.as_posix() for destination, source in relocated_manifests.items()
    }
    return status


def _native_component_path(source_root: Path, filepath: Path, operation: str) -> ComponentChange:
    relative = filepath.relative_to(source_root)
    parts = relative.parts
    relative_path = relative.as_posix()
    match parts:
        case ("skills", skill_name, *_) if not skill_name.startswith("."):
            skill_md = source_root / "skills" / skill_name / "SKILL.md"
            component_type = (
                "skill"
                if filepath.name == "SKILL.md"
                or (operation != "deleted" and skill_md in _staged_paths() and is_git_visible(Path(), skill_md))
                else "other"
            )
            component_path = relative_path if component_type == "other" else f"skills/{skill_name}"
        case ("agents", _) if filepath.suffix == ".md":
            component_type = "agent"
            component_path = relative_path
        case ("commands", _) if filepath.suffix == ".md":
            component_type = "command"
            component_path = relative_path
        case ("hooks", *_) if filepath.suffix == ".json":
            component_type = "hook"
            component_path = relative_path
        case ("mcp", *_):
            component_type = "mcp"
            component_path = relative_path
        case _:
            component_type = "other"
            component_path = relative_path
    return {"component_type": component_type, "component_path": component_path}


def _native_file_changes(manifests: list[NativeManifest], status: _GitStatus) -> dict[Path, ComponentChanges]:
    changes: dict[Path, ComponentChanges] = defaultdict(lambda: {"added": [], "deleted": [], "modified": []})
    manifest_paths = {manifest.path for manifest in manifests if manifest.kind == "plugin"}
    manifests_per_root: dict[Path, int] = defaultdict(int)
    for manifest in manifests:
        if manifest.kind == "plugin":
            manifests_per_root[manifest_root(manifest)] += 1
    deleted_names = {
        data.get("name")
        for path in status["deleted"]
        if isinstance(data := read_ref_json("HEAD", path), dict) and isinstance(data.get("name"), str)
    }
    for operation in ("added", "deleted", "modified"):
        for raw_path in status[operation]:
            filepath = Path(raw_path)
            source_root = source_for_path(manifests, filepath)
            staged_data = _read_staged_json(filepath) if filepath in manifest_paths else None
            staged_name = staged_data.get("name") if staged_data is not None else None
            if source_root is None or (
                filepath in manifest_paths
                and operation != "modified"
                and manifests_per_root[source_root] == 1
                and staged_name not in deleted_names
            ):
                continue
            changes[source_root][operation].append(_native_component_path(source_root, filepath, operation))
    for source in status.get("relocated_manifests", {}).values():
        source_path = Path(source)
        source_root = _native_manifest_root(source_path)
        if manifests_for_source(manifests, source_root):
            deleted_change = _native_component_path(source_root, source_path, "deleted")
            if deleted_change not in changes[source_root]["deleted"]:
                changes[source_root]["deleted"].append(deleted_change)
    return changes


def _shared_target_version(
    manifests: list[NativeManifest], changes: ComponentChanges, relocated_manifests: dict[Path, Path]
) -> str | None:
    source_versions: list[str] = []
    baselines: list[tuple[str, tuple[int, int, int] | None]] = []
    for manifest in manifests:
        staged_data = _read_staged_json(manifest.path)
        current_version = _extract_str_version(staged_data, "version") or "0.0.0"
        source_versions.append(current_version)
        baseline_path = (
            manifest.path
            if read_ref_json("HEAD", manifest.path) is not None
            else relocated_manifests.get(manifest.path)
        )
        baseline_version = (
            _extract_str_version(read_ref_json("HEAD", baseline_path), "version") if baseline_path else None
        )
        baselines.append((current_version, _parse_version_tuple(baseline_version) if baseline_version else None))
    if not source_versions:
        return None
    source_version = max(source_versions, key=lambda version: _parse_version_tuple(version) or (0, 0, 0))
    source_tuple = _parse_version_tuple(source_version)
    baseline_tuples = [baseline for _, baseline in baselines if baseline is not None]
    already_bumped = [
        current_tuple is not None and baseline is not None and current_tuple > baseline
        for current, baseline in baselines
        if (current_tuple := _parse_version_tuple(current)) is not None
    ]
    has_existing_bump = any(already_bumped)
    covered = [
        current_tuple is not None
        and (
            (baseline is not None and current_tuple > baseline)
            or (baseline is None and has_existing_bump and current == source_version)
        )
        for current, baseline in baselines
        if (current_tuple := _parse_version_tuple(current)) is not None
    ]
    if source_tuple is not None and (not baseline_tuples or source_tuple > max(baseline_tuples) or all(covered)):
        return source_version
    return bump_version(source_version, _determine_bump_type(changes))


def sync_staged_manifests(root: Path = Path()) -> dict[Path, str]:
    """Apply staged content changes to every affected native plugin manifest.

    Returns:
        The source roots whose version-owning manifests were updated.
    """
    staged_paths = _staged_paths()
    manifests = [manifest for manifest in discover_manifests(root) if manifest.path in staged_paths]
    updated: dict[Path, str] = {}
    status = get_git_status()
    relocated_manifests = {
        Path(destination): Path(source) for destination, source in status.get("relocated_manifests", {}).items()
    }
    for source_root, changes in _native_file_changes(manifests, status).items():
        source_manifests = manifests_for_source(manifests, source_root)
        target_version = _shared_target_version(source_manifests, changes, relocated_manifests)
        if target_version is None:
            continue
        changed = False
        for manifest in source_manifests:
            original_content = manifest.path.read_bytes()
            unstaged_change = _has_unstaged_change(manifest.path)
            staged_data = _read_staged_json(manifest.path)
            if staged_data is None:
                continue
            _write_json_lf(manifest.path, _format_json(staged_data))
            manifest_updated, _ = _update_plugin_manifest(
                manifest.path, changes, sync_components=True, compare_to_head=True
            )
            generated_data = json.loads(manifest.path.read_text(encoding="utf-8"))
            if generated_data.get("version") != target_version:
                generated_data["version"] = target_version
            generated_content = _format_json(generated_data)
            manifest_updated = generated_content != _format_json(staged_data)
            if manifest_updated:
                _write_json_lf(manifest.path, generated_content)
            changed |= manifest_updated
            if manifest_updated and generated_data is not None:
                _stage_json(manifest.path, generated_data)
            if unstaged_change or not manifest_updated:
                manifest.path.write_bytes(original_content)
        if changed:
            updated[source_root] = target_version
    sync_native_marketplaces(root, bump=False, manifests=manifests, preserve_unstaged=True)
    return updated


def _native_plugin_name(manifest: NativeManifest, root: Path, *, staged: bool = False) -> str:
    try:
        data = _read_staged_json(manifest.path) if staged else None
        if data is None:
            data = json.loads((root / manifest.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return manifest_root(manifest).name
    if isinstance(data, dict) and isinstance(name := data.get("name"), str):
        return name
    return manifest_root(manifest).name


def _marketplace_local_entries(
    data: _MarketplaceJsonData, marketplace: NativeManifest, root: Path
) -> dict[Path, _MarketplacePluginEntry]:
    entries: dict[Path, _MarketplacePluginEntry] = {}
    plugins = data.get("plugins", [])
    for entry in plugins:
        source = _marketplace_entry_source(entry)
        if source is not None and (path := _marketplace_local_source_path(source, marketplace, root)) is not None:
            entries[path] = entry
    return entries


def _marketplace_entry_source(entry: _MarketplacePluginEntry) -> str | None:
    source = entry.get("source")
    if isinstance(source, str):
        return source if source.startswith(".") else None
    if _is_str_dict(source) and source.get("source") == "local":
        path = source.get("path")
        if not isinstance(path, str):
            return None
        return path if path.startswith(".") else None
    return None


def _marketplace_local_source_path(source: str, marketplace: NativeManifest, root: Path) -> Path | None:
    try:
        return (root / marketplace_root(marketplace) / source).resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _bump_native_marketplace_version(
    data: _MarketplaceJsonData, marketplace: NativeManifest, bump_type: Literal["major", "minor", "patch"]
) -> None:
    if marketplace.version_key_path == ("version",):
        current_version = data.get("version", "0.0.0")
        data["version"] = bump_version(current_version if isinstance(current_version, str) else "0.0.0", bump_type)
        return
    metadata = data.setdefault("metadata", {})
    current_version = metadata.get("version", "0.0.0")
    metadata["version"] = bump_version(current_version if isinstance(current_version, str) else "0.0.0", bump_type)


def _marketplace_differs_from_head(path: Path) -> bool:
    if _GIT_PATH is None:
        return False
    return subprocess.run([_GIT_PATH, "diff", "--quiet", "HEAD", "--", path.as_posix()], check=False).returncode == 1


def _source_differs_between_refs(
    root: Path, source: Path, marketplace_path: Path, base_ref: str | None, head_ref: str
) -> bool:
    if _GIT_PATH is None or base_ref is None:
        return False
    result = subprocess.run(
        [
            _GIT_PATH,
            "-C",
            str(root),
            "diff",
            "--quiet",
            base_ref,
            head_ref,
            "--",
            source.as_posix(),
            f":(exclude){marketplace_path.as_posix()}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise RuntimeError(result.stderr.strip() or f"cannot compare {base_ref} and {head_ref}")


def _sync_native_marketplace(
    root: Path,
    marketplace: NativeManifest,
    manifests: list[NativeManifest],
    *,
    bump: bool,
    dry_run: bool,
    base_ref: str | None,
    head_ref: str,
    staged_names: bool = False,
) -> bool:
    marketplace_path = root / marketplace.path
    manual_version = (
        bump
        and marketplace.version_key_path is not None
        and _version_already_bumped(marketplace.path, list(marketplace.version_key_path))
    )
    try:
        data: _MarketplaceJsonData = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    local_entries = _marketplace_local_entries(data, marketplace, root)
    source_parents = {source.parent for source in local_entries}
    local_plugins = {
        manifest_root(manifest): manifest
        for manifest in manifests
        if manifest.kind == "plugin"
        if manifest_root(manifest).parent in source_parents
        or (not local_entries and manifest_root(manifest).is_relative_to(marketplace_root(marketplace)))
    }
    deleted = [source for source in local_entries if source not in local_plugins]
    added = [source for source in local_plugins if source not in local_entries]
    renamed = {
        source: _native_plugin_name(local_plugins[source], root, staged=staged_names)
        for source, entry in local_entries.items()
        if source in local_plugins
        and entry["name"] != _native_plugin_name(local_plugins[source], root, staged=staged_names)
    }
    plugins = data.get("plugins", [])
    data["plugins"] = [
        entry
        for entry in plugins
        if not (
            (source := _marketplace_entry_source(entry)) is not None
            and _marketplace_local_source_path(source, marketplace, root) in deleted
        )
    ]
    for source in sorted(added):
        manifest = local_plugins[source]
        relative_source = source.relative_to(marketplace_root(marketplace))
        data["plugins"].append({
            "name": _native_plugin_name(manifest, root, staged=staged_names),
            "source": f"./{relative_source.as_posix()}",
        })
    for source, name in renamed.items():
        local_entries[source]["name"] = name
    changed = bool(deleted or added or renamed) or any(
        _source_differs_between_refs(root, source, marketplace.path, base_ref, head_ref) for source in local_plugins
    )
    if not changed and (
        not bump or marketplace.version_key_path is None or not _marketplace_differs_from_head(marketplace.path)
    ):
        return False
    if not bump or marketplace.version_key_path is None or manual_version:
        if changed and not dry_run:
            _write_json_lf(marketplace_path, _format_json(data))
        return changed
    if dry_run:
        return True
    bump_type: Literal["major", "minor", "patch"] = "major" if deleted else "minor" if added else "patch"
    _bump_native_marketplace_version(data, marketplace, bump_type)
    _write_json_lf(marketplace_path, _format_json(data))
    return True


def sync_native_marketplaces(
    root: Path = Path(),
    *,
    bump: bool = True,
    dry_run: bool = False,
    manifests: list[NativeManifest] | None = None,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
    preserve_unstaged: bool = False,
) -> list[Path]:
    """Reconcile every Git-visible native marketplace with local plugin manifests.

    Returns:
        Marketplace paths updated in this synchronization run.
    """
    if manifests is None:
        manifests = discover_manifests(root)
    updated: list[Path] = []
    for marketplace in (manifest for manifest in manifests if manifest.kind == "marketplace"):
        marketplace_path = root / marketplace.path
        original_content = (
            marketplace_path.read_bytes() if preserve_unstaged and _has_unstaged_change(marketplace.path) else None
        )
        if original_content is not None:
            staged_data = _read_staged_json(marketplace.path)
            if staged_data is None:
                continue
            _write_json_lf(marketplace_path, _format_json(staged_data))
        try:
            changed = _sync_native_marketplace(
                root,
                marketplace,
                manifests,
                bump=bump,
                dry_run=dry_run,
                base_ref=base_ref,
                head_ref=head_ref,
                staged_names=preserve_unstaged,
            )
            if changed:
                updated.append(marketplace.path)
        finally:
            if original_content is not None:
                if updated and not dry_run and updated[-1] == marketplace.path:
                    _stage_json(marketplace.path, json.loads(marketplace_path.read_text(encoding="utf-8")))
                marketplace_path.write_bytes(original_content)
            elif updated and not dry_run and updated[-1] == marketplace.path:
                _git_stage_file(marketplace.path.as_posix())
    return updated


def parse_plugin_path(filepath: str) -> PluginPathInfo | None:
    """Parse file path to extract plugin name and component type.

    Args:
        filepath: Relative file path from repository root

    Returns:
        {
            'plugin': 'plugin-name',
            'component_type': 'skill' | 'agent' | 'command' | 'hook' | 'mcp' | None,
            'component_path': 'skills/skill-name',
        }
    """
    parts = Path(filepath).parts

    if not parts or parts[0] != "plugins":
        return None

    if len(parts) < _MIN_PLUGIN_PATH_PARTS:
        return None

    plugin_name = parts[1]
    result: PluginPathInfo = {"plugin": plugin_name, "component_type": None, "component_path": None}

    # Check if this is a component file
    if len(parts) >= _MIN_COMPONENT_PATH_PARTS:
        component_dir = parts[2]

        match component_dir:
            case "skills" if len(parts) >= _MIN_SKILL_PATH_PARTS:
                # parts[3] is the top-level skill directory name
                skill_dir_name = parts[3]
                if skill_dir_name.startswith("."):
                    # Hidden directories (e.g. .claude/) are not skills
                    pass
                else:
                    result["component_type"] = "skill"
                    # Register the skill directory, not the full file path
                    result["component_path"] = f"skills/{skill_dir_name}"
            case "agents" if filepath.endswith(".md"):
                result["component_type"] = "agent"
                result["component_path"] = "/".join(parts[2:])
            case "commands" if filepath.endswith(".md"):
                result["component_type"] = "command"
                result["component_path"] = "/".join(parts[2:])
            case "hooks" if filepath.endswith(".json"):
                result["component_type"] = "hook"
                result["component_path"] = "/".join(parts[2:])
            case "mcp":
                result["component_type"] = "mcp"
                result["component_path"] = "/".join(parts[2:])

    return result


def bump_version(current: str, bump_type: Literal["major", "minor", "patch"]) -> str:
    """Bump semantic version.

    Args:
        current: Current version string (e.g., "1.2.3")
        bump_type: Type of version bump

    Returns:
        New version string after bumping
    """
    try:
        major, minor, patch = map(int, current.split("."))
    except (ValueError, AttributeError):
        sys.stderr.write(f"Warning: malformed version '{current}', defaulting to 0.1.0\n")
        return "0.1.0"

    match bump_type:
        case "major":
            return f"{major + 1}.0.0"
        case "minor":
            return f"{major}.{minor + 1}.0"
        case "patch":
            return f"{major}.{minor}.{patch + 1}"


def _parse_version_tuple(version_str: str) -> tuple[int, int, int] | None:
    """Parse a semantic version string into a comparable tuple.

    Args:
        version_str: Version string (e.g., "1.2.3")

    Returns:
        Tuple of (major, minor, patch) integers, or None if malformed.
    """
    try:
        major, minor, patch = map(int, version_str.split("."))
    except (ValueError, AttributeError):
        return None
    return (major, minor, patch)


def extract_version_from_json(data: object, key_path: list[str]) -> tuple[int, int, int] | None:
    """Traverse a JSON object by key path and parse the version string found.

    Args:
        data: Parsed JSON data (typically a nested dict).
        key_path: Keys to traverse to reach the version value.

    Returns:
        Version tuple ``(major, minor, patch)``, or None if the path is
        invalid, the value is not a string, or the version is malformed.
    """
    obj: object = data
    for key in key_path:
        if not _is_str_dict(obj):
            return None
        node = obj
        if key not in node:
            return None
        obj = node[key]
    if not isinstance(obj, str):
        return None
    return _parse_version_tuple(obj)


def _read_head_json(filepath: str | Path) -> object | None:
    """Read and parse JSON content of a file from HEAD.

    Args:
        filepath: Path to the JSON file (relative to repo root).

    Returns:
        Parsed JSON data, or None if the file does not exist in HEAD
        or cannot be parsed.
    """
    if _GIT_PATH is None:
        return None

    result = subprocess.run([_GIT_PATH, "show", f"HEAD:{filepath}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None

    try:
        parsed: object = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed


def resolve_base() -> str | None:
    """Resolve the best available base ref for version comparison.

    Tries refs in order: origin/main → main.
    Returns None when no ref is resolvable (fresh or shallow clone).

    In the CI ``manifest-sync`` job (``fetch-depth: 1``), ``origin/main`` is
    absent.  Returning ``None`` causes callers to fall back to HEAD-based
    behaviour — identical to the pre-refactor logic — so shallow clones are
    fully supported without any CI-side changes.

    Returns:
        The first resolvable ref string, or None if none resolve.
    """
    if _GIT_PATH is None:
        return None

    candidates = ["origin/main", "main"]

    for ref in candidates:
        result = subprocess.run(
            [_GIT_PATH, "rev-parse", "--verify", "--quiet", ref], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            return ref

    return None


def read_ref_json(ref: str, filepath: str | Path) -> object | None:
    """Read and parse JSON content of a file at an arbitrary git ref.

    When ``ref`` is ``"HEAD"``, delegates to ``_read_head_json`` so that
    existing monkeypatches of ``_read_head_json`` in tests remain effective.

    Args:
        ref: Git ref (branch, tag, or ``HEAD``) to read the file from.
        filepath: Path to the JSON file relative to the repo root.

    Returns:
        Parsed JSON data, or None if the file does not exist at the ref
        or cannot be parsed.
    """
    if ref == "HEAD":
        return _read_head_json(filepath)

    if _GIT_PATH is None:
        return None

    # Normalise to forward slashes for git show (handles Windows paths).
    rel_path = Path(filepath).as_posix()
    result = subprocess.run([_GIT_PATH, "show", f"{ref}:{rel_path}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None

    try:
        parsed: object = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed


def _is_ahead_of_ref(filepath: str | Path, version_key_path: list[str], ref: str) -> bool:
    """Check if the working copy version is strictly greater than a given ref.

    Args:
        filepath: Path to the JSON file (relative to repo root).
        version_key_path: Keys to traverse to reach the version value.
            For ``plugin.json``: ``["version"]``
            For ``marketplace.json``: ``["metadata", "version"]``
        ref: Git ref to compare the working copy version against.

    Returns:
        True if the current file version is strictly greater than the
        version at ``ref``.  Returns False if versions are equal, the ref
        lacks the file, or any parsing error occurs.
    """
    ref_data = read_ref_json(ref, filepath)
    if ref_data is None:
        return False

    ref_version = extract_version_from_json(ref_data, version_key_path)
    if ref_version is None:
        return False

    current_path = Path(filepath)
    if not current_path.exists():
        return False

    try:
        current_data = json.loads(current_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return False

    current_version = extract_version_from_json(current_data, version_key_path)
    if current_version is None:
        return False

    return current_version > ref_version


def _version_already_bumped(filepath: str | Path, version_key_path: list[str]) -> bool:
    """Check if the working copy version is already greater than HEAD.

    Backwards-compatible alias for ``_is_ahead_of_ref(..., "HEAD")``.

    Args:
        filepath: Path to the JSON file (relative to repo root).
        version_key_path: Keys to traverse to reach the version value.
            For ``plugin.json``: ``["version"]``
            For ``marketplace.json``: ``["metadata", "version"]``

    Returns:
        True if the current file version is strictly greater than HEAD,
        meaning a bump already happened.  Returns False if versions are
        equal, HEAD lacks the file, or any parsing error occurs.
    """
    return _is_ahead_of_ref(filepath, version_key_path, "HEAD")


def _write_json_lf(path: Path, content: str) -> None:
    """Write string to path with LF line endings (cross-platform).

    Uses write_bytes to avoid Windows CRLF conversion in text mode.
    """
    path.write_bytes(content.encode("utf-8"))


def _format_json(data: object) -> str:
    """Serialize data to JSON with 2-space indentation and trailing newline.

    Args:
        data: JSON-serialisable Python object.

    Returns:
        A JSON string with trailing newline.
    """
    return json.dumps(data, indent=2) + "\n"


def _is_standard_path_component(field_name: str, comp_path: str) -> bool:
    """Return True when the component sits in the auto-discovered default location.

    Claude Code auto-discovers components in these default directories at the
    plugin root:

    - ``skills/`` — every subdirectory containing ``SKILL.md``
    - ``agents/`` — every ``*.md`` file
    - ``commands/`` — every ``*.md`` file

    When the corresponding ``skills`` / ``agents`` / ``commands`` key in
    ``plugin.json`` is ABSENT, auto-discovery registers everything in the
    default directory.  Writing that key — even to add a single entry —
    overrides auto-discovery: the declared list becomes the *complete* list
    and every file not named in it becomes invisible.

    **2026-03-17 incident**: ``python3-development`` committed

    .. code-block:: json

        "agents": ["./agents/t0-baseline-capture.md", "./agents/tn-verification-gate.md"]

    17 of 19 agents disappeared.  **2026-04-12 recurrence**: this same script
    auto-added two new ``development-harness`` agents to a fresh ``agents``
    array in commit 30260566, silently masking 21 of 23 agents.  The fix is
    to teach this function that ``agents`` and ``commands`` obey the same
    auto-discovery semantics as ``skills``.

    File paths (ending in ``/SKILL.md`` or a specific ``.md`` file under
    a non-standard directory) are NOT auto-discovered — those are explicit
    file references that the user deliberately placed outside the default.
    They must still be registered.

    Args:
        field_name: The plugin.json array field being updated
            (``skills``, ``agents``, ``commands``).
        comp_path: Component path relative to the plugin root (without ``./``).
            Production pre-commit detection emits directory form for skills
            (e.g. ``skills/my-skill``) and file form for agents/commands
            (e.g. ``agents/my-agent.md``, ``commands/my-command.md``).

    Returns:
        True if the component lives in its default auto-discovered location
        and therefore MUST NOT be added to ``plugin.json``.

    See Also:
        ``.claude/rules/plugin-development.md`` — canonical rule documenting
        auto-discovery and the 2026-03-17 incident.
    """
    if field_name == "skills":
        # Skills auto-discovered at skills/<name>/SKILL.md. Pre-commit detection
        # emits the directory form (skills/my-skill). Explicit file references
        # (skills/my-skill/SKILL.md) are not subject to this rule.
        return comp_path.startswith("skills/") and not comp_path.endswith("/SKILL.md")
    if field_name == "agents":
        # Agents auto-discovered at agents/*.md. Any path directly under
        # agents/ with no further subdirectory is standard-path.
        return comp_path.startswith("agents/") and comp_path.count("/") == 1 and comp_path.endswith(".md")
    if field_name == "commands":
        # Commands auto-discovered at commands/*.md.
        return comp_path.startswith("commands/") and comp_path.count("/") == 1 and comp_path.endswith(".md")
    return False


# Backwards-compat alias — several tests still import the old name.
_is_standard_path_skill = _is_standard_path_component


def _remove_component_from_array(data: dict[str, list[str] | str], field_name: str, comp_path: str) -> bool:
    """Remove a component path from a plugin.json array field.

    Args:
        data: Plugin.json data dictionary (mutated in place).
        field_name: Array field name (``skills``, ``agents``, ``commands``).
        comp_path: Component path relative to plugin root (without ``./``).

    Returns:
        True if an entry was removed.
    """
    if field_name not in data:
        return False
    field_value = data[field_name]
    if not isinstance(field_value, list):
        return False
    relative_path = f"./{comp_path}"
    if relative_path not in field_value:
        return False
    field_value.remove(relative_path)
    return True


def _update_component_arrays(data: dict[str, list[str] | str], changes: ComponentChanges) -> bool:
    """Update component arrays in plugin.json.

    Args:
        data: Plugin.json data dictionary
        changes: Component changes to apply

    Returns:
        True if modifications were made
    """
    modified = False

    for component in changes["added"]:
        comp_type = component["component_type"]
        comp_path = component["component_path"]

        if comp_type not in {"skill", "agent", "command"}:
            continue

        field_name = f"{comp_type}s"

        # Standard-path components (skills/, agents/, commands/) are
        # auto-discovered by Claude Code. Registering them in plugin.json is
        # ACTIVELY HARMFUL: any write to the field — even to add one entry —
        # overrides auto-discovery and makes every unlisted component invisible.
        # See _is_standard_path_component for the 2026-03-17 / 2026-04-12
        # incident history.
        #
        # Record the addition so the caller emits a minor version bump, but do
        # NOT touch the plugin.json array. Only when the field ALREADY exists
        # (Mode B — manual allowlist) do we append the new entry, because in
        # that mode auto-discovery is already disabled and we must keep the
        # explicit list complete.
        if _is_standard_path_component(field_name, comp_path):
            modified = True
            if field_name in data:
                # Mode B: array already declared by the user — carry the new
                # entry forward so it stays visible under manual allowlist mode.
                relative_path = f"./{comp_path}"
                field_value = data[field_name]
                if isinstance(field_value, list) and relative_path not in field_value:
                    field_value.append(relative_path)
            continue

        # Non-standard path (e.g. custom subdirectory) — explicit registration
        # is mandatory because auto-discovery does not see these paths.
        relative_path = f"./{comp_path}"

        if field_name not in data:
            data[field_name] = []

        field_value = data[field_name]
        if isinstance(field_value, list):
            if relative_path not in field_value:
                field_value.append(relative_path)
                modified = True
        elif isinstance(field_value, str):
            data[field_name] = [field_value, relative_path]
            modified = True

    for component in changes["deleted"]:
        comp_type = component["component_type"]
        comp_path = component["component_path"]

        if comp_type in {"skill", "agent", "command"}:
            field_name = f"{comp_type}s"
            modified |= _remove_component_from_array(data, field_name, comp_path)

    return modified


def _determine_bump_type(changes: ComponentChanges) -> Literal["major", "minor", "patch"]:
    """Derive the semver bump type from component changes.

    Args:
        changes: Component changes with added, deleted, and modified lists.

    Returns:
        ``"major"`` when components were deleted (breaking change),
        ``"minor"`` when components were added (new feature),
        ``"patch"`` for pure modifications.
    """
    if any(component["component_type"] != "other" for component in changes["deleted"]):
        return "major"
    if any(component["component_type"] != "other" for component in changes["added"]):
        return "minor"
    return "patch"


def _write_plugin_version(
    plugin_json_path: Path,
    data: dict[str, list[str] | str],
    from_version: str,
    bump_type: Literal["major", "minor", "patch"],
    current_version: str,
) -> tuple[bool, str]:
    """Bump the version in *data*, write the file, and return the outcome.

    Skips the write when the computed new content already matches the file on
    disk — acting as a secondary idempotency guard after the version-comparison
    guard in callers.

    Args:
        plugin_json_path: Absolute or cwd-relative path to the plugin.json file.
        data: Parsed plugin.json dict to mutate and write.
        from_version: The version string to apply the bump step to.
        bump_type: Semver bump category.
        current_version: The working-copy version (returned unchanged when the
            write is skipped).

    Returns:
        ``(True, new_version)`` when the file was written,
        ``(False, current_version)`` when the write was skipped.
    """
    new_version = bump_version(from_version, bump_type)
    existing_content = plugin_json_path.read_text(encoding="utf-8")
    data["version"] = new_version
    new_content = _format_json(data)
    if new_content == existing_content:
        return False, current_version
    _write_json_lf(plugin_json_path, new_content)
    return True, new_version


def _extract_str_version(json_data: object, key: str) -> str | None:
    """Extract a top-level string version field from a parsed JSON object.

    Args:
        json_data: Parsed JSON — expected to be a dict.
        key: Top-level key holding the version string (e.g. ``"version"``).

    Returns:
        The version string, or None if absent or not a string.
    """
    if not _is_str_dict(json_data):
        return None
    raw = json_data.get(key)
    return raw if isinstance(raw, str) else None


def _update_from_base_ref(
    plugin_json_path: Path,
    data: dict[str, list[str] | str],
    current_version: str,
    base_ref: str,
    changes: ComponentChanges,
    *,
    sync_components: bool,
) -> tuple[bool, str] | None:
    """Attempt to update plugin.json using a resolved base ref.

    Compares the working-copy version against the version at *base_ref*.  If
    the working copy is already strictly ahead, the bump is skipped.
    Otherwise, the bump is derived from *base_ref* so concurrent branches
    never land on the same version number.

    Args:
        plugin_json_path: Path to the plugin.json file.
        data: Parsed plugin.json dict — mutated in place on write.
        current_version: Version string read from the working copy.
        base_ref: Resolved git ref (e.g. ``"origin/main"`` or ``"main"``).
        changes: Component changes for this plugin.
        sync_components: Whether to update Claude component arrays in the manifest.

    Returns:
        ``(updated, version)`` when the base ref path was authoritative,
        or ``None`` when the plugin is absent at *base_ref* (caller should
        fall back to HEAD-based logic).
    """
    base_data = read_ref_json(base_ref, str(plugin_json_path))
    base_ver = _extract_str_version(base_data, "version") if base_data is not None else None
    if base_ver is None:
        return None

    modified = _update_component_arrays(data, changes) if sync_components else False
    ahead = _is_ahead_of_ref(plugin_json_path, ["version"], base_ref)
    if ahead:
        if modified:
            _write_json_lf(plugin_json_path, _format_json(data))
            return True, current_version
        return False, current_version
    if modified or any(changes.values()):
        return _write_plugin_version(plugin_json_path, data, base_ver, _determine_bump_type(changes), current_version)
    return False, current_version


def _update_from_head(
    plugin_json_path: Path,
    data: dict[str, list[str] | str],
    current_version: str,
    changes: ComponentChanges,
    *,
    sync_components: bool,
) -> tuple[bool, str]:
    """Update plugin.json using HEAD as the comparison base (fallback path).

    Used when no base ref is resolvable (shallow CI clones, fresh checkouts)
    or when the plugin does not yet exist on the base branch.  Behaviour is
    identical to the pre-refactor implementation.

    Args:
        plugin_json_path: Path to the plugin.json file.
        data: Parsed plugin.json dict — mutated in place on write.
        current_version: Version string read from the working copy.
        changes: Component changes for this plugin.
        sync_components: Whether to update Claude component arrays in the manifest.

    Returns:
        ``(updated, version)``
    """
    modified = _update_component_arrays(data, changes) if sync_components else False
    if _version_already_bumped(str(plugin_json_path), ["version"]):
        if modified:
            _write_json_lf(plugin_json_path, _format_json(data))
            return True, current_version
        return False, current_version
    if modified or any(changes.values()):
        return _write_plugin_version(
            plugin_json_path, data, current_version, _determine_bump_type(changes), current_version
        )
    return False, current_version


def _plugin_manifest_paths(plugin_name: str) -> list[tuple[Path, bool]]:
    plugin_root = Path("plugins") / plugin_name
    return [
        (path, path.parent.name == ".claude-plugin")
        for path in sorted(plugin_root.glob(".*-plugin/plugin.json"))
        if path.is_file()
    ]


def _update_plugin_manifest(
    plugin_json_path: Path, changes: ComponentChanges, *, sync_components: bool, compare_to_head: bool = False
) -> tuple[bool, str]:

    with plugin_json_path.open(encoding="utf-8") as f:
        data: dict[str, list[str] | str] = json.load(f)

    raw_ver = data.get("version", "0.0.0")
    current_version = raw_ver if isinstance(raw_ver, str) else "0.0.0"

    base_ref = None if compare_to_head else resolve_base()
    if base_ref is not None:
        result = _update_from_base_ref(
            plugin_json_path, data, current_version, base_ref, changes, sync_components=sync_components
        )
        if result is not None:
            return result

    return _update_from_head(plugin_json_path, data, current_version, changes, sync_components=sync_components)


def update_plugin_json(plugin_name: str, changes: ComponentChanges) -> tuple[bool, str]:
    """Update plugin.json based on component changes.

    Resolves a base ref (origin/main → main) and bumps from the base version
    when available, so that concurrent PR branches never collide on the same
    version number.  Falls back to HEAD-based comparison when no base ref is
    resolvable (shallow CI clones, fresh checkouts).

    Args:
        plugin_name: Name of the plugin directory under ``plugins/``.
        changes: Component changes with ``added``, ``deleted``, and
            ``modified`` lists.

    Returns:
        ``(updated, version)`` — updated is True when the file was written;
        version is the new version on update or the unchanged version otherwise.
    """
    updated = False
    version = "0.0.0"
    version_set = False
    manifest_versions: dict[str, str] = {}
    for manifest_path, sync_components in _plugin_manifest_paths(plugin_name):
        manifest_updated, manifest_version = _update_plugin_manifest(
            manifest_path, changes, sync_components=sync_components
        )
        updated |= manifest_updated
        manifest_versions[str(manifest_path)] = manifest_version
        if sync_components or not version_set:
            version = manifest_version
            version_set = True

    # Each manifest variant (.claude-plugin, .codex-plugin, ...) bumps from its
    # own base version independently by design (see
    # test_update_plugin_json_bumps_all_harness_manifests) -- they are not
    # forced into numeric agreement. Surface divergence instead of leaving it
    # silent, so a widening gap is visible in hook/CI output.
    if updated and len({*manifest_versions.values()}) > 1:
        drift = ", ".join(f"{path}={ver}" for path, ver in sorted(manifest_versions.items()))
        print(f"Info: {plugin_name} manifest versions diverge across harnesses ({drift})")

    return updated, version


def _read_plugin_name(plugin_dir_name: str) -> str:
    """Read the canonical plugin name from plugin.json.

    The ``"name"`` field in plugin.json is authoritative.  Falls back to the
    directory name when plugin.json is absent or contains no ``"name"`` field.

    Args:
        plugin_dir_name: Directory name under ``plugins/`` (e.g. ``"the-rewrite-room"``).

    Returns:
        The plugin name as declared in plugin.json, or the directory name as fallback.
    """
    plugin_json_path = Path(f"plugins/{plugin_dir_name}/.claude-plugin/plugin.json")
    if plugin_json_path.exists():
        try:
            with plugin_json_path.open(encoding="utf-8") as f:
                data = json.load(f)
            name = data.get("name")
            if name and isinstance(name, str):
                return name
        except (OSError, json.JSONDecodeError):
            pass
    return plugin_dir_name


def _update_marketplace_plugins(data: _MarketplaceJsonData, plugin_changes: MarketplaceChanges) -> bool:
    """Add and remove plugins in the marketplace data structure.

    The ``"name"`` field in each marketplace entry is derived from plugin.json
    (authoritative), not from the directory name.

    Args:
        data: Marketplace JSON data dictionary (mutated in place).
        plugin_changes: Changes describing added/deleted plugins.

    Returns:
        True if the plugins list was modified.
    """
    modified = False

    for plugin_dir_name in plugin_changes["added"]:
        canonical_name = _read_plugin_name(plugin_dir_name)
        plugin_entry = _MarketplacePluginEntry(name=canonical_name, source=f"./plugins/{plugin_dir_name}")

        if "plugins" not in data:
            data["plugins"] = []

        plugins_list = data["plugins"]

        if not any(p["name"] == canonical_name for p in plugins_list):
            plugins_list.append(plugin_entry)
            modified = True

    for plugin_dir_name in plugin_changes["deleted"]:
        if "plugins" in data:
            canonical_name = _read_plugin_name(plugin_dir_name)
            plugins_list = data["plugins"]
            data["plugins"] = [p for p in plugins_list if p["name"] != canonical_name]
            modified = True

    return modified


def update_marketplace_json(plugin_changes: MarketplaceChanges) -> bool:
    """Update marketplace.json based on plugin changes.

    Args:
        plugin_changes: Added, deleted, and modified plugin names grouped by
            change type.

    Returns:
        updated: bool
    """
    marketplace_json_path = Path(".claude-plugin/marketplace.json")

    if not marketplace_json_path.exists():
        print("Warning: marketplace.json not found")
        return False

    with marketplace_json_path.open(encoding="utf-8") as f:
        data: _MarketplaceJsonData = json.load(f)

    metadata: _MarketplaceMetadata = data.get("metadata", {})
    current_version = metadata.get("version", "0.0.0")

    # Skip if the version was already bumped (e.g., user manually edited
    # marketplace.json in the same commit, or commit retry after hook failure).
    if _version_already_bumped(str(marketplace_json_path), ["metadata", "version"]):
        return False
    bump_type: Literal["major", "minor", "patch"] = "patch"

    # Determine bump type
    if plugin_changes["deleted"]:
        bump_type = "major"  # Breaking change
    elif plugin_changes["added"]:
        bump_type = "minor"  # New plugins

    # Add and remove plugins
    modified = _update_marketplace_plugins(data, plugin_changes)

    # Bump marketplace version if any changes
    if modified or plugin_changes["modified"]:
        existing_content = marketplace_json_path.read_text(encoding="utf-8")
        new_version = bump_version(current_version, bump_type)

        if "metadata" not in data:
            data["metadata"] = {}

        metadata = data["metadata"]
        metadata["version"] = new_version

        new_content = _format_json(data)

        if new_content == existing_content:
            return False

        _write_json_lf(marketplace_json_path, new_content)

        return True

    return False


def _process_file_changes(status: dict[str, list[str]]) -> tuple[dict[str, ComponentChanges], MarketplaceChanges]:
    """Process changed files and categorize them.

    Args:
        status: Git status dictionary

    Returns:
        (plugin_component_changes, marketplace_changes)
    """
    plugin_component_changes: dict[str, ComponentChanges] = defaultdict(
        lambda: {"added": [], "deleted": [], "modified": []}
    )

    marketplace_changes: MarketplaceChanges = {"added": set(), "deleted": set(), "modified": []}

    # Parse all changed files
    for operation in ["added", "deleted", "modified"]:
        for filepath in status[operation]:
            parsed = parse_plugin_path(filepath)

            if not parsed:
                continue

            plugin_name = parsed["plugin"]

            # Check if this is a new/deleted plugin
            if filepath.endswith(".claude-plugin/plugin.json"):
                if operation == "added":
                    marketplace_changes["added"].add(plugin_name)
                elif operation == "deleted":
                    marketplace_changes["deleted"].add(plugin_name)
                else:
                    # plugin.json was modified (not created/deleted) — treat as
                    # an "other" change so it triggers a patch version bump.
                    plugin_component_changes[plugin_name]["modified"].append({
                        "component_type": "other",
                        "component_path": "/".join(Path(filepath).parts[2:]),
                    })
                continue

            # Track component changes
            if parsed["component_type"] and parsed["component_path"]:
                component_change: ComponentChange = {
                    "component_type": parsed["component_type"],
                    "component_path": parsed["component_path"],
                }

                match operation:
                    case "added":
                        plugin_component_changes[plugin_name]["added"].append(component_change)
                    case "deleted":
                        plugin_component_changes[plugin_name]["deleted"].append(component_change)
                    case "modified":
                        plugin_component_changes[plugin_name]["modified"].append(component_change)
            # Non-component file changed inside plugin dir — still
            # triggers a patch version bump.
            else:
                plugin_component_changes[plugin_name]["modified"].append({
                    "component_type": "other",
                    "component_path": "/".join(Path(filepath).parts[2:]),
                })

    return plugin_component_changes, marketplace_changes


def _git_stage_file(filepath: str) -> None:
    """Stage a file with git add, logging warnings on failure.

    Args:
        filepath: Relative path to stage.
    """
    if not _GIT_PATH:
        return
    result = subprocess.run([_GIT_PATH, "add", filepath], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(f"Warning: git add {filepath} failed: {result.stderr.strip()}\n")


def _staged_paths() -> set[Path]:
    return {
        Path(path.decode("utf-8", errors="surrogateescape"))
        for path in _run_git_bytes(["ls-files", "--cached", "-z"]).split(b"\0")
        if path
    }


def _has_unstaged_change(path: Path) -> bool:
    if _GIT_PATH is None:
        return False
    return subprocess.run([_GIT_PATH, "diff", "--quiet", "--", path.as_posix()], check=False).returncode == 1


def _read_staged_json(path: Path) -> dict[str, list[str] | str] | None:
    if _GIT_PATH is None:
        return None
    result = subprocess.run([_GIT_PATH, "show", f":{path.as_posix()}"], capture_output=True, check=False)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _stage_json(path: Path, data: dict[str, list[str] | str]) -> None:
    if _GIT_PATH is None:
        return
    content = _format_json(data).encode()
    blob = subprocess.run([_GIT_PATH, "hash-object", "-w", "--stdin"], input=content, capture_output=True, check=True)
    subprocess.run(
        [_GIT_PATH, "update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.decode().strip()},{path.as_posix()}"],
        check=True,
    )


def _discover_skills(plugin_dir: Path) -> list[str]:
    """Discover skill components on disk for a plugin.

    Finds SKILL.md files and script files within skill directories.

    Args:
        plugin_dir: Root directory of the plugin (e.g., plugins/my-plugin/)

    Returns:
        List of relative paths (e.g., ``./skills/foo``, ``./skills/bar/SKILL.md``)
    """
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []

    found: list[str] = []

    for item in sorted(skills_dir.iterdir()):
        if item.name.startswith("."):
            continue

        if item.is_dir():
            skill_md = item / "SKILL.md"
            if skill_md.is_file() and is_git_visible(Path(), skill_md):
                # Skill directory with SKILL.md — skills are always flat: skills/{name}/
                found.append(f"./skills/{item.name}")

        elif item.suffix == ".md" and item.name == "SKILL.md":
            # Bare SKILL.md directly in skills/ (unusual but valid)
            found.append("./skills/SKILL.md")

    return found


def _discover_agents(plugin_dir: Path) -> list[str]:
    """Discover agent files on disk for a plugin.

    Args:
        plugin_dir: Root directory of the plugin

    Returns:
        List of relative paths (e.g., ``./agents/my-agent.md``)
    """
    agents_dir = plugin_dir / "agents"
    if not agents_dir.is_dir():
        return []

    return [
        f"./agents/{f.name}"
        for f in sorted(agents_dir.iterdir())
        if f.is_file() and f.suffix == ".md" and not f.name.startswith(".") and is_git_visible(Path(), f)
    ]


def _discover_commands(plugin_dir: Path) -> list[str]:
    """Discover command files on disk for a plugin.

    Args:
        plugin_dir: Root directory of the plugin

    Returns:
        List of relative paths (e.g., ``./commands/my-command.md``)
    """
    commands_dir = plugin_dir / "commands"
    if not commands_dir.is_dir():
        return []

    return [
        f"./commands/{f.name}"
        for f in sorted(commands_dir.iterdir())
        if f.is_file() and f.suffix == ".md" and not f.name.startswith(".") and is_git_visible(Path(), f)
    ]


def _is_skill_user_invocable(skill_md_path: Path) -> bool:
    """Check if a SKILL.md file has ``user-invocable: true`` in its frontmatter.

    Parses the YAML frontmatter (between ``---`` delimiters) and looks for
    the ``user-invocable`` field.  Skills default to user-invocable when the
    field is absent, matching Claude Code's behavior.

    Args:
        skill_md_path: Absolute path to a SKILL.md file

    Returns:
        True if the skill is user-invocable (explicit ``true`` or field absent)
    """
    if not skill_md_path.is_file():
        return False

    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return False

    # Extract frontmatter between first two --- lines
    if not text.startswith("---"):
        return True  # No frontmatter = default (invocable)

    end = text.find("\n---", 3)
    if end == -1:
        return True

    frontmatter = text[3:end]

    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped.startswith("user-invocable:"):
            value = stripped.split(":", 1)[1].strip().lower()
            return value in {"true", "yes"}

    # Field absent = default invocable
    return True


def _discover_invocable_skills(plugin_dir: Path) -> list[str]:
    """Discover skills with ``user-invocable: true`` for the commands array.

    User-invocable skills appear as ``/skill-name`` shortcuts in Claude Code.
    Registration in the ``skills`` array alone is sufficient -- Claude Code
    deduplicates entries that appear in both ``skills`` and ``commands``.

    This function exists as a compatibility measure from Claude Code v2.19
    when skills did not reliably appear as commands without explicit
    ``commands`` array registration. As of 2026-02-13 testing, the
    duplication is harmless but unnecessary.

    Args:
        plugin_dir: Root directory of the plugin

    Returns:
        List of relative skill paths (e.g., ``./skills/my-skill``)
    """
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []

    found: list[str] = []

    for item in sorted(skills_dir.iterdir()):
        if item.name.startswith(".") or not item.is_dir() or not is_git_visible(Path(), item):
            continue

        skill_md = item / "SKILL.md"
        if skill_md.is_file() and is_git_visible(Path(), skill_md) and _is_skill_user_invocable(skill_md):
            found.append(f"./skills/{item.name}")

        # Check nested skill directories (e.g., skills/testing/*)
        for nested in sorted(item.iterdir()):
            if nested.is_dir() and not nested.name.startswith(".") and is_git_visible(Path(), nested):
                nested_skill_md = nested / "SKILL.md"
                if (
                    nested_skill_md.is_file()
                    and is_git_visible(Path(), nested_skill_md)
                    and _is_skill_user_invocable(nested_skill_md)
                ):
                    found.append(f"./skills/{item.name}/{nested.name}")

    return found


def _normalize_skill_ref(ref: str) -> str:
    """Normalize a skill reference for comparison.

    Both ``./skills/foo`` and ``./skills/foo/SKILL.md`` refer to the same skill.
    The ``./`` prefix is optional in some hand-edited plugin.json files
    (e.g. ``skills/foo``), so it is stripped here as well. This normalizes
    to the bare directory form.

    Args:
        ref: Skill reference path from plugin.json

    Returns:
        Normalized path for comparison
    """
    normalized = ref.removeprefix("./")
    return normalized.removesuffix("/SKILL.md")


def _strip_ref_prefix(ref: str) -> str:
    """Strip the leading ``./`` from a component reference for path predicates.

    Args:
        ref: Component reference path as it appears in plugin.json.

    Returns:
        The same path with any leading ``./`` removed, matching the bare
        form used by ``_is_standard_path_component``.
    """
    return ref.removeprefix("./")


def _reconcile_mode_b(
    data: dict[str, list[str] | str], field_name: str, disk_items: list[str], plugin_name: str, *, dry_run: bool
) -> bool:
    """Reconcile a component array that is already present in plugin.json.

    Mode B invariant: when the key is present, the declared list overrides
    auto-discovery and becomes the *complete* set of registered components.
    Any default-path component on disk that is not in the list becomes
    invisible — the 2026-03-17 / 2026-04-12 masking pattern.

    This function keeps the invariant true by:

    1. Adding every default-path item discovered on disk that is not already
       in the registered list. New files added to disk after the key was
       created are no longer silently dropped.
    2. Removing stale default-path entries that refer to files no longer
       present on disk.
    3. Preserving non-default-path entries (explicit references to files
       outside ``agents/`` / ``commands/`` / ``skills/`` one-level roots)
       untouched. Those entries are legitimate because Claude Code's
       auto-discovery would not pick them up; the user deliberately placed
       them outside the default location.

    A cleaner fix for many plugins is to remove the key entirely so
    auto-discovery handles everything. This function does not perform that
    rewrite — it only maintains the all-or-nothing invariant for plugins
    that keep the key.

    Args:
        data: Plugin.json data dictionary (mutated in place unless dry_run)
        field_name: Array field name (``skills``, ``agents``, ``commands``)
        disk_items: Default-path items discovered on disk (authority set for
            the default directory only — non-default paths are not included)
        plugin_name: Plugin name for logging
        dry_run: If True, only report

    Returns:
        True if drift was detected (entries added or removed)
    """
    raw = data.get(field_name, [])
    registered = list(raw) if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    if field_name not in data:
        return False

    normalize = field_name == "skills"

    # Add: default-path disk items that are not yet registered.
    missing = _find_missing_items(disk_items, registered, normalize=normalize)

    # Remove stale: only prune default-path entries whose on-disk file is
    # gone. Non-default-path entries are preserved regardless — they are
    # explicit declarations that auto-discovery cannot satisfy.
    stale: list[str] = []
    for reg in registered:
        if not _is_standard_path_component(field_name, _strip_ref_prefix(reg)):
            continue  # Non-default path — preserve untouched
        if not any(_refs_match(reg, item, normalize=normalize) for item in disk_items):
            stale.append(reg)

    if not missing and not stale:
        return False

    _apply_drift_changes(data, field_name, missing, stale, plugin_name, dry_run=dry_run)
    return True


def _reconcile_one_plugin(plugin_name: str, plugins_root: Path, *, dry_run: bool) -> bool:
    """Reconcile a single plugin's plugin.json against its directory contents.

    Two modes based on whether ``plugin.json`` has an explicit ``skills`` field:

    **Mode A — Auto-discovery (no ``skills`` field)**
        The ``skills/`` directory is auto-discovered by Claude Code.  Skills
        reconciliation is skipped entirely — absent registration is correct, not
        drift.  The same applies to the ``commands`` field for standard-path
        invocable skills.

    **Mode B — Explicit field present**
        The declared list overrides auto-discovery — the array becomes the
        *complete* set Claude Code sees.  Reconciliation keeps that invariant
        true by adding every default-path component found on disk that is
        not already in the array, removing stale default-path entries whose
        files no longer exist, and preserving non-default-path entries
        untouched.  The preferred long-term fix is to remove the key so
        auto-discovery handles everything, but that rewrite is out of scope
        for reconcile mode.

    The same Mode A / Mode B logic applies to the ``agents`` and ``commands``
    fields.

    Args:
        plugin_name: Name of the plugin
        plugins_root: Path to the plugins/ directory
        dry_run: If True, only report; do not modify files

    Returns:
        True if changes were made (or would be made in dry_run)
    """
    plugin_dir = plugins_root / plugin_name
    plugin_json_path = plugin_dir / ".claude-plugin" / "plugin.json"

    if not plugin_json_path.exists():
        return False

    with plugin_json_path.open(encoding="utf-8") as f:
        data: dict[str, list[str] | str] = json.load(f)

    disk_skills = _discover_skills(plugin_dir)
    disk_agents = _discover_agents(plugin_dir)
    disk_commands = _discover_commands(plugin_dir)
    invocable_skills = _discover_invocable_skills(plugin_dir)

    # All invocable skills (both standard-path and non-standard) discovered on
    # disk, combined with commands/ files, form the full commands authority set.
    disk_commands_full = disk_commands + invocable_skills

    has_drift = False

    # All three component types (skills, agents, commands) share the same
    # auto-discovery semantics: if the field is ABSENT from plugin.json,
    # Claude Code auto-discovers every file in the default directory. If the
    # field is PRESENT, its list becomes the complete set and every file not
    # listed becomes invisible.
    #
    # Reconciliation therefore runs in one of two modes per field:
    #
    #   Mode A (field absent): auto-discovery handles everything — do NOT
    #       write the field. Writing an empty or partial list would silently
    #       mask every unlisted file.
    #
    #   Mode B (field present): the declared list overrides auto-discovery,
    #       so it must remain complete. Add every default-path item on disk
    #       that is not already registered, remove stale default-path entries
    #       whose files are gone, and preserve non-default-path entries
    #       untouched. A partial list would silently mask unlisted files.
    #
    # A previous revision of this script violated this rule for agents:
    # "agents always require explicit registration" was a false assumption
    # carried over from an older Claude Code version. On 2026-04-12 commit
    # 30260566 the agents branch auto-created an array containing only the
    # two newly-added agent files, silently masking 21 of 23 existing
    # development-harness agents. See the docstring of
    # _is_standard_path_component for the full incident history.

    # --- Skills reconciliation ---
    if "skills" not in data:
        pass  # Mode A
    else:
        has_drift |= _reconcile_mode_b(data, "skills", disk_skills, plugin_name, dry_run=dry_run)

    # --- Agents reconciliation ---
    if "agents" not in data:
        pass  # Mode A — auto-discovery handles agents/*.md
    else:
        has_drift |= _reconcile_mode_b(data, "agents", disk_agents, plugin_name, dry_run=dry_run)

    # --- Commands reconciliation ---
    if "commands" not in data:
        pass  # Mode A
    else:
        has_drift |= _reconcile_mode_b(data, "commands", disk_commands_full, plugin_name, dry_run=dry_run)

    if has_drift and not dry_run:
        raw_ver = data.get("version", "0.0.0")
        current_version = raw_ver if isinstance(raw_ver, str) else "0.0.0"
        data["version"] = bump_version(current_version, "minor")
        _write_json_lf(plugin_json_path, _format_json(data))
        print(f"  Updated {plugin_name} -> {data['version']}")

    return has_drift


def _refs_match(ref_a: str, ref_b: str, *, normalize: bool) -> bool:
    """Check if two component references are equivalent.

    Args:
        ref_a: First reference path
        ref_b: Second reference path
        normalize: If True, normalize skill paths before comparison

    Returns:
        True if references point to the same component
    """
    if normalize:
        return _normalize_skill_ref(ref_a) == _normalize_skill_ref(ref_b)
    return ref_a == ref_b


def _find_missing_items(disk_items: list[str], registered: list[str], *, normalize: bool) -> list[str]:
    """Find items on disk that are not registered in the manifest.

    Args:
        disk_items: Paths discovered on disk
        registered: Paths currently in the manifest
        normalize: If True, normalize skill paths before comparison

    Returns:
        List of disk items with no matching registered entry
    """
    return [item for item in disk_items if not any(_refs_match(reg, item, normalize=normalize) for reg in registered)]


def _find_stale_items(registered: list[str], disk_items: list[str], *, normalize: bool) -> list[str]:
    """Find registered items not present in the discovery list.

    Discovery functions are the sole authority on what belongs in each
    component array.  Any registered entry not matched by discovery is stale.

    Args:
        registered: Paths currently in the manifest
        disk_items: Paths discovered on disk
        normalize: If True, normalize skill paths before comparison

    Returns:
        List of registered items with no matching discovered entry
    """
    return [reg for reg in registered if not any(_refs_match(reg, item, normalize=normalize) for item in disk_items)]


def _apply_drift_changes(
    data: dict[str, list[str] | str],
    field_name: str,
    missing: list[str],
    stale: list[str],
    plugin_name: str,
    *,
    dry_run: bool,
) -> None:
    """Report and optionally apply missing/stale changes to a component array.

    Args:
        data: Plugin.json data dictionary (mutated in place unless dry_run)
        field_name: Array field name
        missing: Items to add
        stale: Items to remove
        plugin_name: Plugin name for logging
        dry_run: If True, only report
    """
    label_add = "Would add" if dry_run else "Adding"
    label_rm = "Would remove" if dry_run else "Removing"

    for item in missing:
        print(f"  {label_add} {field_name}: {item} ({plugin_name})")

    for item in stale:
        print(f"  {label_rm} stale {field_name}: {item} ({plugin_name})")

    if dry_run:
        return

    if missing:
        if field_name not in data:
            data[field_name] = []
        field_value = data[field_name]
        if isinstance(field_value, list):
            field_value.extend(missing)

    if stale:
        field_value = data.get(field_name, [])
        if isinstance(field_value, list):
            data[field_name] = [r for r in field_value if r not in stale]


def _reconcile_component_array(
    data: dict[str, list[str] | str], field_name: str, disk_items: list[str], plugin_name: str, *, dry_run: bool
) -> bool:
    """Reconcile a component array (skills/agents/commands) against disk.

    Args:
        data: Plugin.json data dictionary (mutated in place unless dry_run)
        field_name: Array field name (``skills``, ``agents``, ``commands``)
        disk_items: Items discovered on disk
        plugin_name: Plugin name for logging
        dry_run: If True, only report

    Returns:
        True if drift was detected
    """
    raw = data.get(field_name, [])
    registered = list(raw) if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    normalize = field_name == "skills"

    missing = _find_missing_items(disk_items, registered, normalize=normalize)
    stale = _find_stale_items(registered, disk_items, normalize=normalize)

    if missing or stale:
        _apply_drift_changes(data, field_name, missing, stale, plugin_name, dry_run=dry_run)
        return True

    return False


def _apply_marketplace_drift(
    data: _MarketplaceJsonData,
    plugins_list: list[_MarketplacePluginEntry],
    missing: set[str],
    stale: set[str],
    disk_plugins: dict[str, str],
    *,
    dry_run: bool,
) -> None:
    """Print and apply missing/stale plugin changes to marketplace data in place.

    Args:
        data: Marketplace JSON data (mutated in place when not dry_run).
        plugins_list: Current plugins list from data (mutated in place for additions).
        missing: Plugin names present on disk but absent from marketplace.json.
        stale: Plugin names in marketplace.json with no matching directory on disk.
        disk_plugins: Map of {canonical_name: dir_name} for plugins found on disk.
        dry_run: If True, only report changes without writing.
    """
    if missing:
        label = "Would add" if dry_run else "Adding"
        for name in sorted(missing):
            print(f"  {label} plugin to marketplace: {name}")
        if not dry_run:
            plugins_list.extend(
                _MarketplacePluginEntry(name=name, source=f"./plugins/{disk_plugins[name]}") for name in sorted(missing)
            )

    if stale:
        label = "Would remove" if dry_run else "Removing"
        for name in sorted(stale):
            print(f"  {label} stale plugin from marketplace: {name}")
        if not dry_run:
            data["plugins"] = [p for p in plugins_list if p["name"] not in stale]


def _reconcile_marketplace(plugins_root: Path, *, dry_run: bool) -> bool:
    """Reconcile marketplace.json against plugins on disk.

    Ensures every plugin directory with a valid plugin.json is listed
    in marketplace.json, and removes entries for deleted plugins.

    Args:
        plugins_root: Path to the plugins/ directory
        dry_run: If True, only report

    Returns:
        True if changes were made (or would be made in dry_run)
    """
    marketplace_path = Path(".claude-plugin/marketplace.json")
    if not marketplace_path.exists():
        print("Warning: marketplace.json not found")
        return False

    with marketplace_path.open(encoding="utf-8") as f:
        data: _MarketplaceJsonData = json.load(f)

    plugins_list = data.get("plugins", [])
    # Only track locally-sourced plugins (relative path strings) in the stale check.
    # Plugins with external sources (dict with "source": "github" etc.) are managed
    # manually and must not be removed by reconciliation.
    registered_names = {p["name"] for p in plugins_list if _marketplace_entry_source(p) is not None}

    # Discover plugins on disk — keyed by canonical name (from plugin.json),
    # mapped to directory name (for the source field).
    # Using canonical name for comparison avoids mismatches when the directory
    # name differs from the "name" field in plugin.json (e.g. dir=the-rewrite-room,
    # name=rwr).
    disk_plugins: dict[str, str] = {}  # {canonical_name: dir_name}
    for d in sorted(plugins_root.iterdir()):
        if d.is_dir() and (d / ".claude-plugin" / "plugin.json").exists():
            disk_plugins[_read_plugin_name(d.name)] = d.name

    missing = set(disk_plugins.keys()) - registered_names
    stale = registered_names - set(disk_plugins.keys())
    _apply_marketplace_drift(data, plugins_list, missing, stale, disk_plugins, dry_run=dry_run)

    if (missing or stale) and not dry_run:
        metadata: _MarketplaceMetadata = data.get("metadata", {})
        current_version = metadata.get("version", "0.0.0")
        bump_type: Literal["major", "minor", "patch"] = "major" if stale else "minor"
        metadata["version"] = bump_version(current_version, bump_type)
        data["metadata"] = metadata
        _write_json_lf(marketplace_path, _format_json(data))
        print(f"  Updated marketplace -> {metadata['version']}")

    return bool(missing or stale)


def reconcile(*, dry_run: bool) -> int:
    """Run full filesystem reconciliation.

    Scans all plugin directories, compares against manifest files,
    and fixes drift.

    Args:
        dry_run: If True, only report drift without modifying files

    Returns:
        Exit code (0 for success)
    """
    plugins_root = Path("plugins")
    if not plugins_root.is_dir():
        sys.stderr.write("Error: plugins/ directory not found\n")
        return 1

    mode = "DRY RUN" if dry_run else "RECONCILE"
    print(f"[{mode}] Scanning plugins/ for manifest drift...\n")

    any_drift = False

    # Reconcile each plugin
    for plugin_dir in sorted(plugins_root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
        if not plugin_json.exists():
            continue

        drift = _reconcile_one_plugin(plugin_dir.name, plugins_root, dry_run=dry_run)
        if drift:
            any_drift = True

    # Reconcile marketplace
    print()
    drift = _reconcile_marketplace(plugins_root, dry_run=dry_run)
    if drift:
        any_drift = True

    if not any_drift:
        print("No drift detected — all manifests match filesystem.")
    elif dry_run:
        print("\nDrift detected. Run without --dry-run to fix.")
        return 1

    return 0


def reconcile_native_manifests(*, dry_run: bool) -> int:
    """Reconcile native component arrays and catalog membership.

    Returns:
        One for detected drift in dry-run mode, otherwise zero.
    """
    drift = False
    for manifest in discover_manifests():
        if manifest.kind != "plugin":
            continue
        source = manifest_root(manifest)
        data = json.loads(manifest.path.read_text(encoding="utf-8"))
        changed = False
        components = {
            "skills": _discover_skills(source),
            "agents": _discover_agents(source),
            "commands": _discover_commands(source) + _discover_invocable_skills(source),
        }
        for field, items in components.items():
            if isinstance(data.get(field), list):
                changed |= _reconcile_mode_b(data, field, items, source.as_posix(), dry_run=dry_run)
        if changed and not dry_run:
            data["version"] = bump_version(data.get("version", "0.0.0"), "minor")
            _write_json_lf(manifest.path, _format_json(data))
        drift |= changed
    drift |= bool(sync_native_marketplaces(bump=False, dry_run=dry_run))
    print("Drift detected." if drift else "No drift detected — all manifests match filesystem.")
    return int(drift and dry_run)


def _report_plugin_update(plugin_name: str, new_version: str, changes: ComponentChanges) -> None:
    """Print a summary of component changes for a plugin update.

    Args:
        plugin_name: Name of the updated plugin
        new_version: New version after bumping
        changes: The component changes that triggered the update
    """
    parts: list[str] = []
    if changes["added"]:
        parts.append(f"+{len(changes['added'])}")
    if changes["deleted"]:
        parts.append(f"-{len(changes['deleted'])}")
    if changes["modified"]:
        parts.append(f"~{len(changes['modified'])}")
    print(f"Updated {plugin_name} -> {new_version} ({', '.join(parts)} components)")


def _precommit_sync() -> int:
    updated = sync_staged_manifests()
    for marketplace_path in sync_native_marketplaces(bump=False):
        _git_stage_file(marketplace_path.as_posix())
    if not updated:
        print("Info: No manifest updates needed")
    return 0


def _sync_marketplace_mode() -> int:
    sync_native_marketplaces()
    return 0


def main() -> int:
    """Main entry point — dispatches to pre-commit, reconcile, or sync-marketplace mode.

    Returns:
        Exit code (0 for success)
    """
    parser = argparse.ArgumentParser(description="Sync plugin and marketplace manifests.")
    parser.add_argument(
        "--reconcile", action="store_true", help="Full directory scan to fix drift between filesystem and manifests"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report drift without modifying files (requires --reconcile)"
    )
    parser.add_argument(
        "--sync-marketplace",
        action="store_true",
        help="Post-merge mode: reconcile marketplace.json structure and bump version (for CI use)",
    )
    args = parser.parse_args()

    if args.sync_marketplace:
        return _sync_marketplace_mode()

    if args.reconcile:
        if not args.dry_run:
            sync_native_marketplaces()
        return 0

    return _precommit_sync()


if __name__ == "__main__":
    sys.exit(main())
