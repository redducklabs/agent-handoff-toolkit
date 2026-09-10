# Repository instructions

Read `docs/agent-handoff/contract.md` before changing record behavior, templates, validation, rendering, hooks, or the handoff skill.

- Preserve the continuation/completion-audit distinction.
- Keep the core package independent of Claude Code, Codex, GitHub, and tracker APIs.
- Automatic hooks fail open; explicit CLI validation fails closed.
- Never claim semantic truth is mechanically verified. Validation proves structure and internal consistency only.
- Add or update tests before implementation changes and run the full verification commands in `README.md` before reporting success.
- Do not add secrets, credentials, prompts, replies, transcripts, or customer data to fixtures or logs.
- Use `gh` for GitHub operations.
- Do not use `gh pr merge --auto` as a check gate unless repository rulesets
  actually require the checks. Without that protection GitHub merges
  immediately; inspect `gh pr checks` and merge only after every CI job
  reports `pass`.
- Do not add AI attribution to commits or pull requests.
- On Windows, invoke bundled Bash helper scripts only after confirming they use LF line endings; CRLF copies fail at `set -o pipefail`. Use the equivalent PowerShell setup when they are not portable.
- Resolved Windows setup warning: cached Bash helpers had CRLF and were unusable directly in this worktree. Use an equivalent PowerShell scratch-artifact generator with `git log` and `git diff`; source edits still use `apply_patch`.
- Resolved Claude CLI review warning: `--allowedTools` consumes trailing positional values, so a review prompt placed after that variadic option is treated as another tool value and `--print` reports that no input was provided. Pass the prompt before variadic options or provide it on standard input.
- Resolved PowerShell review-package warning: inserting command-output arrays directly into a larger array and joining it serializes nested results as `System.Object[]`. Convert each `git log`, `git diff --stat`, and `git diff` result to a newline-joined string before composing the scratch review package.
- Resolved lifecycle-storage test warning: before a RED test forces a platform-fallback branch, override every platform state-root environment variable to paths inside the test's temporary root; mocking a selector that does not exist yet does not isolate the branch actually executed.
- Resolved PowerShell test-helper warning: positional values placed after `powershell -Command` did not populate `$args` as expected in this workflow. Embed already-quoted literal temporary paths in the single PowerShell command instead.
- In a tag-only or shallow release checkout, `git fetch origin main` may not create `origin/main`. Run `git fetch origin main:refs/remotes/origin/main` before creating a main-based worktree.
- Run Ruff through the repository configuration in `pyproject.toml`; unconfigured broad rules incorrectly reject the intentional catch-all boundary that makes automatic hooks fail open.

These repository-local rules apply to both Codex and Claude Code. Keep
`AGENTS.md` and `CLAUDE.md` semantically identical.
