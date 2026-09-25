# Setup

Install paths, supported manifest layouts, and the choice between versioning
approaches. Command semantics live in [Commands](index.md#commands); hook and
Action snippets in [Git hook](index.md#git-hook) and
[GitHub Action](index.md#github-action).

## Install

| Surface | Install |
| --- | --- |
| CLI | `uv tool install agent-marketplace-versioner==<version>` (PyPI) → `agent-marketplace-versioner` on PATH; `uvx agent-marketplace-versioner@<version>` runs it without installing |
| pre-commit hook | Repo stanza in the consumer's `.pre-commit-config.yaml`, then `prek install` — see [Git hook](index.md#git-hook) |
| GitHub Action | `uses: Jamie-BitFlight/agent-marketplace-versioner@<rev>` — see [GitHub Action](index.md#github-action) |

No per-consumer adapter or versioner-specific configuration file is required.
The CLI needs `git` on PATH.

### Pin one release everywhere

Pin the hook `rev`, the Action ref, and the CLI version to the same release.
List the release tags:

```sh
git ls-remote --tags https://github.com/Jamie-BitFlight/agent-marketplace-versioner
```

An annotated tag prints two lines; the `refs/tags/vX.Y.Z^{}` line holds the
commit SHA. Use that SHA for `rev:` and `uses:`, with the tag as a comment:

```yaml
# .pre-commit-config.yaml
- repo: https://github.com/Jamie-BitFlight/agent-marketplace-versioner
  rev: 562755fd8f14ee76280130eb6db4612af72fdf45 # v0.1.2
  hooks:
    - id: agent-marketplace-versioner
```

```yaml
# .github/workflows/*.yml
- uses: Jamie-BitFlight/agent-marketplace-versioner@562755fd8f14ee76280130eb6db4612af72fdf45 # v0.1.2
```

To upgrade, repeat the lookup and replace every pin in one commit. The moving
`v1` tag advances with each `v1.x.y` release, and exists only once a `v1.x.y`
release does.

## Manifest layouts

The versioner manages Git-visible manifests at these paths and ignores every
other path, including gitignored files:

- **Plugin manifests** — `.<host>-plugin/plugin.json` (source root: the
  directory holding `.<host>-plugin/`), and `<name>.plugin.json` or
  `<name>-plugin.json` (source root: its own directory).
- **Marketplace catalogs** — `.<host>-plugin/marketplace.json` and
  `.agents/plugins/marketplace.json`.

All plugin manifests under one source root share one version. A repository root
holding `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, and
`kimi.plugin.json` bumps all three together.

A catalog is versioned only when it already carries `version` or
`metadata.version`. A versionless catalog still has its entries reconciled and
never gains a version field.

## Exclude manifests from formatters and linters

The versioner rewrites `plugin.json`, `*.plugin.json`, `*-plugin.json`, and
`marketplace.json` with a canonical style (2-space indent, trailing newline).
If a commit-time formatter (biome, prettier, oxfmt/oxlint, ...) also touches
them, each pass reformats against the other and hooks cycle until one side
stops; CI format checks flag the same churn. Exclude these files everywhere
they are formatted or linted — the formatter's own config so CI inherits it:

```text
# prettier: .prettierignore
**/plugin.json
**/*.plugin.json
**/*-plugin.json
**/marketplace.json
```

```json
// biome.json — formatter and linter; "**" must lead for negations to apply
{
  "files": {
    "includes": [
      "**",
      "!**/plugin.json",
      "!**/*.plugin.json",
      "!**/*-plugin.json",
      "!**/marketplace.json"
    ]
  }
}
```

For oxlint/oxfmt, list the same four globs in its ignore/exclude option.

For pre-commit, also set the formatter hook's `exclude` so it never receives the
manifests, keeping each versioner-managed file owned by exactly one tool. When
the formatter runs only through pre-commit, this `exclude` is the whole
exclusion:

```yaml
- id: oxfmt
  exclude: '(^|/)([^/]+[.-])?plugin\.json$|(^|/)marketplace\.json$'
```

## Choosing a versioning approach

**Hook + CI** — plugin authors commit locally and want immediate cache-busting:

1. The pre-commit hook runs `sync` on every commit: plugin manifest versions
   bump, catalog entries reconcile, and both stage locally.
2. A post-merge workflow on the default branch runs the Action with
   `command: sync`, `marketplace: true`: catalog entries reconcile and existing
   marketplace versions bump. The workflow owns commit and push of the result.
   When branch protection blocks the push, the workflow opens a PR holding every
   changed manifest; `GITHUB_TOKEN` PRs trigger no workflows, so use a token
   whose PRs do. Skip this step when every catalog is versionless.
3. Optional belt-and-braces: a PR workflow runs the default `command: check`
   gate.

**CI-only** — commits arrive from agents or bots, or hook maintenance is
unwanted; enforcement lives entirely at merge:

1. A PR workflow runs the Action default `command: check` (checkout with
   `fetch-depth: 0`). It exits 1 when changed plugin content lacks a version
   bump.
2. The PR author — human or agent — clears the gate by running
   `agent-marketplace-versioner repair` locally and committing the bumps.
3. The same post-merge workflow as Hook + CI step 2 reconciles catalog entries
   and bumps catalog versions. With no hook, it runs even when every catalog is
   versionless.

Choose Hook + CI when local plugin evaluation matters during development;
choose CI-only when one enforcement point at merge beats per-machine setup.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `check` exits 1 in CI | Changed plugin content is missing a version bump; run `repair`, commit the result |
| `check` cannot find a revision | Shallow checkout; use `fetch-depth: 0` so both refs exist locally |
| A manifest never bumps | Its path matches no [manifest layout](#manifest-layouts), or Git ignores it |
| Hook runs an older release than its `rev` tag now names | Runners cache the revision first resolved for a moving tag; refresh with `prek clean && prek install --install-hooks` — see [Git hook](index.md#git-hook) |
| Action ran but nothing was committed | By design; the Action never commits or pushes — the calling workflow owns publication |
| `git executable not found in PATH` | Install git; manifest discovery shells out to it |
