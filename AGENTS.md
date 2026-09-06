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

`CLAUDE.md` carries the same repository-local rules for Claude Code. Keep the two files semantically identical.
