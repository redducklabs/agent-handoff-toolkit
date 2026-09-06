from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "distribution" / "manifest.json"

CONTINUATION_SECTIONS = [
    "Objective",
    "Authoritative references",
    "User decisions",
    "Repository state",
    "Completed work",
    "Verification evidence",
    "Incomplete work and risks",
    "Exact next action",
    "External effects",
    "Remaining code by active scope",
    "Next-session prompt",
]

AUDIT_SECTIONS = [
    "Completed objective",
    "Authoritative references",
    "User decisions",
    "Final repository state",
    "Completed work",
    "Verification evidence",
    "Known risks or separately tracked follow-ups",
    "External effects",
]

REQUIRED_SKILL_GATES = {
    "Choose the record type",
    "Close gating questions",
    "Reconcile live state",
    "Validate the record",
    "Render the final response tail",
}


def load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def level_two_headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


class DistributionTests(unittest.TestCase):
    def test_manifest_hashes_every_managed_artifact(self) -> None:
        manifest = load_manifest()

        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["record_schema_version"], 1)
        artifacts = manifest["artifacts"]
        self.assertIsInstance(artifacts, list)
        self.assertGreater(len(artifacts), 0)

        seen_sources: set[str] = set()
        seen_targets: set[str] = set()
        for artifact in artifacts:
            source = artifact["source"]
            source_path = PurePosixPath(source)
            self.assertFalse(source_path.is_absolute())
            self.assertNotIn("..", source_path.parts)
            self.assertNotIn(source, seen_sources)
            seen_sources.add(source)

            payload = (ROOT / Path(*source_path.parts)).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), artifact["sha256"])

            install = artifact["install"]
            self.assertIn(install["mode"], {"copy", "merge-json"})
            for target in install["targets"]:
                target_path = PurePosixPath(target)
                self.assertFalse(target_path.is_absolute())
                self.assertNotIn("..", target_path.parts)
                self.assertNotIn(target, seen_targets)
                seen_targets.add(target)

    def test_managed_artifact_bytes_are_stable_across_git_checkouts(self) -> None:
        sources = [artifact["source"] for artifact in load_manifest()["artifacts"]]

        result = subprocess.run(
            ["git", "check-attr", "eol", "--", *sources],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        attributes = {
            line.split(": ", 2)[0]: line.split(": ", 2)[2]
            for line in result.stdout.splitlines()
        }
        self.assertEqual(attributes, {source: "lf" for source in sources})

    def test_skill_has_byte_identical_dual_host_install_semantics(self) -> None:
        manifest = load_manifest()
        skill = next(
            artifact
            for artifact in manifest["artifacts"]
            if artifact["source"] == "skills/agent-handoff/SKILL.md"
        )

        self.assertEqual(skill["install"]["mode"], "copy")
        self.assertEqual(skill["install"]["transform"], "none")
        self.assertEqual(
            set(skill["install"]["targets"]),
            {
                ".agents/skills/agent-handoff/SKILL.md",
                ".claude/skills/agent-handoff/SKILL.md",
            },
        )

    def test_skill_exposes_each_required_behavior_gate(self) -> None:
        skill_path = ROOT / "skills" / "agent-handoff" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        lines = text.splitlines()

        self.assertEqual(lines[0], "---")
        frontmatter_end = lines.index("---", 1)
        fields = dict(
            line.split(":", 1) for line in lines[1:frontmatter_end] if ":" in line
        )
        self.assertEqual(fields["name"].strip(), "agent-handoff")
        self.assertTrue(fields["description"].strip().startswith("Use when "))
        self.assertLessEqual(len("\n".join(lines[: frontmatter_end + 1])), 1024)

        gates = {
            match.group(1)
            for line in lines
            if (match := re.fullmatch(r"## Gate: (.+)", line))
        }
        self.assertEqual(gates, REQUIRED_SKILL_GATES)

    def test_templates_match_the_contract_section_shapes(self) -> None:
        continuation = (ROOT / "templates" / "continuation.md").read_text(
            encoding="utf-8"
        )
        audit = (ROOT / "templates" / "completion-audit.md").read_text(encoding="utf-8")

        self.assertTrue(continuation.startswith("<!-- agent-handoff-metadata\n"))
        self.assertEqual(level_two_headings(continuation), CONTINUATION_SECTIONS)
        self.assertTrue(
            audit.startswith(
                "> Audit record — not a handoff. Do not use this file to start or "
                "continue a session.\n\n<!-- agent-handoff-metadata\n"
            )
        )
        self.assertEqual(level_two_headings(audit), AUDIT_SECTIONS)
        self.assertNotIn("## Exact next action", audit)
        self.assertNotIn("## Next-session prompt", audit)

    def test_hook_fragments_use_the_supported_event_matrix(self) -> None:
        claude = json.loads(
            (ROOT / "adapters" / "claude" / "settings.fragment.json").read_text(
                encoding="utf-8"
            )
        )
        codex = json.loads(
            (ROOT / "adapters" / "codex" / "hooks.fragment.json").read_text(
                encoding="utf-8"
            )
        )

        for platform, fragment, matcher in (
            ("claude", claude, "Write|Edit|MultiEdit"),
            ("codex", codex, "apply_patch"),
        ):
            self.assertEqual(set(fragment["hooks"]), {"SessionStart", "PostToolUse"})
            self.assertNotIn("Stop", fragment["hooks"])
            self.assertEqual(fragment["hooks"]["PostToolUse"][0]["matcher"], matcher)
            for host_event, cli_event in (
                ("SessionStart", "session-start"),
                ("PostToolUse", "post-tool-use"),
            ):
                hook = fragment["hooks"][host_event][0]["hooks"][0]
                self.assertEqual(hook["type"], "command")
                self.assertEqual(
                    hook["command"],
                    "python -m agent_handoff_toolkit hook "
                    f"--platform {platform} --event {cli_event}",
                )


if __name__ == "__main__":
    unittest.main()
