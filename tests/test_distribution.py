from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from urllib.parse import quote

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

EXPECTED_INSTALL_TARGETS = {
    "distribution/consumer-instructions.md": {"AGENTS.md", "CLAUDE.md"},
    "LICENSE": {".agent-handoff-toolkit/LICENSE"},
    "docs/agent-handoff/contract.md": {"docs/agent-handoff/contract.md"},
    "docs/consumer-integration.md": {".agent-handoff-toolkit/consumer-integration.md"},
    "skills/agent-handoff/SKILL.md": {
        ".agents/skills/agent-handoff/SKILL.md",
        ".claude/skills/agent-handoff/SKILL.md",
    },
    "templates/continuation.md": {"handoffs/templates/continuation.md"},
    "templates/completion-audit.md": {"handoffs/templates/completion-audit.md"},
    "adapters/claude/settings.fragment.json": {".claude/settings.json"},
    "adapters/codex/hooks.fragment.json": {".codex/hooks.json"},
    "distribution/runner.py": {".agent-handoff-toolkit/runner.py"},
    "src/agent_handoff_toolkit/__init__.py": {
        ".agent-handoff-toolkit/src/agent_handoff_toolkit/__init__.py"
    },
    "src/agent_handoff_toolkit/__main__.py": {
        ".agent-handoff-toolkit/src/agent_handoff_toolkit/__main__.py"
    },
    "src/agent_handoff_toolkit/cli.py": {
        ".agent-handoff-toolkit/src/agent_handoff_toolkit/cli.py"
    },
    "src/agent_handoff_toolkit/hooks.py": {
        ".agent-handoff-toolkit/src/agent_handoff_toolkit/hooks.py"
    },
    "src/agent_handoff_toolkit/records.py": {
        ".agent-handoff-toolkit/src/agent_handoff_toolkit/records.py"
    },
}

METADATA_RE = re.compile(
    r"<!-- agent-handoff-metadata\n(?P<metadata>.*?)\n-->", re.DOTALL
)


def load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def level_two_headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def template_metadata(text: str) -> tuple[dict[str, object], re.Match[str]]:
    match = METADATA_RE.search(text)
    if match is None:
        raise AssertionError("template metadata marker is missing")
    return json.loads(match.group("metadata")), match


