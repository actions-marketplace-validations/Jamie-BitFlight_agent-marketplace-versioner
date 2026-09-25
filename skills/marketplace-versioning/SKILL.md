---
name: marketplace-versioning
description: agent-marketplace-versioner setup and troubleshooting for an agent plugin marketplace repo. Use when installing its pre-commit hook or GitHub Action, or when its `check` gate fails.
---

# Marketplace versioning

Reference lives in the versioner repository; fetch the page a step names.

- **setup** — install, release pinning, manifest layouts, approach choice, formatter exclusions, troubleshooting: <https://github.com/Jamie-BitFlight/agent-marketplace-versioner/blob/main/docs/setup.md>
- **index** — command semantics, hook and Action snippets: <https://github.com/Jamie-BitFlight/agent-marketplace-versioner/blob/main/docs/index.md>
- **action.yml** — Action inputs: <https://github.com/Jamie-BitFlight/agent-marketplace-versioner/blob/main/action.yml>
- **CLI** — `agent-marketplace-versioner --help` and `<command> --help`

## Set up

1. **Match manifests to layouts.** Compare the repo's plugin and marketplace manifests against "Manifest layouts" in setup. Done when every manifest the user wants versioned matches a listed layout and is Git-visible.
2. **Choose the approach.** Read "Choosing a versioning approach" in setup. Recommend Hook + CI or CI-only from the repo's evidence: who commits, and which hooks already exist. Done when the user confirms the approach.
3. **Exclude manifests from formatters.** Apply "Exclude manifests from formatters and linters" in setup. Done when every formatter and linter config, and every pre-commit formatter hook, excludes all four manifest globs.
4. **Pin one release.** Follow "Pin one release everywhere" in setup. Done when the hook `rev`, every Action `uses:`, and any CLI version name the same release commit.
5. **Wire enforcement.** Add the hook, the `check` workflow, or both, per the chosen approach, from the index snippets with the step 4 pin as the ref. Done when the `check` workflow checks out with `fetch-depth: 0`.
6. **Wire the post-merge marketplace workflow.** Follow the approach's post-merge step in setup, including its skip condition. Done when the workflow publishes its result by push or by a PR holding every changed manifest, or the skip condition holds.
7. **Verify.** Run the repo's hook set twice. Done when the second run passes and leaves no manifest modified.

## Troubleshoot

Match the symptom in the setup Troubleshooting table and apply its fix. Done when the failing command re-runs clean.
