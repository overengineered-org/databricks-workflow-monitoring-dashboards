# Agent guide

Read this guide before changing repository files.
Use `CONTRIBUTING.md` for local setup and validation commands.

## Personal Git strategy

These rules are mandatory for repository work.

### Before editing

- Verify `pwd`, `git status -sb`, current branch, remotes, and worktrees.
- Start from current `origin/main` in a fresh worktree when possible.
- Use a clear `feat/`, `fix/`, `chore/`, or `docs/` branch name.
- Keep ownership explicit. Stage explicit paths and preserve unrelated changes.

### Pull requests

- Push a branch and open a pull request for normal work.
- Do not push to `main` without explicit approval in the current request.
- Before merging, recheck the head, reviews, threads, checks, `origin/main`, and mergeability.
- Treat merge, deploy, and production writes as separate approvals.
- Use GitHub CLI. Never run `gh auth refresh` unless explicitly requested.

If GitHub fails only inside the sandbox, retry that command with escalation before reporting a
blocker.

### Merge and proof

- Squash-merge only after a real review body or explicit approval.
- After conflicts, rebase on current `origin/main`, validate, then use `--force-with-lease`.
- After merge, verify the merged head, main, related closure, and safe cleanup.
- Separate local, GitHub, backend, visible-device, deployed, and live proof.
- Never claim stronger proof than the evidence.

## Dashboard project rules

### Source files

- Use fake account values in upstream configuration and generated dashboards.
- Adopter repositories may track their own configuration under their security policy.
- Keep repository metadata aligned with the GitHub About section, topics, and social preview.
- Edit the dashboard scaffold, then run the generator. Never edit the generated dashboard.

### Validation and language

- Keep live SQL validation read-only. Do not create dummy tables or insert test rows.
- Empty results prove SQL syntax, fields, and access, not metric correctness.
- Use simplified technical English.
- Comment non-obvious intent and constraints only.
- Do not use em dashes in code, comments, documentation, or commit messages.

## Pipelines: local act only

This repository must not run pipelines on GitHub.

- Never create or modify `.github/workflows/`.
- Never configure or run GitHub Actions.
- Store local workflow definitions under `.act/workflows/`.
- Run the repository-specific local gate through its wrapper:

  ```sh
  scripts/run-local-validation.sh
  ```

- The wrapper selects the host architecture and rebuilds the fixed image.
- It reuses the labelled Act container while its image and checkout are current.
- It removes only dangling images.
- Do not bypass the wrapper for the standard local gate.

If Docker, `act`, or the local image is unavailable, repair the local gate. Do not substitute
GitHub Actions. Preserve the wrapper command and outcome as evidence.

## Change quality

- Diagnose first, then make the smallest durable change that fully meets the current requirement.
- Use established libraries and tools where suitable.
- Do not add compatibility layers or unfinished stopgaps.
- Keep names intention-revealing and domain-specific.
- Fix all issues found during the session, while keeping unrelated scope explicit.

Next: verify the repository state before editing.
