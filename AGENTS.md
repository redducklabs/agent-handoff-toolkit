# Repository instructions

Read `docs/agent-handoff/contract.md` and `docs/agent-handoff/mechanics.md` before changing record behavior, templates, validation, rendering, hooks, or the handoff skill. The contract states what an author must write; mechanics states the formats and enforcement the toolkit applies.

- Preserve the continuation/completion-audit distinction.
- Keep the core package independent of Claude Code, Codex, GitHub, and tracker APIs.
- Informational hooks fail open; tracked lifecycle hooks fail closed. Explicit CLI validation fails closed.
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
- Resolved SDD review-package warning: relative scratch-package paths resolve against the command's working directory. Run the generator from the intended linked worktree or use its verified absolute workspace path; otherwise it targets the primary checkout where the plan workspace does not exist.
- In a tag-only or shallow release checkout, `git fetch origin main` may not create `origin/main`. Run `git fetch origin main:refs/remotes/origin/main` before creating a main-based worktree.
- Resolved Git Bash warning: MSYS path conversion rewrites a `rev:path` argument such as `git show origin/main:.agent-handoff-toolkit/install-state.json` into `origin\main;.agent-handoff-toolkit\install-state.json`, and Git then reports an ambiguous argument. Export `MSYS_NO_PATHCONV=1` for those commands.
- Run Ruff through the repository configuration in `pyproject.toml`; unconfigured broad rules incorrectly reject the intentional catch-all boundary that makes automatic hooks fail open.
- Resolved Windows verification warning: a default temporary directory beneath an unrelated Git checkout used to make non-Git lifecycle tests inherit that checkout and create test state in its metadata. Tests now set `GIT_CEILING_DIRECTORIES`, which fences `git rev-parse`, and an empty `.git` directory in the temporary consumer, which fences only the installer's pure-Python ancestor walk. Git ignores a ceiling that is not a strict ancestor of the directory being resolved, so set the ceiling above the directory the test resolves from: the temporary root when resolving a child of it, its parent when resolving the root itself. No `TEMP`/`TMP` preparation is needed.

These repository-local rules apply to both Codex and Claude Code. Keep
`AGENTS.md` and `CLAUDE.md` semantically identical.
