# agent-marketplace-versioner

Manage native agent plugin and marketplace versions from the consumer Git repository.
The distributable hook and composite GitHub Action invoke the same CLI.

## Commands

| Command | Effect |
| --- | --- |
| `check --base-ref BASE --head-ref HEAD` | Read-only gate; exits 1 when required version bumps are missing. |
| `audit` | Read-only JSON drift report. |
| `sync` | Update and stage versions affected by the Git index. |
| `sync --marketplace` | Reconcile and version native marketplace catalogs. |
| `repair` | Apply manifest repairs and report results as JSON. |
| `reconcile --dry-run` | Preview full reconciliation; exits 1 when changes are needed. |
| `reconcile` | Repair explicit native component arrays and catalog membership; catalog versions are unchanged. |

Both revisions must exist locally for `check`. A shallow checkout may need
additional history. Mutation commands change local files; the caller owns review,
commit and publication.

## Git hook

The pre-commit hook is optional local-development support. Plugin version bumps
act as cache busters: maintainers refresh installed plugins with their host's
refresh or reinstall workflow, because a running agent keeps the version it loaded.

Add this to the consumer's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/Jamie-BitFlight/agent-marketplace-versioner
    rev: v1
    hooks:
      - id: agent-marketplace-versioner
```

Run `prek install` or `pre-commit install`. Both runners install the Python package
and call `agent-marketplace-versioner sync` once per pre-commit run, without passing
filenames. Stage intended content changes before running the hook. It stages
updated version manifests and preserves the same bump on repeat runs.
Each plugin source root computes one shared version for all of its native manifests,
using the highest sibling version as its staged-change baseline.
Catalog entry additions and removals are also reconciled and staged, without
changing the catalog version or introducing a version field.

`rev: v1` is a moving compatibility tag, but each runner caches the revision first
resolved for that literal value. After a new v1 release, refresh an existing local
installation before expecting it to run the newer hook:

```sh
prek clean && prek install --install-hooks
# or
pre-commit clean && pre-commit install --install-hooks
```

## GitHub Action

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: Jamie-BitFlight/agent-marketplace-versioner@v1
```

The default `command: check` reads the consumer repository at `repository`
(the workflow workspace by default). `base-ref` and `head-ref` default to the pull
request's base and head, or to the pushed range on `push`. Without a base, as on a
new-branch push or `workflow_dispatch`, `check` compares against the default branch.
`sync` with `marketplace: true` also uses both refs when a base exists.
Pass `command: sync`, `repair`, or `reconcile` only when local mutation is intended.
The action does not commit or push. It supports Linux and macOS Bash runners.

For post-merge catalog reconciliation, use `command: sync` with `marketplace: true`.
This invokes `sync --marketplace`. The default `marketplace: false` keeps ordinary
staged-manifest synchronization; the input has no effect on other commands.

Run this release-like workflow on the default branch after merges, separately from
optional local cache-busting:

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: Jamie-BitFlight/agent-marketplace-versioner@v1
  with:
    command: sync
    marketplace: true
```

It reconciles catalog entries and bumps existing marketplace versions, preserving
versionless catalogs. Add a commit-and-push step to publish the result.

The `v1` tag advances only through compatible v1 releases. Pin an immutable
commit SHA instead when your supply-chain policy requires it. The action installs
the source at its selected checkout with `uv sync --locked`
and no development dependencies. GitHub downloads actions without Git metadata,
so the temporary installation uses package metadata version `0+action`; the
reviewed action commit selects the actual code, and its lockfile selects dependencies.