def install_copy_artifacts(consumer: Path) -> None:
    for artifact in load_manifest()["artifacts"]:
        if artifact["install"]["mode"] != "copy":
            continue
        source = ROOT / Path(*PurePosixPath(artifact["source"]).parts)
        for raw_target in artifact["install"]["targets"]:
            target = consumer / Path(*PurePosixPath(raw_target).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def run_installed_command(
    command: str, consumer: Path, input_text: str = ""
) -> subprocess.CompletedProcess[str]:
    arguments = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        arguments = [
            argument[1:-1]
            if len(argument) >= 2
            and argument[0] == argument[-1]
            and argument[0] in {'"', "'"}
            else argument
            for argument in arguments
        ]
    if not arguments or arguments[0] != "python":
        raise AssertionError(f"unsupported hook command: {command}")
    return subprocess.run(
        [sys.executable, *arguments[1:]],
        cwd=consumer,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def run_source_cli(
    command: str, *arguments: str, target: Path, release: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "distribution/runner.py",
            command,
            "--target",
            str(target),
            "--release",
            release,
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def normalized_sha256(data: bytes) -> str:
    return hashlib.sha256(
        data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


class DistributionTests(unittest.TestCase):
    def test_public_repository_ci_uses_github_hosted_runners(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("runs-on: redducklabs-runners", workflow)

    def test_manifest_hashes_every_managed_artifact(self) -> None:
        manifest = load_manifest()

        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["toolkit_version"], "0.2.7")
        self.assertEqual(manifest["text_hash"], "utf8-lf-sha256-v1")
        self.assertIn(
            '__version__ = "0.2.7"',
            (ROOT / "src/agent_handoff_toolkit/__init__.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertIn(
            'version = "0.2.7"',
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual(manifest["record_schema_version"], 1)
        expected_release = f"v{manifest['toolkit_version']}"
        for documentation in ("README.md", "docs/consumer-integration.md"):
            release_references = set(
                re.findall(
                    r"v\d+\.\d+\.\d+",
                    (ROOT / documentation).read_text(encoding="utf-8"),
                )
            )
            self.assertEqual(release_references, {expected_release})
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
            self.assertEqual(normalized_sha256(payload), artifact["sha256"])

            install = artifact["install"]
            self.assertIn(install["mode"], {"copy", "managed-block", "merge-json"})
            for target in install["targets"]:
                target_path = PurePosixPath(target)
                self.assertFalse(target_path.is_absolute())
                self.assertNotIn("..", target_path.parts)
                self.assertNotIn(target, seen_targets)
                seen_targets.add(target)

        instructions = next(
            artifact
            for artifact in artifacts
            if artifact["source"] == "distribution/consumer-instructions.md"
        )
        self.assertEqual(instructions["install"]["mode"], "managed-block")
        self.assertEqual(instructions["block_id"], "agent-handoff-toolkit")
        self.assertEqual(instructions["install"]["targets"], ["AGENTS.md", "CLAUDE.md"])

        claude = next(
            artifact
            for artifact in artifacts
            if artifact["source"] == "adapters/claude/settings.fragment.json"
        )
        self.assertEqual(
            claude["array_identities"],
            [{"pointer": "/hooks/PostToolUse", "fields": ["matcher"]}],
        )

    def test_consumer_guidance_requires_prospective_acceptance_only(self) -> None:
        managed = (
            (ROOT / "distribution/consumer-instructions.md")
            .read_text(encoding="utf-8")
            .lower()
        )
        integration = (
            (ROOT / "docs/consumer-integration.md").read_text(encoding="utf-8").lower()
        )
        combined = re.sub(r"\s+", " ", f"{managed}\n{integration}")
        managed_normalized = re.sub(r"\s+", " ", managed)
        integration_normalized = re.sub(r"\s+", " ", integration)

        for phrase in (
            "deprecated historical artifact",
            "existed before the current pinned release was adopted",
            "do not open, read, review, validate, migrate, summarize, reconcile, or rewrite",
            "new or materially replaced records",
            "cannot loosen or contradict",
            "highest authorized scope",
            "derived from record metadata",
            "do not resolve questions from or mark individual legacy files",
            ".agent-handoff-toolkit/consumer-integration.md",
        ):
            self.assertIn(phrase, managed_normalized)

        for phrase in (
            "install-state.json` is the source of truth",
            "must report `current`",
            "consumer repository root",
            "automatic codex hook discovery",
            "consumer regression tests",
            "deprecated by policy",
            "rewrite",
            "not the acceptance test's oracle",
            "never derive expected values from the state under test",
            "missing, empty, wrong-type, or unmounted artifacts must fail",
            "never launch the consumer's full local application",
            "let required pull-request ci run normally",
        ):
            self.assertIn(phrase, combined)

        self.assertIsNone(
            re.search(
                r"validate\s+(?:any\s+)?(?:existing|historical|legacy|pre[- ](?:existing|toolkit))\s+(?:records?|handoffs?)",
                combined,
            )
        )
        self.assertIn("merged claude and codex hook json", integration_normalized)
        self.assertIn("continuation and completion-audit", integration_normalized)
        for phrase in (
            "hash-check the complete managed blocks",
            "derive each command from the installed json",
            "do not search or inspect deprecated legacy handoffs",
            "active consumer-owned documentation and tests",
            "stale toolkit release tags, versions, commit pins, or assertions",
            "render and validate both record types",
            "audit record (not a handoff)",
            "url encoding",
            "no restart prompt",
            "never derive expected values from the state under test",
            "every merge-owned json fragment",
            "mount every asserted managed artifact read-only",
        ):
            self.assertIn(phrase, integration_normalized)

    def test_managed_instruction_file_references_are_installed(self) -> None:
        instructions = (ROOT / "distribution/consumer-instructions.md").read_text(
            encoding="utf-8"
        )
        installed_targets = {
            target
            for artifact in load_manifest()["artifacts"]
            for target in artifact["install"]["targets"]
        }
        file_references = set(
            re.findall(r"(?<![\w/])(?:\.?[\w-]+/)+[\w.-]+\.(?:md|py)", instructions)
        )

        self.assertTrue(file_references)
        self.assertLessEqual(file_references, installed_targets)

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

    def test_manifest_contains_the_complete_installed_layout(self) -> None:
        actual = {
            artifact["source"]: set(artifact["install"]["targets"])
            for artifact in load_manifest()["artifacts"]
        }

        self.assertEqual(actual, EXPECTED_INSTALL_TARGETS)

    def test_declared_python_launcher_is_available_at_the_minimum_version(self) -> None:
        runtime = load_manifest()["runtime"]
        self.assertEqual(
            runtime,
            {"python_command": "python", "minimum_version": "3.11"},
        )
        try:
            result = subprocess.run(
                [runtime["python_command"], "--version"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except FileNotFoundError:
            self.fail(
                "distribution prerequisite failed: `python --version` is unavailable"
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r"Python (?P<major>\d+)\.(?P<minor>\d+)", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        actual = (int(match.group("major")), int(match.group("minor")))
        minimum = tuple(int(part) for part in runtime["minimum_version"].split("."))
        self.assertGreaterEqual(actual, minimum)

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
        description = fields["description"].strip()
        self.assertTrue(description.startswith("Use when "))
        for trigger in (
            "unfinished authorized work",
            "review",
            "UAT",
            "decision",
            "highest authorized scope",
            "rollout",
            "standalone",
        ):
            self.assertIn(trigger.lower(), description.lower())
        self.assertLessEqual(len("\n".join(lines[: frontmatter_end + 1])), 1024)

        gates = {
            match.group(1)
            for line in lines
            if (match := re.fullmatch(r"## Gate: (.+)", line))
        }
        self.assertEqual(gates, REQUIRED_SKILL_GATES)
        self.assertIn("docs/agent-handoff/contract.md", text)
        self.assertIn(
            "must not reproduce the handoff document",
            " ".join(text.lower().split()),
        )
        contract = " ".join(
            (ROOT / "docs/agent-handoff/contract.md")
            .read_text(encoding="utf-8")
            .lower()
            .split()
        )
        self.assertIn("each `exact_action` item", contract)
        self.assertIn("must not be a no-action assertion", contract)

    def test_continuation_output_contract_is_a_concise_handoff_pointer(self) -> None:
        required_phrases = (
            "this session is stopped because authorized work remains",
            "what you need to do: start a new session from the continuation handoff below",
            "continue from handoff",
            "exact next action",
            "essential blockers, decisions, and validation gates",
            "must not reproduce the handoff document",
            "120 words",
        )
        for relative_path in (
            "README.md",
            "docs/agent-handoff/contract.md",
            "distribution/consumer-instructions.md",
            "skills/agent-handoff/SKILL.md",
        ):
            text = " ".join(
                (ROOT / relative_path).read_text(encoding="utf-8").lower().split()
            )
            with self.subTest(path=relative_path):
                for phrase in required_phrases:
                    self.assertIn(phrase, text)

    def test_templates_match_the_contract_section_shapes(self) -> None:
        continuation = (ROOT / "templates" / "continuation.md").read_text(
            encoding="utf-8"
        )
        audit = (ROOT / "templates" / "completion-audit.md").read_text(encoding="utf-8")

        continuation_metadata, continuation_match = template_metadata(continuation)
        self.assertEqual(
            set(continuation_metadata),
            {
                "schema_version",
                "record_type",
                "timestamp",
                "active_scopes",
                "next_session_gates",
                "verification",
                "exact_action",
                "next_session_prompt",
            },
        )
        self.assertEqual(continuation_metadata["schema_version"], 1)
        self.assertEqual(continuation_metadata["record_type"], "continuation")
        self.assertEqual(continuation_metadata["next_session_gates"], [])
        self.assertEqual(
            set(continuation_metadata["exact_action"]),
            {"action", "target", "constraints", "completion_condition"},
        )
        continuation_scope = continuation_metadata["active_scopes"][0]
        self.assertIs(continuation_scope["highest_authorized"], True)
        self.assertIs(continuation_scope["remaining_work"], True)
        self.assertEqual(
            continuation[continuation_match.end() :].lstrip().splitlines()[0],
            "# Session continuation",
        )
        self.assertEqual(level_two_headings(continuation), CONTINUATION_SECTIONS)
        prompt_section = continuation.split("## Next-session prompt\n", 1)[1].strip()
        prompt_match = re.fullmatch(
            r"```text\n(?P<prompt>.*?)\n```", prompt_section, re.DOTALL
        )
        self.assertIsNotNone(prompt_match)
        self.assertEqual(
            prompt_match.group("prompt"), continuation_metadata["next_session_prompt"]
        )

        sentinel = (
            "> Audit record — not a handoff. Do not use this file to start or "
            "continue a session."
        )
        self.assertEqual(audit.splitlines()[0], sentinel)
        self.assertTrue(
            audit.startswith(f"{sentinel}\n\n<!-- agent-handoff-metadata\n")
        )
        audit_metadata, audit_match = template_metadata(audit)
        self.assertEqual(
            set(audit_metadata),
            {
                "schema_version",
                "record_type",
                "timestamp",
                "active_scopes",
                "verification",
                "completed_scope_id",
                "authorization_basis",
            },
        )
        self.assertEqual(audit_metadata["schema_version"], 1)
        self.assertEqual(audit_metadata["record_type"], "completion-audit")
        audit_scope = audit_metadata["active_scopes"][0]
        self.assertIs(audit_scope["highest_authorized"], True)
        self.assertIs(audit_scope["remaining_work"], False)
        self.assertEqual(audit_metadata["completed_scope_id"], audit_scope["scope_id"])
        self.assertEqual(
            audit[audit_match.end() :].lstrip().splitlines()[0],
            "# Completion audit",
        )
        self.assertEqual(level_two_headings(audit), AUDIT_SECTIONS)
        self.assertNotIn("## Exact next action", audit)
        self.assertNotIn("## Next-session prompt", audit)

    def test_hook_fragments_use_the_supported_event_matrix(self) -> None:
        python_command = load_manifest()["runtime"]["python_command"]
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
            if platform == "claude":
                self.assertEqual(
                    fragment["hooks"]["SessionStart"][0]["matcher"],
                    "startup|resume|clear|compact",
                )
            self.assertEqual(fragment["hooks"]["PostToolUse"][0]["matcher"], matcher)
            for host_event, cli_event in (
                ("SessionStart", "session-start"),
                ("PostToolUse", "post-tool-use"),
            ):
                hook = fragment["hooks"][host_event][0]["hooks"][0]
                self.assertEqual(hook["type"], "command")
                self.assertEqual(
                    hook["command"],
                    f"{python_command} .agent-handoff-toolkit/runner.py hook "
                    f"--platform {platform} --event {cli_event}",
                )
                if platform == "codex":
                    self.assertEqual(hook["commandWindows"], hook["command"])

    def test_source_templates_validate_after_materializing_the_timestamp(self) -> None:
        timestamp_placeholder = "<replace with ISO-8601 timestamp including timezone>"
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory)
            install_copy_artifacts(consumer)
            for name in ("continuation.md", "completion-audit.md"):
                template = consumer / "handoffs" / "templates" / name
                materialized = consumer / "handoffs" / "fresh record Ω" / name
                materialized.parent.mkdir(parents=True, exist_ok=True)
                materialized.write_text(
                    template.read_text(encoding="utf-8").replace(
                        timestamp_placeholder, "2026-09-06T12:00:00Z"
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                result = run_installed_command(
                    "python .agent-handoff-toolkit/runner.py validate "
                    f'"{materialized.relative_to(consumer).as_posix()}"',
                    consumer,
                )
                with self.subTest(template=name):
                    self.assertEqual(result.returncode, 0, result.stderr)

                    tail = run_installed_command(
                        "python .agent-handoff-toolkit/runner.py render-tail "
                        f'"{materialized.relative_to(consumer).as_posix()}"',
                        consumer,
                    )
                    self.assertEqual(tail.returncode, 0, tail.stderr)
                    expected_path = quote(
                        materialized.resolve().as_posix(), safe="/:._-"
                    )
                    if name == "continuation.md":
                        self.assertIn("[Continuation handoff]", tail.stdout)
                        self.assertIn("```text", tail.stdout)
                        self.assertIn(expected_path, tail.stdout)
                        self.assertIn("Continue from handoff:", tail.stdout)
                        self.assertIn("Exact next action:", tail.stdout)
                        self.assertIn(
                            "Essential blockers, decisions, and validation gates:",
                            tail.stdout,
                        )
                        self.assertNotIn("<!-- agent-handoff-metadata", tail.stdout)
                        self.assertNotIn("## Objective", tail.stdout)
                    else:
                        self.assertIn("[Audit record (not a handoff)]", tail.stdout)
                        self.assertIn(expected_path, tail.stdout)
                        self.assertNotIn("```text", tail.stdout)

    def test_installed_runner_executes_explicit_commands_without_an_ambient_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory)
            install_copy_artifacts(consumer)
            fixture = ROOT / "tests" / "fixtures" / "continuation.json"
            record = consumer / "handoffs" / "current.md"
            record.parent.mkdir(parents=True, exist_ok=True)

            rendered = run_installed_command(
                "python .agent-handoff-toolkit/runner.py render "
                f"{fixture} --output {record}",
                consumer,
            )
            validated = run_installed_command(
                f"python .agent-handoff-toolkit/runner.py validate {record}",
                consumer,
            )
            tail = run_installed_command(
                f"python .agent-handoff-toolkit/runner.py render-tail {record}",
                consumer,
            )
            context_health = run_installed_command(
                "python .agent-handoff-toolkit/runner.py context-health "
                "--percent 61 --session-id distribution-test "
                "--state-dir .agent-handoff-toolkit/context-state",
                consumer,
            )

        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertIn("valid:", validated.stdout)
        self.assertEqual(tail.returncode, 0, tail.stderr)
        self.assertIn("[Continuation handoff]", tail.stdout)
        self.assertIn("Continue from handoff:", tail.stdout)
        self.assertNotIn("<!-- agent-handoff-metadata", tail.stdout)
        self.assertEqual(context_health.returncode, 0, context_health.stderr)
        self.assertIn("60%", context_health.stdout)

    def test_installed_runner_directs_managed_commands_to_release_checkout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory)
            install_copy_artifacts(consumer)

            result = run_installed_command(
                "python .agent-handoff-toolkit/runner.py install "
                "--target . --release v0.2.7 --dry-run",
                consumer,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stderr,
            "error: install and sync must be run from an agent-handoff-toolkit "
            "release checkout using distribution/runner.py\n",
        )

    def test_installed_runner_does_not_create_unmanifested_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory)
            install_copy_artifacts(consumer)
            expected = {
                Path(*PurePosixPath(target).parts)
                for artifact in load_manifest()["artifacts"]
                if artifact["install"]["mode"] == "copy"
                for target in artifact["install"]["targets"]
            }

            result = run_installed_command(
                "python .agent-handoff-toolkit/runner.py hook "
                "--platform codex --event session-start",
                consumer,
                "{}",
            )
            actual = {
                path.relative_to(consumer)
                for path in consumer.rglob("*")
                if path.is_file()
            }

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual, expected)

    def test_manifest_install_then_sync_check_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory)
            result = run_source_cli(
                "install", "--apply", target=consumer, release="v0.2.7"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            check = run_source_cli("sync", "--check", target=consumer, release="v0.2.7")
            self.assertEqual(check.returncode, 0, check.stderr)
            state = json.loads(
                (consumer / ".agent-handoff-toolkit/install-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["release"], "v0.2.7")
            self.assertEqual(state["toolkit_version"], "0.2.7")
            self.assertEqual(
                [target["target"] for target in state["targets"]],
                sorted(
                    (target["target"] for target in state["targets"]),
                    key=str.casefold,
                ),
            )

            for instruction_name in ("AGENTS.md", "CLAUDE.md"):
                instruction = (consumer / instruction_name).read_text(encoding="utf-8")
                self.assertIn("<!-- agent-handoff-toolkit:start -->", instruction)
                self.assertIn("<!-- agent-handoff-toolkit:end -->", instruction)
                self.assertIn("docs/agent-handoff/contract.md", instruction)

            template = consumer / "handoffs/templates/continuation.md"
            record = consumer / "handoffs/current.md"
            record.write_text(
                template.read_text(encoding="utf-8").replace(
                    "<replace with ISO-8601 timestamp including timezone>",
                    "2026-09-06T12:00:00Z",
                ),
                encoding="utf-8",
                newline="\n",
            )
            validated = run_installed_command(
                f"python .agent-handoff-toolkit/runner.py validate {record}", consumer
            )
            hooked = run_installed_command(
                "python .agent-handoff-toolkit/runner.py hook "
                "--platform claude --event post-tool-use",
                consumer,
                json.dumps(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": "handoffs/current.md"},
                    }
                ),
            )
            installed_source = (
                consumer / ".agent-handoff-toolkit/src/agent_handoff_toolkit"
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(hooked.returncode, 0, hooked.stderr)
            hook_output = json.loads(hooked.stdout)
            self.assertEqual(
                hook_output["hookSpecificOutput"]["hookEventName"], "PostToolUse"
            )
            self.assertIn(
                "Edited: handoffs/current.md",
                hook_output["hookSpecificOutput"]["additionalContext"],
            )
            self.assertIn(
                "Run the explicit `validate` subcommand",
                hook_output["hookSpecificOutput"]["additionalContext"],
            )
            self.assertFalse((installed_source / "installer.py").exists())
            self.assertFalse((installed_source / "manifest.py").exists())
            self.assertFalse((installed_source / "operations.py").exists())
            self.assertFalse((installed_source / "state.py").exists())

    def test_sync_upgrades_a_prior_release_without_touching_legacy_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_source = root / "v0.2.6"
            shutil.copytree(
                ROOT,
                previous_source,
                ignore=shutil.ignore_patterns(
                    ".git", "build", "dist", "*.egg-info", "__pycache__"
                ),
            )

            replacements = {
                "distribution/consumer-instructions.md": (
                    "Treat every handoff record that existed before the current pinned "
                    "release was\n  adopted in the consumer",
                    "Treat every pre-toolkit handoff",
                ),
                "src/agent_handoff_toolkit/__init__.py": ("0.2.7", "0.2.6"),
                "src/agent_handoff_toolkit/hooks.py": (
                    "deprecated legacy handoffs",
                    "deprecated pre-toolkit handoffs",
                ),
            }
            for relative, (current, previous) in replacements.items():
                path = previous_source / relative
                path.write_text(
                    path.read_text(encoding="utf-8").replace(current, previous),
                    encoding="utf-8",
                    newline="\n",
                )

            previous_manifest_path = previous_source / "distribution/manifest.json"
            previous_manifest = json.loads(
                previous_manifest_path.read_text(encoding="utf-8")
            )
            previous_manifest["toolkit_version"] = "0.2.6"
            for artifact in previous_manifest["artifacts"]:
                source = previous_source / Path(
                    *PurePosixPath(artifact["source"]).parts
                )
                artifact["sha256"] = normalized_sha256(source.read_bytes())
            previous_manifest_path.write_text(
                json.dumps(previous_manifest, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            consumer = root / "consumer"
            consumer.mkdir()
            (consumer / "AGENTS.md").write_text(
                "Project-owned rule.\n", encoding="utf-8", newline="\n"
            )
            installed = subprocess.run(
                [
                    sys.executable,
                    "distribution/runner.py",
                    "install",
                    "--target",
                    str(consumer),
                    "--release",
                    "v0.2.6",
                    "--apply",
                ],
                cwd=previous_source,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            legacy_record = consumer / "handoffs/legacy.md"
            legacy_record.parent.mkdir(exist_ok=True)
            legacy_bytes = b"opaque legacy record\r\n"
            legacy_record.write_bytes(legacy_bytes)

            upgraded = run_source_cli(
                "sync", "--apply", target=consumer, release="v0.2.7"
            )
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            current = run_source_cli(
                "sync", "--check", target=consumer, release="v0.2.7"
            )

            self.assertEqual(current.returncode, 0, current.stderr)
            self.assertIn("CURRENT", current.stdout)
            self.assertEqual(legacy_record.read_bytes(), legacy_bytes)
            self.assertIn(
                "Project-owned rule.",
                (consumer / "AGENTS.md").read_text(encoding="utf-8"),
            )
            state = json.loads(
                (consumer / ".agent-handoff-toolkit/install-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["release"], "v0.2.7")
            self.assertEqual(state["toolkit_version"], "0.2.7")

    def test_hook_fragment_commands_execute_against_the_installed_layout(self) -> None:
        payloads = {
            "claude": json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": "handoffs/current.md"},
                }
            ),
            "codex": json.dumps(
                {
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": "*** Begin Patch\n"
                        "*** Update File: handoffs/current.md\n"
                        "*** End Patch\n"
                    },
                }
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory)
            installed = run_source_cli(
                "install", "--apply", target=consumer, release="v0.2.7"
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installed_configs = {
                "claude": json.loads(
                    (consumer / ".claude" / "settings.json").read_text(encoding="utf-8")
                ),
                "codex": json.loads(
                    (consumer / ".codex" / "hooks.json").read_text(encoding="utf-8")
                ),
            }
            for platform, config in installed_configs.items():
                for event, raw in (
                    ("SessionStart", "{}"),
                    ("PostToolUse", payloads[platform]),
                ):
                    command = config["hooks"][event][0]["hooks"][0]["command"]
                    result = run_installed_command(command, consumer, raw)
                    with self.subTest(platform=platform, event=event):
                        self.assertEqual(result.returncode, 0, result.stderr)
                        output = json.loads(result.stdout)
                        self.assertEqual(
                            output["hookSpecificOutput"]["hookEventName"], event
                        )
                        self.assertIn(
                            "docs/agent-handoff/contract.md",
                            output["hookSpecificOutput"]["additionalContext"],
                        )
                        if event == "SessionStart":
                            self.assertIn(
                                "Do not search or inspect deprecated legacy handoffs.",
                                output["hookSpecificOutput"]["additionalContext"],
                            )

                malformed = run_installed_command(command, consumer, "{")
                with self.subTest(platform=platform, event="malformed"):
                    self.assertEqual(malformed.returncode, 0, malformed.stderr)
                    self.assertEqual(malformed.stdout, "")


if __name__ == "__main__":
    unittest.main()
