# Agent guide

## Personal Git strategy

These rules are mandatory for repository work.

- Before editing: verify `pwd`, `git status -sb`, current branch, remotes, and worktrees. Keep repository ownership explicit.
- Start work from the current `origin/main` in a fresh worktree when possible. Do not build on stale local `main`.
- Use an intention-revealing branch name: `feat/`, `fix/`, `chore/`, or `docs/` plus a short scope.
- Keep each change focused. Stage explicit paths. Do not silently include unrelated worktree changes.
- Push the branch and use a pull request for normal work. Do not push directly to `main` after repository bootstrap unless explicitly approved in the current request.
- Before merging, recheck the PR head, latest reviews, review threads, checks, `origin/main`, and mergeability live.
- Merge, deploy, production writes, and other consequential mutations are separate approvals. PR readiness is not merge approval.
- Merge with squash only. Reactions and Codex review are not approval. Wait for a real review body or explicit go-ahead.
- After conflicts: fetch current `origin/main`, rebase, rerun local validation, then push with `--force-with-lease`. Never force-push blindly.
- After merge: verify the merged head, `origin/main`, PR/issue closure when relevant, and cleanup of safe temporary branches/worktrees.
- Keep proof precise: distinguish local results, GitHub metadata, backend readbacks, visible-device proof, and deployed/live proof. Never claim a stronger result than the evidence.
- Use GitHub CLI as the primary GitHub interface. Never run `gh auth refresh` unless explicitly requested. If a GitHub command fails only inside the sandbox, retry that same command with escalation before reporting GitHub blocked.

## Dashboard project rules

- Upstream contributions must use fake account values in the configuration example and generated dashboard. Adopter repositories may track their own configuration and generated dashboard according to their security policy.
- Keep `.github/repository-metadata.yml` aligned with the GitHub About section, topics, and social preview.
- Edit `workflow-monitoring.scaffold.lvdash.json`, then run the generator. Never edit the generated `workflow-monitoring.lvdash.json` directly.
- Live SQL validation must remain read-only. Do not create dummy tables or insert test rows.
- Report empty-query validation accurately. It proves SQL syntax, referenced fields, and access, not real workflow metric correctness.
- Use simplified technical English. Comment non-obvious intent and constraints, but do not restate self-explanatory code.
- Do not use em dashes in code, comments, documentation, or commit messages.

## Pipelines: local act only

This repository must not run pipelines on GitHub.

- Never create or modify `.github/workflows/`.
- Never configure GitHub Actions, run `gh workflow run`, or use a GitHub-hosted runner for validation.
- Store local workflow definitions under `.act/workflows/`.
- Build the repository-specific runner image from `.act/Dockerfile`:

  ```sh
  docker build --platform linux/arm64 -t databricks-workflow-monitoring-dashboards-act:local -f .act/Dockerfile .
  ```

- Run workflows with local `act` and that image:

  ```sh
  act --container-architecture linux/arm64 --pull=false \
    -P ubuntu-latest=databricks-workflow-monitoring-dashboards-act:local \
    -W .act/workflows/validate.yml
  ```

- Replace `linux/arm64` with `linux/amd64` on Intel or AMD machines.
- Preserve the command and outcome as validation evidence.
- If Docker, `act`, or the local image is unavailable, stop and repair the local gate. Do not substitute GitHub Actions.

## Change quality

- Diagnose first, then make the smallest durable change that fully meets the current requirement.
- Use established libraries and tools where suitable. Do not add compatibility layers or unfinished stopgaps.
- Keep names intention-revealing and domain-specific.
- Fix all issues found during the session, while keeping unrelated scope explicit.
