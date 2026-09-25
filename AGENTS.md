# AGENTS.md

## Before editing

1. **Define success.** State the required outcome, acceptance criteria, preserved behavior, constraints, and non-goals.
2. **Map the boundary.** Identify owners, consumers, inputs, outputs, dependencies, interfaces, and affected environments.
3. **Separate evidence from inference.** Observed state — code, tests, file placement, wiring, versions — shows what exists, not intended ownership or the correct change location.
4. **Resolve consequential uncertainty.** State assumptions and competing interpretations. Resolve any that could change scope, compatibility, ownership, or architecture before editing.
5. **Pick the smallest viable change.** Reuse existing abstractions and dependencies. Compare approaches by evidence, blast radius, maintenance cost, and reversibility.

## Claims

These rules govern statements: replies, issues, commit messages, code comments, and subagent briefs.

- **A claim needs a command.** State a fact about a codebase, library, or tool only when a check run in this session established it. Otherwise mark the claim _unverified_ in the same sentence.
- **A subagent's report is a claim.** Check the artifact it describes before you repeat it or build on it.
- **Contradicting evidence stops the work.** An empty grep, a truncated capture, or a result that disagrees with the plan in flight — resolve it before continuing.
- **A published claim becomes a premise.** An _unverified_ claim in an issue, commit, or comment reads as fact to later readers. When you correct it, revisit the work built on it.
- **Committed state is the repository.** Read `git show <ref>:<path>` before describing what a repository contains; a working tree can hold another session's uncommitted work.
- **Resolution shows tool use, configuration does not.** Check where a tool actually resolves from — the lockfile, the hook, the CI step. A disabled stanza greps as present; a tool can be configured in a file that never names it.

## Editing

- Keep every changed line inside the defined outcome; leave unrelated code as it is.
- Match established repository conventions.
- Remove only artifacts your change made obsolete.
- Reassess the plan when evidence contradicts an assumption.
- Change dependencies only through `uv add` and `uv remove`.
- Change Action inputs in `action.yml`. On `main`, `update-readme.yml` regenerates the README blocks between `<!-- start … -->` and `<!-- end … -->` markers from it.

## Verification

The gate is these three commands. The hook set covers lint, formatting, type-check, workflow, Markdown, and shell; a subset is a different gate.

```sh
uv run prek run --all-files
uv run pytest
uv build
```

- Validate each affected _boundary_ independently; a pass in one test, harness, environment, or consumer proves only that one.
- Test required behavior over implementation detail.

## Before commit

Review the complete diff against this file.

- Every changed line serves the defined outcome.
- Every new constraint, threshold, default, prohibition, workflow step, fallback, abstraction, or policy is justified by the user requirement, repository evidence, an external contract, or measured behavior. Remove the rest, or label it a hypothesis.
- Re-run the gate when the review changes behavior.

Commit once the diff holds no unjustified constraint and no instruction violation.

## Completion

Declare success when the acceptance criteria are demonstrated, the gate passes, and every affected _boundary_ is verified. State remaining uncertainty, limitations, and follow-up work.
