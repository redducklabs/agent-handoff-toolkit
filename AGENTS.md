# Repository instructions

Read `docs/contract.md` before changing record behavior, templates, validation, rendering, hooks, or the handoff skill.

- Preserve the continuation/completion-audit distinction.
- Keep the core package independent of Claude Code, Codex, GitHub, and tracker APIs.
- Automatic hooks fail open; explicit CLI validation fails closed.
- Never claim semantic truth is mechanically verified. Validation proves structure and internal consistency only.
- Add or update tests before implementation changes and run the full verification commands in `README.md` before reporting success.
- Do not add secrets, credentials, prompts, replies, transcripts, or customer data to fixtures or logs.
- Use `gh` for GitHub operations.
- Do not add AI attribution to commits or pull requests.
- On Windows, invoke bundled Bash helper scripts only after confirming they use LF line endings; CRLF copies fail at `set -o pipefail`. Use the equivalent PowerShell setup when they are not portable.

`CLAUDE.md` carries the same repository-local rules for Claude Code. Keep the two files semantically identical.
