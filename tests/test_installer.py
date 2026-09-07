from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.manifest import (  # noqa: E402
    ManifestError,
    load_manifest,
    normalize_text,
    text_sha256,
)
from agent_handoff_toolkit.installer import (  # noqa: E402
    ApplyError,
    apply_plan,
    build_plan,
    render_plan,
)
from agent_handoff_toolkit.operations import (  # noqa: E402
    OperationConflict,
    merge_json_fragment,
    merge_managed_block,
)
from agent_handoff_toolkit.state import (  # noqa: E402
    InstalledState,
    StateError,
    TargetState,
    load_state,
    render_state,
)


class InstallerFixture:
    """Create real source and consumer repositories for installer tests."""

    root: Path

    def make_install_source(
        self,
        name: str,
        version: str,
        files: dict[str, bytes],
        artifacts: list[dict[str, object]],
    ) -> Path:
        source = self.root / name
        for relative, content in files.items():
            path = source / Path(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        payload = {
            "manifest_version": 2,
            "toolkit_version": version,
            "record_schema_version": 1,
            "runtime": {"python_command": "python", "minimum_version": "3.11"},
            "text_hash": "utf8-lf-sha256-v1",
            "artifacts": artifacts,
        }
        for artifact in artifacts:
            source_name = artifact["source"]
            assert isinstance(source_name, str)
            artifact["sha256"] = text_sha256(
                (source / Path(*source_name.split("/"))).read_bytes()
            )
        distribution = source / "distribution"
        distribution.mkdir(parents=True, exist_ok=True)
        (distribution / "manifest.json").write_text(
            json.dumps(payload), encoding="utf-8", newline="\n"
        )
        return source

    def copy_artifact_for_install(
        self, source: str = "contract.md", target: str = "docs/contract.md"
    ) -> dict[str, object]:
        return {
            "source": source,
            "sha256": "",
            "install": {
                "mode": "copy",
                "transform": "none",
                "targets": [target],
            },
        }

    def apply_test_plan(self, plan: object) -> None:
        changes = getattr(plan, "changes")
        for change in changes:
            change.target.parent.mkdir(parents=True, exist_ok=True)
            change.target.write_bytes(change.after)


class CopyPlanningTests(InstallerFixture, unittest.TestCase):
    """Copy planning protects both unowned and installed consumer files."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_v1 = self.make_install_source(
            "source-v1",
            "0.2.0",
            {"contract.md": b"contract v1\n"},
            [self.copy_artifact_for_install()],
        )
        self.source_v2 = self.make_install_source(
            "source-v2",
            "0.2.1",
            {"contract.md": b"contract v2\n"},
            [self.copy_artifact_for_install()],
        )

    def make_consumer(self) -> Path:
        consumer = self.root / "consumer"
        consumer.mkdir(exist_ok=True)
        return consumer

    def install_fixture(self) -> Path:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        self.assertFalse(plan.conflicts)
        self.apply_test_plan(plan)
        return consumer

    def test_install_plans_an_absent_copy_and_state_last(self) -> None:
        consumer = self.make_consumer()

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        self.assertEqual(
            [change.relative_target for change in plan.changes],
            ["docs/contract.md", ".agent-handoff-toolkit/install-state.json"],
        )
        self.assertIsNone(plan.changes[0].before)
        self.assertEqual(plan.changes[0].after, b"contract v1\n")
        self.assertFalse((consumer / "docs" / "contract.md").exists())
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )

    def test_install_adopts_an_identical_unowned_copy(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "contract.md"
        target.parent.mkdir()
        target.write_bytes(b"contract v1\n")

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        self.assertFalse(plan.conflicts)
        self.assertEqual(
            [change.relative_target for change in plan.changes],
            [".agent-handoff-toolkit/install-state.json"],
        )
        self.assertEqual(target.read_bytes(), b"contract v1\n")

    def test_install_refuses_conflicting_unowned_content(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "contract.md"
        target.parent.mkdir()
        target.write_bytes(b"consumer content\n")

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        self.assertEqual(
            [item.code for item in plan.conflicts], ["unowned-copy-conflict"]
        )
        self.assertEqual(plan.changes, ())
        self.assertIsNone(plan.state)
        self.assertEqual(target.read_bytes(), b"consumer content\n")

    def test_sync_plans_a_safe_managed_copy_update(self) -> None:
        consumer = self.install_fixture()

        plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")

        self.assertFalse(plan.conflicts)
        self.assertEqual(
            [change.relative_target for change in plan.changes],
            ["docs/contract.md", ".agent-handoff-toolkit/install-state.json"],
        )
        self.assertEqual(plan.changes[0].before, b"contract v1\n")
        self.assertEqual(plan.changes[0].after, b"contract v2\n")

    def test_sync_refuses_a_locally_modified_copy(self) -> None:
        installed = self.install_fixture()
        target = installed / "docs" / "contract.md"
        target.write_text("local edit\n", encoding="utf-8")

        plan = build_plan(self.source_v2, installed, "v0.2.1", "sync")

        self.assertEqual(
            [item.code for item in plan.conflicts], ["managed-copy-modified"]
        )
        self.assertEqual(plan.changes, ())

    def test_sync_refuses_a_missing_installed_copy(self) -> None:
        installed = self.install_fixture()
        (installed / "docs" / "contract.md").unlink()

        plan = build_plan(self.source_v2, installed, "v0.2.1", "sync")

        self.assertEqual(
            [item.code for item in plan.conflicts], ["managed-copy-missing"]
        )
        self.assertEqual(plan.changes, ())

    def test_copy_hashes_treat_crlf_and_lf_as_equivalent(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "contract.md"
        target.parent.mkdir()
        target.write_bytes(b"contract v1\r\n")

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        self.assertFalse(plan.conflicts)
        self.assertEqual(
            [change.relative_target for change in plan.changes],
            [".agent-handoff-toolkit/install-state.json"],
        )
        self.assertEqual(target.read_bytes(), b"contract v1\r\n")

    def test_source_and_target_must_not_be_the_same_repository(self) -> None:
        plan = build_plan(self.source_v1, self.source_v1, "v0.2.0", "install")

        self.assertEqual([item.code for item in plan.conflicts], ["self-install"])
        self.assertEqual(plan.changes, ())


class ManagedBlockTests(unittest.TestCase):
    """Managed blocks preserve unowned bytes and enforce exact ownership."""

    block_id = "agent-handoff-toolkit"
    start = b"<!-- agent-handoff-toolkit:start -->"
    end = b"<!-- agent-handoff-toolkit:end -->"

    def marked(self, body: bytes, newline: bytes = b"\n") -> bytes:
        normalized = normalize_text(body).replace(b"\n", newline)
        if normalized and not normalized.endswith(newline):
            normalized += newline
        return self.start + newline + normalized + self.end + newline

    def test_managed_block_creates_an_absent_file(self) -> None:
        result = merge_managed_block(None, b"Managed\n", self.block_id)

        self.assertEqual(result, self.marked(b"Managed\n"))

    def test_managed_block_appends_without_changing_lf_outside_content(self) -> None:
        before = b"# Project\n\nKeep me.\n"

        result = merge_managed_block(before, b"Managed\n", self.block_id)

        self.assertEqual(result, before + self.marked(b"Managed\n"))

    def test_managed_block_preserves_crlf_outside_the_block(self) -> None:
        before = b"# Project\r\n\r\nKeep me.\r\n"

        result = merge_managed_block(before, b"Managed\n", self.block_id)

        self.assertTrue(result.startswith(before))
        self.assertNotIn(b"Keep me.\n", result.replace(b"\r\n", b""))
        self.assertIn(b"<!-- agent-handoff-toolkit:start -->\r\n", result)
        self.assertEqual(result, before + self.marked(b"Managed\n", b"\r\n"))

    def test_equivalent_existing_block_is_adopted_byte_for_byte(self) -> None:
        current = b"Consumer\r\n" + self.marked(b"Managed\n", b"\r\n")

        result = merge_managed_block(current, b"Managed\n", self.block_id)

        self.assertIs(result, current)

    def test_sync_replaces_only_an_unchanged_owned_block(self) -> None:
        prefix = b"Consumer policy\r\n"
        suffix = b"Consumer footer\r\n"
        old = self.marked(b"Old managed policy\n", b"\r\n")
        current = prefix + old + suffix

        result = merge_managed_block(
            current,
            b"New managed policy\n",
            self.block_id,
            installed_hash=text_sha256(old),
        )

        self.assertEqual(
            result,
            prefix + self.marked(b"New managed policy\n", b"\r\n") + suffix,
        )

    def test_sync_refuses_a_locally_modified_owned_block(self) -> None:
        installed = self.marked(b"Installed\n")
        current = self.marked(b"Local edit\n")

        with self.assertRaisesRegex(OperationConflict, "locally modified"):
            merge_managed_block(
                current,
                b"Desired\n",
                self.block_id,
                installed_hash=text_sha256(installed),
            )

    def test_sync_refuses_a_missing_owned_block(self) -> None:
        with self.assertRaisesRegex(OperationConflict, "missing"):
            merge_managed_block(
                b"Consumer only\n",
                b"Desired\n",
                self.block_id,
                installed_hash=text_sha256(self.marked(b"Installed\n")),
            )

    def test_duplicate_partial_reversed_and_nested_markers_are_rejected(self) -> None:
        well_formed = self.marked(b"Managed\n")
        cases = {
            "duplicate": well_formed + well_formed,
            "partial": self.start + b"\nManaged\n",
            "reversed": self.end + b"\nManaged\n" + self.start + b"\n",
            "nested": (
                self.start
                + b"\n"
                + self.start
                + b"\nManaged\n"
                + self.end
                + b"\n"
                + self.end
                + b"\n"
            ),
        }
        for label, current in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(OperationConflict, "malformed"):
                    merge_managed_block(current, b"Managed\n", self.block_id)


class JsonMergeTests(InstallerFixture, unittest.TestCase):
    """JSON ownership advances exact fragments while preserving consumer data."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.toolkit_hook = {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [{"type": "command", "command": "python toolkit.py"}],
        }
        self.toolkit_claude_fragment = {"hooks": {"PostToolUse": [self.toolkit_hook]}}
        self.identities = {"/hooks/PostToolUse": ("matcher",)}

    def test_recursive_object_merge_preserves_consumer_keys_and_order(self) -> None:
        current = {
            "theme": "dark",
            "hooks": {"UserPromptSubmit": [{"command": "context.py"}]},
            "status": {"consumer": True},
        }
        desired = {
            "hooks": {"PostToolUse": [self.toolkit_hook]},
            "status": {"managed": True},
        }

        result = merge_json_fragment(current, desired, self.identities)

        self.assertEqual(
            result,
            {
                "theme": "dark",
                "hooks": {
                    "UserPromptSubmit": [{"command": "context.py"}],
                    "PostToolUse": [self.toolkit_hook],
                },
                "status": {"consumer": True, "managed": True},
            },
        )
        self.assertEqual(list(result), ["theme", "hooks", "status"])

    def test_array_entries_append_after_unrelated_entries(self) -> None:
        consumer_hook = {"matcher": "Read", "hooks": [{"command": "audit"}]}
        current = {"hooks": {"PostToolUse": [consumer_hook]}}

        result = merge_json_fragment(
            current, self.toolkit_claude_fragment, self.identities
        )

        self.assertEqual(
            result["hooks"]["PostToolUse"], [consumer_hook, self.toolkit_hook]
        )

    def test_exact_existing_entry_is_adopted_without_duplication(self) -> None:
        current = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Read", "hooks": [{"command": "audit"}]},
                    self.toolkit_hook,
                ]
            }
        }

        result = merge_json_fragment(
            current, self.toolkit_claude_fragment, self.identities
        )

        self.assertEqual(result, current)
        self.assertEqual(result["hooks"]["PostToolUse"].count(self.toolkit_hook), 1)

    def test_same_matcher_with_different_hook_is_a_collision(self) -> None:
        current = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit",
                        "hooks": [{"command": "legacy"}],
                    }
                ]
            }
        }

        with self.assertRaisesRegex(OperationConflict, "same identity"):
            merge_json_fragment(
                current,
                self.toolkit_claude_fragment,
                identities=self.identities,
            )

    def test_sync_advances_an_exact_owned_entry_in_place(self) -> None:
        consumer_hook = {"matcher": "Read", "hooks": [{"command": "audit"}]}
        old_hook = {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [{"command": "python old.py"}],
        }
        current = {
            "theme": "consumer-edited",
            "hooks": {"PostToolUse": [consumer_hook, old_hook]},
        }
        owned = {"hooks": {"PostToolUse": [old_hook]}}

        result = merge_json_fragment(
            current,
            self.toolkit_claude_fragment,
            identities=self.identities,
            owned=owned,
        )

        self.assertEqual(result["theme"], "consumer-edited")
        self.assertEqual(
            result["hooks"]["PostToolUse"], [consumer_hook, self.toolkit_hook]
        )

    def test_sync_accepts_an_entry_that_already_equals_the_desired_fragment(
        self,
    ) -> None:
        old_hook = {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [{"command": "python old.py"}],
        }
        current = {
            "consumer": {"keep": True},
            "hooks": {"PostToolUse": [self.toolkit_hook]},
        }
        owned = {"hooks": {"PostToolUse": [old_hook]}}

        result = merge_json_fragment(
            current,
            self.toolkit_claude_fragment,
            identities=self.identities,
            owned=owned,
        )

        self.assertEqual(result, current)

    def test_sync_refuses_missing_or_locally_edited_owned_entries(self) -> None:
        old_hook = {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [{"command": "python old.py"}],
        }
        owned = {"hooks": {"PostToolUse": [old_hook]}}
        cases = {
            "missing": {"hooks": {"PostToolUse": []}},
            "modified": {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write|Edit|MultiEdit",
                            "hooks": [{"command": "local edit"}],
                        }
                    ]
                }
            },
        }
        for label, current in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    OperationConflict, "missing|locally modified"
                ):
                    merge_json_fragment(
                        current,
                        self.toolkit_claude_fragment,
                        identities=self.identities,
                        owned=owned,
                    )

    def test_sync_removes_only_an_exact_owned_entry(self) -> None:
        consumer_hook = {"matcher": "Read", "hooks": [{"command": "audit"}]}
        current = {
            "hooks": {"PostToolUse": [consumer_hook, self.toolkit_hook]},
        }

        result = merge_json_fragment(
            current,
            {"hooks": {"PostToolUse": []}},
            identities=self.identities,
            owned=self.toolkit_claude_fragment,
        )

        self.assertEqual(result["hooks"]["PostToolUse"], [consumer_hook])

    def test_sync_removal_refuses_missing_or_modified_owned_scalar_keys(self) -> None:
        owned = {"managed": "installed"}
        cases = {
            "missing": {},
            "modified": {"managed": "local edit"},
        }
        for label, current in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    OperationConflict, "missing|locally modified"
                ):
                    merge_json_fragment(current, {}, identities={}, owned=owned)

    def test_sync_removal_refuses_missing_or_modified_owned_object_keys(self) -> None:
        owned = {"managed": {"value": "installed"}}
        cases = {
            "missing": {},
            "modified": {"managed": {"value": "local edit"}},
        }
        for label, current in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    OperationConflict, "missing|locally modified"
                ):
                    merge_json_fragment(current, {}, identities={}, owned=owned)

    def test_sync_removal_refuses_missing_or_modified_owned_identity_entries(
        self,
    ) -> None:
        owned_hook = {"matcher": "Write", "command": "installed"}
        owned = {"hooks": [owned_hook]}
        cases = {
            "missing": {"hooks": []},
            "modified": {"hooks": [{"matcher": "Write", "command": "local edit"}]},
        }
        for label, current in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    OperationConflict, "missing|locally modified"
                ):
                    merge_json_fragment(
                        current,
                        {"hooks": []},
                        identities={"/hooks": ("matcher",)},
                        owned=owned,
                    )

    def test_sync_removal_refuses_missing_or_modified_owned_positional_entries(
        self,
    ) -> None:
        owned = {"commands": ["installed"]}
        cases = {
            "missing": {"commands": []},
            "modified": {"commands": ["local edit"]},
        }
        for label, current in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    OperationConflict, "missing|locally modified"
                ):
                    merge_json_fragment(
                        current,
                        {"commands": []},
                        identities={},
                        owned=owned,
                    )

    def test_object_roots_are_required(self) -> None:
        for current, desired in (([], {}), ({}, [])):
            with self.subTest(current=current, desired=desired):
                with self.assertRaisesRegex(OperationConflict, "JSON object"):
                    merge_json_fragment(current, desired, identities={})

    def test_build_plan_rejects_malformed_target_json(self) -> None:
        artifact = {
            "source": "settings.fragment.json",
            "sha256": "",
            "install": {
                "mode": "merge-json",
                "transform": "none",
                "targets": [".claude/settings.json"],
            },
        }
        source = self.make_install_source(
            "json-source",
            "0.2.0",
            {"settings.fragment.json": b'{"hooks": {}}\n'},
            [artifact],
        )
        consumer = self.root / "consumer"
        target = consumer / ".claude" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"{not JSON\n")

        plan = build_plan(source, consumer, "v0.2.0", "install")

        self.assertEqual([item.code for item in plan.conflicts], ["invalid-json"])
        self.assertEqual(plan.changes, ())

    def test_build_plan_serializes_json_with_existing_crlf_convention(self) -> None:
        artifact = {
            "source": "settings.fragment.json",
            "sha256": "",
            "install": {
                "mode": "merge-json",
                "transform": "none",
                "targets": [".claude/settings.json"],
            },
            "array_identities": [
                {"pointer": "/hooks/PostToolUse", "fields": ["matcher"]}
            ],
        }
        source = self.make_install_source(
            "json-source",
            "0.2.0",
            {
                "settings.fragment.json": json.dumps(
                    self.toolkit_claude_fragment
                ).encode()
                + b"\n"
            },
            [artifact],
        )
        consumer = self.root / "consumer"
        target = consumer / ".claude" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b'{\r\n  "consumer": true\r\n}\r\n')

        plan = build_plan(source, consumer, "v0.2.0", "install")

        self.assertFalse(plan.conflicts)
        change = next(
            item
            for item in plan.changes
            if item.relative_target.endswith("settings.json")
        )
        self.assertIn(b'  "consumer": true,\r\n', change.after)
        self.assertNotIn(b"\n", change.after.replace(b"\r\n", b""))


class PlanTests(InstallerFixture, unittest.TestCase):
    """Whole plans are deterministic, reviewable, and all-or-nothing."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_v1 = self.make_full_source("full-v1", "0.2.0", "v1")
        self.source_v2 = self.make_full_source("full-v2", "0.2.1", "v2")

    def full_artifacts(self) -> list[dict[str, object]]:
        return [
            self.copy_artifact_for_install("contract.md", "docs/contract.md"),
            {
                "source": "policy.md",
                "sha256": "",
                "install": {
                    "mode": "managed-block",
                    "transform": "none",
                    "targets": ["CLAUDE.md", "AGENTS.md"],
                },
                "block_id": "agent-handoff-toolkit",
            },
            {
                "source": "settings.fragment.json",
                "sha256": "",
                "install": {
                    "mode": "merge-json",
                    "transform": "none",
                    "targets": [".claude/settings.json"],
                },
                "array_identities": [
                    {"pointer": "/hooks/PostToolUse", "fields": ["matcher"]}
                ],
            },
        ]

    def make_full_source(self, name: str, version: str, label: str) -> Path:
        fragment = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python toolkit-{label}.py",
                            }
                        ],
                    }
                ]
            }
        }
        return self.make_install_source(
            name,
            version,
            {
                "contract.md": f"contract {label}\n".encode(),
                "policy.md": f"## Shared policy\n\n{label}\n".encode(),
                "settings.fragment.json": (
                    json.dumps(fragment, indent=2) + "\n"
                ).encode(),
            },
            self.full_artifacts(),
        )

    def make_consumer(self) -> Path:
        consumer = self.root / "consumer"
        consumer.mkdir(exist_ok=True)
        return consumer

    def install_full_fixture(self) -> Path:
        consumer = self.make_consumer()
        (consumer / "AGENTS.md").write_bytes(b"Consumer AGENTS\r\n")
        settings = consumer / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps({"theme": "dark"}, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        historical = consumer / "handoffs" / "2025-legacy.md"
        historical.parent.mkdir()
        historical.write_bytes(b"historical evidence\n")
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        self.assertFalse(plan.conflicts)
        self.apply_test_plan(plan)
        self.assertEqual(historical.read_bytes(), b"historical evidence\n")
        return consumer

    def test_install_and_sync_require_the_expected_state_precondition(self) -> None:
        consumer = self.install_full_fixture()

        install_plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        missing_state_consumer = self.root / "uninstalled"
        missing_state_consumer.mkdir()
        sync_plan = build_plan(self.source_v1, missing_state_consumer, "v0.2.0", "sync")

        self.assertEqual(
            [item.code for item in install_plan.conflicts], ["already-installed"]
        )
        self.assertEqual(install_plan.changes, ())
        self.assertEqual([item.code for item in sync_plan.conflicts], ["not-installed"])
        self.assertEqual(sync_plan.changes, ())

    def test_plan_uses_the_exact_state_bytes_that_were_parsed(self) -> None:
        consumer = self.install_full_fixture()
        state_path = consumer / ".agent-handoff-toolkit" / "install-state.json"
        original_bytes = state_path.read_bytes()
        rewritten_bytes = json.dumps(
            json.loads(original_bytes), separators=(",", ":")
        ).encode("utf-8")

        from agent_handoff_toolkit import state as state_module

        original_loads = state_module.strict_json_loads
        state_was_rewritten = False

        def rewrite_after_read(text: str) -> object:
            nonlocal state_was_rewritten
            if not state_was_rewritten:
                state_was_rewritten = True
                state_path.write_bytes(rewritten_bytes)
            return original_loads(text)

        with mock.patch.object(
            state_module, "strict_json_loads", side_effect=rewrite_after_read
        ):
            plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")

        self.assertFalse(plan.conflicts)
        state_change = next(
            change
            for change in plan.changes
            if change.relative_target == ".agent-handoff-toolkit/install-state.json"
        )
        self.assertEqual(state_change.before, original_bytes)
        self.assertEqual(state_path.read_bytes(), rewritten_bytes)

    def test_dry_run_rejects_an_existing_non_directory_parent(self) -> None:
        consumer = self.make_consumer()
        (consumer / "docs").write_bytes(b"not a directory\n")

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        self.assertIn("target-parent-not-directory", [c.code for c in plan.conflicts])
        self.assertEqual(plan.changes, ())
        self.assertIsNone(plan.state)

    def test_sync_rejects_previously_managed_targets_omitted_from_manifest(
        self,
    ) -> None:
        consumer = self.install_full_fixture()
        reduced_source = self.make_install_source(
            "reduced-v2",
            "0.2.1",
            {"contract.md": b"contract v2\n"},
            [self.copy_artifact_for_install("contract.md", "docs/contract.md")],
        )

        plan = build_plan(reduced_source, consumer, "v0.2.1", "sync")

        self.assertEqual(
            {conflict.target for conflict in plan.conflicts},
            {".claude/settings.json", "AGENTS.md", "CLAUDE.md"},
        )
        self.assertEqual(
            {conflict.code for conflict in plan.conflicts},
            {"managed-target-omitted"},
        )
        self.assertEqual(plan.changes, ())
        self.assertIsNone(plan.state)

    def test_release_must_match_the_source_manifest(self) -> None:
        consumer = self.make_consumer()

        plan = build_plan(self.source_v1, consumer, "v0.2.1", "install")

        self.assertEqual([item.code for item in plan.conflicts], ["release-mismatch"])
        self.assertEqual(plan.changes, ())

    def test_state_is_planned_last_after_deterministically_sorted_targets(self) -> None:
        consumer = self.make_consumer()

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        relative_targets = [change.relative_target for change in plan.changes]
        self.assertEqual(
            relative_targets[:-1], sorted(relative_targets[:-1], key=str.casefold)
        )
        self.assertEqual(
            relative_targets[-1], ".agent-handoff-toolkit/install-state.json"
        )
        assert plan.state is not None
        state_targets = [target.target for target in plan.state.targets]
        self.assertEqual(state_targets, sorted(state_targets, key=str.casefold))

    def test_conflict_suppresses_every_planned_write(self) -> None:
        source = self.make_install_source(
            "conflict-source",
            "0.2.0",
            {"a.md": b"managed a\n", "b.md": b"managed b\n"},
            [
                self.copy_artifact_for_install("a.md", "docs/a.md"),
                self.copy_artifact_for_install("b.md", "docs/b.md"),
            ],
        )
        consumer = self.make_consumer()
        first = consumer / "docs" / "a.md"
        first.parent.mkdir()
        first.write_bytes(b"consumer a\n")

        plan = build_plan(source, consumer, "v0.2.0", "install")

        self.assertTrue(plan.conflicts)
        self.assertEqual(plan.changes, ())
        self.assertEqual(first.read_bytes(), b"consumer a\n")
        self.assertFalse((consumer / "docs" / "b.md").exists())
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )

    def test_manifest_artifacts_cannot_claim_the_install_state_path(self) -> None:
        source = self.make_install_source(
            "reserved-target-source",
            "0.2.0",
            {"state.txt": b"not installer state\n"},
            [
                self.copy_artifact_for_install(
                    "state.txt", ".agent-handoff-toolkit/install-state.json"
                )
            ],
        )
        consumer = self.make_consumer()

        plan = build_plan(source, consumer, "v0.2.0", "install")

        self.assertEqual(
            [item.code for item in plan.conflicts], ["reserved-state-target"]
        )
        self.assertEqual(plan.changes, ())

    def test_sync_preserves_unowned_block_json_and_historical_content(self) -> None:
        consumer = self.install_full_fixture()
        agents = consumer / "AGENTS.md"
        agents.write_bytes(agents.read_bytes() + b"Consumer suffix\r\n")
        settings = consumer / ".claude" / "settings.json"
        payload = json.loads(settings.read_text(encoding="utf-8"))
        payload["consumer_after_install"] = {"keep": True}
        settings.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")

        self.assertFalse(plan.conflicts)
        agents_after = next(
            change.after
            for change in plan.changes
            if change.relative_target == "AGENTS.md"
        )
        self.assertTrue(agents_after.startswith(b"Consumer AGENTS\r\n"))
        self.assertTrue(agents_after.endswith(b"Consumer suffix\r\n"))
        settings_after = next(
            change.after
            for change in plan.changes
            if change.relative_target == ".claude/settings.json"
        )
        self.assertTrue(json.loads(settings_after)["consumer_after_install"]["keep"])
        self.assertEqual(
            (consumer / "handoffs" / "2025-legacy.md").read_bytes(),
            b"historical evidence\n",
        )

    def test_state_directory_junction_escape_returns_a_conflict_only_plan(self) -> None:
        consumer = self.make_consumer()
        outside = self.root / "outside-state"
        outside.mkdir()
        state_directory = consumer / ".agent-handoff-toolkit"
        try:
            state_directory.symlink_to(outside, target_is_directory=True)
            self.addCleanup(state_directory.unlink)
        except OSError:
            junction = subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(state_directory),
                    str(outside),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("symlink and junction creation unavailable")
            self.addCleanup(
                lambda: subprocess.run(
                    ["cmd", "/c", "rmdir", str(state_directory)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
            )

        try:
            plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        except StateError as error:
            self.fail(f"build_plan leaked a state containment error: {error}")

        self.assertEqual(
            [(conflict.target, conflict.code) for conflict in plan.conflicts],
            [
                (
                    ".agent-handoff-toolkit/install-state.json",
                    "state-path-escape",
                )
            ],
        )
        self.assertEqual(plan.changes, ())
        self.assertIsNone(plan.state)
        self.assertEqual(list(outside.iterdir()), [])

    def test_target_junction_escape_is_rejected_without_writing_outside(self) -> None:
        consumer = self.make_consumer()
        outside = self.root / "outside"
        outside.mkdir()
        linked = consumer / "docs"
        try:
            linked.symlink_to(outside, target_is_directory=True)
            self.addCleanup(linked.unlink)
        except OSError:
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("symlink and junction creation unavailable")
            self.addCleanup(
                lambda: subprocess.run(
                    ["cmd", "/c", "rmdir", str(linked)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
            )

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        self.assertIn("target-escape", [item.code for item in plan.conflicts])
        self.assertEqual(plan.changes, ())
        self.assertEqual(list(outside.iterdir()), [])

    def test_plan_models_are_immutable(self) -> None:
        plan = build_plan(self.source_v1, self.make_consumer(), "v0.2.0", "install")

        with self.assertRaises(FrozenInstanceError):
            plan.release = "v9.9.9"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            plan.changes[0].after = b"changed"  # type: ignore[misc]

    def test_render_plan_uses_concise_relative_status_and_diff_headers(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        rendered = render_plan(plan)

        self.assertTrue(rendered.startswith("install: v0.2.0\n"))
        self.assertIn("CHANGE docs/contract.md\n", rendered)
        self.assertIn("--- a/docs/contract.md\n", rendered)
        self.assertIn("+++ b/docs/contract.md\n", rendered)
        self.assertNotIn(str(consumer), rendered)

    def test_render_plan_omits_unchanged_consumer_json_values(self) -> None:
        artifact = {
            "source": "settings.fragment.json",
            "sha256": "",
            "install": {
                "mode": "merge-json",
                "transform": "none",
                "targets": [".claude/settings.json"],
            },
        }
        source = self.make_install_source(
            "private-json-source",
            "0.2.0",
            {"settings.fragment.json": b'{"managed": true}\n'},
            [artifact],
        )
        consumer = self.make_consumer()
        target = consumer / ".claude" / "settings.json"
        target.parent.mkdir()
        target.write_bytes(b'{"consumer_note":"private consumer value"}\n')
        plan = build_plan(source, consumer, "v0.2.0", "install")

        rendered = render_plan(plan)

        self.assertIn("--- a/.claude/settings.json\n", rendered)
        self.assertIn("+++ b/.claude/settings.json\n", rendered)
        self.assertNotIn("private consumer value", rendered)

    def test_render_plan_reports_conflicts_without_a_diff(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "contract.md"
        target.parent.mkdir()
        target.write_bytes(b"consumer\n")
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        rendered = render_plan(plan)

        self.assertIn("CONFLICT docs/contract.md: unowned-copy-conflict:", rendered)
        self.assertNotIn("CHANGE ", rendered)
        self.assertNotIn("--- ", rendered)

    def test_render_plan_reports_current_after_an_unchanged_sync(self) -> None:
        consumer = self.install_full_fixture()

        plan = build_plan(self.source_v1, consumer, "v0.2.0", "sync")

        self.assertEqual(plan.changes, ())
        self.assertEqual(render_plan(plan), "sync: v0.2.0\nCURRENT\n")


class ApplyTests(InstallerFixture, unittest.TestCase):
    """Atomic application preserves planned bytes or restores them on failure."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_v1 = self.make_install_source(
            "source-v1",
            "0.2.0",
            {"first.md": b"first v1\n", "second.md": b"second v1\n"},
            [
                self.copy_artifact_for_install("first.md", "docs/first.md"),
                self.copy_artifact_for_install("second.md", "docs/second.md"),
            ],
        )
        self.source_v2 = self.make_install_source(
            "source-v2",
            "0.2.1",
            {"first.md": b"first v2\n", "second.md": b"second v2\n"},
            [
                self.copy_artifact_for_install("first.md", "docs/first.md"),
                self.copy_artifact_for_install("second.md", "docs/second.md"),
            ],
        )

    def make_consumer(self) -> Path:
        consumer = self.root / "consumer"
        consumer.mkdir()
        return consumer

    def link_directory(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("symlink and junction creation unavailable")
            self.addCleanup(
                lambda: subprocess.run(
                    ["cmd", "/c", "rmdir", str(link)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
            )
        else:
            self.addCleanup(link.unlink)

    def install_v1(self, consumer: Path) -> None:
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        self.assertFalse(plan.conflicts)
        apply_plan(plan)

    def test_apply_creates_parent_files_and_state_last(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        writes: list[str] = []

        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def record_write(target: Path, content: bytes, target_root: Path) -> None:
            writes.append(target.relative_to(consumer).as_posix())
            original(target, content, target_root)

        with mock.patch.object(installer, "_atomic_replace", side_effect=record_write):
            apply_plan(plan)

        self.assertEqual((consumer / "docs" / "first.md").read_bytes(), b"first v1\n")
        self.assertEqual((consumer / "docs" / "second.md").read_bytes(), b"second v1\n")
        self.assertTrue(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").is_file()
        )
        self.assertEqual(writes[-1], ".agent-handoff-toolkit/install-state.json")
        self.assertEqual(list(consumer.rglob("*.agent-handoff-tmp-*")), [])

    def test_apply_replaces_existing_managed_files_atomically(self) -> None:
        consumer = self.make_consumer()
        self.install_v1(consumer)
        plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")

        apply_plan(plan)

        self.assertEqual((consumer / "docs" / "first.md").read_bytes(), b"first v2\n")
        self.assertEqual((consumer / "docs" / "second.md").read_bytes(), b"second v2\n")
        self.assertEqual(list(consumer.rglob("*.agent-handoff-tmp-*")), [])

    def test_apply_refuses_when_target_changed_after_planning(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        target = consumer / "docs" / "second.md"
        target.parent.mkdir()
        target.write_text("concurrent edit\n", encoding="utf-8")

        with self.assertRaisesRegex(ApplyError, "changed after planning"):
            apply_plan(plan)

        self.assertEqual(target.read_text(encoding="utf-8"), "concurrent edit\n")
        self.assertFalse((consumer / "docs" / "first.md").exists())
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )

    def test_apply_rechecks_each_target_immediately_before_its_write(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        second = consumer / "docs" / "second.md"

        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def edit_second_after_first(
            target: Path, content: bytes, target_root: Path
        ) -> None:
            original(target, content, target_root)
            if target.relative_to(consumer).as_posix() == "docs/first.md":
                second.write_bytes(b"concurrent edit\n")

        with mock.patch.object(
            installer, "_atomic_replace", side_effect=edit_second_after_first
        ):
            with self.assertRaisesRegex(
                ApplyError, "second.md: changed after planning"
            ):
                apply_plan(plan)

        self.assertFalse((consumer / "docs" / "first.md").exists())
        self.assertEqual(second.read_bytes(), b"concurrent edit\n")

    def test_rollback_preserves_bytes_that_no_longer_match_this_run(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        first = consumer / "docs" / "first.md"

        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def edit_first_then_fail_second(
            target: Path, content: bytes, target_root: Path
        ) -> None:
            relative = target.relative_to(consumer).as_posix()
            if relative == "docs/second.md":
                raise OSError("later write failure")
            original(target, content, target_root)
            if relative == "docs/first.md":
                first.write_bytes(b"concurrent edit\n")

        with mock.patch.object(
            installer, "_atomic_replace", side_effect=edit_first_then_fail_second
        ):
            with self.assertRaisesRegex(
                ApplyError,
                "later write failure.*concurrent change preserved",
            ):
                apply_plan(plan)

        self.assertEqual(first.read_bytes(), b"concurrent edit\n")
        self.assertFalse((consumer / "docs" / "second.md").exists())

    def test_apply_refuses_parent_link_substituted_after_planning(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        outside = self.root / "outside"
        outside.mkdir()
        self.link_directory(consumer / "docs", outside)

        with self.assertRaisesRegex(ApplyError, "outside the target root"):
            apply_plan(plan)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )

    def test_apply_binds_parent_across_final_replace(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        outside = self.root / "outside-final-replace"
        outside.mkdir()
        held = self.root / "held-docs"
        substitution_errors: list[OSError] = []

        from agent_handoff_toolkit import installer

        original_replace = installer.os.replace
        substituted = False

        def substitute_parent_then_replace(
            source: str | Path, destination: str | Path, *args: object, **kwargs: object
        ) -> None:
            nonlocal substituted
            if not substituted:
                substituted = True
                try:
                    original_replace(consumer / "docs", held)
                except OSError as error:
                    substitution_errors.append(error)
                else:
                    self.link_directory(consumer / "docs", outside)
                    self.addCleanup(held.rmdir)
            original_replace(source, destination, *args, **kwargs)

        with mock.patch.object(
            installer.os, "replace", side_effect=substitute_parent_then_replace
        ):
            if os.name == "nt":
                apply_plan(plan)
            else:
                with self.assertRaisesRegex(
                    ApplyError, "target parent changed|outside the target root"
                ):
                    apply_plan(plan)

        self.assertTrue(substituted)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(held.glob("*.agent-handoff-tmp-*")), [])
        if os.name == "nt":
            self.assertTrue(substitution_errors)
            self.assertEqual(
                (consumer / "docs" / "first.md").read_bytes(), b"first v1\n"
            )
        else:
            self.assertFalse((held / "first.md").exists())

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_apply_restores_existing_bytes_after_parent_rename_during_replace(
        self,
    ) -> None:
        consumer = self.make_consumer()
        self.install_v1(consumer)
        plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")
        outside = self.root / "outside-existing-race"
        outside.mkdir()
        held = self.root / "held-existing-race"
        observed: list[bytes] = []

        from agent_handoff_toolkit import installer

        original_replace = installer.os.replace
        substituted = False

        def rename_parent_during_replace(
            source: str | Path, destination: str | Path, *args: object, **kwargs: object
        ) -> None:
            nonlocal substituted
            if not substituted and destination == "first.md":
                substituted = True
                original_replace(consumer / "docs", held)
                self.link_directory(consumer / "docs", outside)
                original_replace(source, destination, *args, **kwargs)
                observed.append((held / "first.md").read_bytes())
                return
            original_replace(source, destination, *args, **kwargs)

        with mock.patch.object(
            installer.os, "replace", side_effect=rename_parent_during_replace
        ):
            with self.assertRaisesRegex(
                ApplyError, "target parent changed|outside the target root"
            ):
                apply_plan(plan)

        self.assertTrue(substituted)
        self.assertEqual(observed, [b"first v2\n"])
        self.assertEqual((held / "first.md").read_bytes(), b"first v1\n")
        self.assertEqual(list(held.glob("*.agent-handoff-tmp-*")), [])
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_apply_reports_posix_primary_and_recovery_failure_then_retries_rollback(
        self,
    ) -> None:
        consumer = self.make_consumer()
        self.install_v1(consumer)
        plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")
        target = consumer / "docs" / "first.md"

        from agent_handoff_toolkit import installer

        original_verify = installer._verify_bound_parent
        original_restore = installer._restore_posix_bound_file
        containment_failed = False
        recovery_failed = False

        def fail_post_replace_verification(bound: object) -> None:
            nonlocal containment_failed
            original_verify(bound)
            if (
                not containment_failed
                and bound.target == target
                and target.read_bytes() == b"first v2\n"
            ):
                containment_failed = True
                raise ApplyError("primary containment failure")

        def fail_first_recovery(
            bound: object, before: bytes | None, directory_fd: int
        ) -> None:
            nonlocal recovery_failed
            if not recovery_failed:
                recovery_failed = True
                raise OSError("recovery restore failure")
            original_restore(bound, before, directory_fd)

        with (
            mock.patch.object(
                installer,
                "_verify_bound_parent",
                side_effect=fail_post_replace_verification,
            ),
            mock.patch.object(
                installer,
                "_restore_posix_bound_file",
                side_effect=fail_first_recovery,
            ),
        ):
            with self.assertRaises(ApplyError) as raised:
                apply_plan(plan)

        self.assertTrue(containment_failed)
        self.assertTrue(recovery_failed)
        self.assertRegex(
            str(raised.exception),
            "primary containment failure.*recovery restore failure",
        )
        self.assertEqual(target.read_bytes(), b"first v1\n")
        self.assertEqual(list(consumer.rglob("*.agent-handoff-tmp-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX mkdir mode behavior")
    def test_apply_creates_parent_with_mkdir_default_mode_subject_to_umask(
        self,
    ) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        previous_umask = os.umask(0o027)
        try:
            apply_plan(plan)
        finally:
            os.umask(previous_umask)

        mode = stat.S_IMODE((consumer / "docs").stat().st_mode)
        self.assertEqual(mode, 0o750)

    @unittest.skipIf(os.name == "nt", "POSIX file mode behavior")
    def test_apply_uses_umask_for_new_files(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        previous_umask = os.umask(0o027)
        try:
            apply_plan(plan)
        finally:
            os.umask(previous_umask)

        self.assertEqual(
            stat.S_IMODE((consumer / "docs" / "first.md").stat().st_mode),
            0o640,
        )
        self.assertEqual(
            stat.S_IMODE(
                (consumer / ".agent-handoff-toolkit" / "install-state.json")
                .stat()
                .st_mode
            ),
            0o640,
        )

    @unittest.skipIf(os.name == "nt", "POSIX file mode behavior")
    def test_apply_preserves_existing_file_mode_on_replacement(self) -> None:
        consumer = self.make_consumer()
        self.install_v1(consumer)
        target = consumer / "docs" / "first.md"
        target.chmod(0o751)
        plan = build_plan(self.source_v2, consumer, "v0.2.1", "sync")

        apply_plan(plan)

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o751)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_posix_failed_remove_restores_name_without_temp_residue(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "first.md"
        target.parent.mkdir()
        target.write_bytes(b"original bytes\n")

        from agent_handoff_toolkit import installer

        original_verify = installer._verify_bound_parent
        failed_after_move = False

        def fail_after_original_moves(bound: object) -> None:
            nonlocal failed_after_move
            original_verify(bound)
            if (
                not failed_after_move
                and bound.target == target
                and not target.exists()
                and list(target.parent.glob("*.agent-handoff-tmp-*"))
            ):
                failed_after_move = True
                raise ApplyError("post-remove containment failure")

        with mock.patch.object(
            installer, "_verify_bound_parent", side_effect=fail_after_original_moves
        ):
            with self.assertRaisesRegex(ApplyError, "post-remove containment failure"):
                installer._atomic_remove(target, consumer)

        self.assertTrue(failed_after_move)
        self.assertEqual(target.read_bytes(), b"original bytes\n")
        self.assertEqual(list(target.parent.glob("*.agent-handoff-tmp-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_posix_remove_reports_recovery_error_and_preserves_original(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "first.md"
        target.parent.mkdir()
        target.write_bytes(b"original bytes\n")

        from agent_handoff_toolkit import installer

        original_verify = installer._verify_bound_parent
        original_replace = installer.os.replace
        primary_failed = False
        recovery_failed = False

        def fail_after_original_moves(bound: object) -> None:
            nonlocal primary_failed
            original_verify(bound)
            if (
                not primary_failed
                and bound.target == target
                and not target.exists()
                and list(target.parent.glob("*.agent-handoff-tmp-*"))
            ):
                primary_failed = True
                raise ApplyError("post-remove containment failure")

        def fail_first_recovery_replace(
            source: str | Path, destination: str | Path, *args: object, **kwargs: object
        ) -> None:
            nonlocal recovery_failed
            if primary_failed and not recovery_failed and destination == target.name:
                recovery_failed = True
                raise OSError("remove recovery replace failure")
            original_replace(source, destination, *args, **kwargs)

        with (
            mock.patch.object(
                installer, "_verify_bound_parent", side_effect=fail_after_original_moves
            ),
            mock.patch.object(
                installer.os, "replace", side_effect=fail_first_recovery_replace
            ),
        ):
            with self.assertRaises(ApplyError) as raised:
                installer._atomic_remove(target, consumer)

        self.assertTrue(primary_failed)
        self.assertTrue(recovery_failed)
        self.assertRegex(
            str(raised.exception),
            "post-remove containment failure.*remove recovery replace failure",
        )
        self.assertEqual(target.read_bytes(), b"original bytes\n")
        self.assertEqual(list(target.parent.glob("*.agent-handoff-tmp-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_posix_restore_reports_publish_and_temp_cleanup_failures(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "first.md"
        target.parent.mkdir()
        target.write_bytes(b"current bytes\n")

        from agent_handoff_toolkit import installer

        bound = installer._open_bound_parent(target, consumer)
        directory_fd = installer._posix_operation_parent_fd(bound)
        try:
            with (
                mock.patch.object(
                    installer.os,
                    "replace",
                    side_effect=OSError("restore publish failure"),
                ),
                mock.patch.object(
                    installer.os,
                    "unlink",
                    side_effect=OSError("restore temp cleanup failure"),
                ),
            ):
                with self.assertRaises(ApplyError) as raised:
                    installer._replace_posix_bound_file(
                        bound, b"original bytes\n", directory_fd
                    )
        finally:
            installer.os.close(directory_fd)
            bound.close()

        self.assertRegex(
            str(raised.exception),
            "restore publish failure.*restore temp cleanup failure",
        )

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_posix_prewrite_failure_reports_temp_cleanup_failure(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")
        target = consumer / "docs" / "first.md"

        from agent_handoff_toolkit import installer

        original_verify = installer._verify_bound_parent
        original_unlink = installer.os.unlink
        verification_failed = False
        cleanup_failed = False

        def fail_after_temp_creation(bound: object) -> None:
            nonlocal verification_failed
            original_verify(bound)
            if (
                not verification_failed
                and bound.target == target
                and list(target.parent.glob("*.agent-handoff-tmp-*"))
            ):
                verification_failed = True
                raise ApplyError("prewrite containment failure")

        def fail_first_temp_cleanup(
            path: str | Path, *args: object, **kwargs: object
        ) -> None:
            nonlocal cleanup_failed
            if not cleanup_failed and ".agent-handoff-tmp-" in str(path):
                cleanup_failed = True
                raise OSError("prewrite temp cleanup failure")
            original_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(
                installer, "_verify_bound_parent", side_effect=fail_after_temp_creation
            ),
            mock.patch.object(
                installer.os, "unlink", side_effect=fail_first_temp_cleanup
            ),
        ):
            with self.assertRaises(ApplyError) as raised:
                apply_plan(plan)

        self.assertTrue(verification_failed)
        self.assertTrue(cleanup_failed)
        self.assertRegex(
            str(raised.exception),
            "prewrite containment failure.*prewrite temp cleanup failure",
        )

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_apply_rolls_back_posix_mutation_when_operation_fd_close_fails(
        self,
    ) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        from agent_handoff_toolkit import installer

        original_operation_fd = installer._posix_operation_parent_fd
        original_close = installer.os.close
        operation_fds: set[int] = set()
        close_failed = False

        def capture_operation_fd(bound: object) -> int:
            descriptor = original_operation_fd(bound)
            operation_fds.add(descriptor)
            return descriptor

        def close_then_fail(descriptor: int) -> None:
            nonlocal close_failed
            original_close(descriptor)
            if descriptor in operation_fds and not close_failed:
                close_failed = True
                raise OSError("operation descriptor close failure")

        with (
            mock.patch.object(
                installer,
                "_posix_operation_parent_fd",
                side_effect=capture_operation_fd,
            ),
            mock.patch.object(installer.os, "close", side_effect=close_then_fail),
        ):
            with self.assertRaisesRegex(
                ApplyError, "operation descriptor close failure"
            ):
                apply_plan(plan)

        self.assertTrue(close_failed)
        self.assertFalse((consumer / "docs" / "first.md").exists())
        self.assertFalse((consumer / "docs" / "second.md").exists())
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )

    @unittest.skipIf(os.name == "nt", "POSIX descriptor behavior")
    def test_posix_remove_reports_primary_and_operation_fd_release_failure(
        self,
    ) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "first.md"
        target.parent.mkdir()
        target.write_bytes(b"original bytes\n")

        from agent_handoff_toolkit import installer

        original_verify = installer._verify_bound_parent
        original_operation_fd = installer._posix_operation_parent_fd
        original_close = installer.os.close
        operation_fds: set[int] = set()
        primary_failed = False
        close_failed = False

        def fail_after_original_moves(bound: object) -> None:
            nonlocal primary_failed
            original_verify(bound)
            if (
                not primary_failed
                and bound.target == target
                and not target.exists()
                and list(target.parent.glob("*.agent-handoff-tmp-*"))
            ):
                primary_failed = True
                raise ApplyError("post-remove containment failure")

        def capture_operation_fd(bound: object) -> int:
            descriptor = original_operation_fd(bound)
            operation_fds.add(descriptor)
            return descriptor

        def close_then_fail(descriptor: int) -> None:
            nonlocal close_failed
            original_close(descriptor)
            if descriptor in operation_fds and not close_failed:
                close_failed = True
                raise OSError("remove operation descriptor close failure")

        with (
            mock.patch.object(
                installer, "_verify_bound_parent", side_effect=fail_after_original_moves
            ),
            mock.patch.object(
                installer,
                "_posix_operation_parent_fd",
                side_effect=capture_operation_fd,
            ),
            mock.patch.object(installer.os, "close", side_effect=close_then_fail),
        ):
            with self.assertRaises(ApplyError) as raised:
                installer._atomic_remove(target, consumer)

        self.assertTrue(primary_failed)
        self.assertTrue(close_failed)
        self.assertRegex(
            str(raised.exception),
            "post-remove containment failure.*remove operation descriptor close failure",
        )
        self.assertEqual(target.read_bytes(), b"original bytes\n")
        self.assertEqual(list(target.parent.glob("*.agent-handoff-tmp-*")), [])

    def test_windows_handle_cleanup_reports_close_failure_after_guard_cleanup(
        self,
    ) -> None:
        guard = self.root / "windows-lock-guard"
        guard.write_bytes(b"")

        from agent_handoff_toolkit import installer

        with mock.patch.object(
            installer,
            "_windows_close_handle",
            side_effect=(OSError("first close failed"), OSError("second close failed")),
        ) as close_handle:
            with self.assertRaises(ApplyError) as raised:
                installer._windows_release_resources((101, 102), (guard,))

        self.assertEqual(close_handle.call_args_list, [mock.call(102), mock.call(101)])
        self.assertFalse(guard.exists())
        self.assertRegex(
            str(raised.exception), "first close failed.*second close failed"
        )

    def test_bound_parent_close_attempts_and_reports_every_descriptor(self) -> None:
        from agent_handoff_toolkit import installer

        bound = installer._BoundParent(
            self.root / "target",
            self.root,
            "target",
            self.root,
            directory_fd=303,
            posix_directory_fds=(101, 202, 303),
        )
        close_errors = (
            OSError("close 303 failed"),
            OSError("close 202 failed"),
            None,
        )

        with mock.patch.object(
            installer.os, "close", side_effect=close_errors
        ) as close:
            with self.assertRaises(ApplyError) as raised:
                bound.close()

        self.assertEqual(
            close.call_args_list, [mock.call(303), mock.call(202), mock.call(101)]
        )
        message = str(raised.exception)
        self.assertRegex(message, "close 303 failed.*close 202 failed")

    def test_windows_kernel32_declarations_use_last_error_and_pointer_handles(
        self,
    ) -> None:
        from agent_handoff_toolkit import installer

        kernel32 = mock.MagicMock()
        with mock.patch.object(
            installer.ctypes, "WinDLL", return_value=kernel32, create=True
        ) as win_dll:
            self.assertIs(installer._kernel32(), kernel32)

        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        self.assertEqual(kernel32.CreateFileW.argtypes[6], installer.ctypes.c_void_p)
        self.assertEqual(
            kernel32.GetFinalPathNameByHandleW.argtypes[0], installer.ctypes.c_void_p
        )
        self.assertEqual(kernel32.CloseHandle.argtypes, (installer.ctypes.c_void_p,))
        self.assertEqual(kernel32.CloseHandle.restype, installer.ctypes.c_int)

    def test_windows_final_path_normalizes_unc_device_prefix(self) -> None:
        from agent_handoff_toolkit import installer

        device_path = r"\\?\UNC\server\share\folder"
        kernel32 = mock.MagicMock()

        def return_final_path(
            handle: object, buffer: object, length: int, flags: int
        ) -> int:
            del handle, length, flags
            if buffer is None:
                return len(device_path)
            buffer.value = device_path
            return len(device_path)

        kernel32.GetFinalPathNameByHandleW.side_effect = return_final_path
        with mock.patch.object(installer, "_kernel32", return_value=kernel32):
            actual = installer._windows_final_path(101)

        self.assertEqual(str(actual), r"\\server\share\folder")

    @unittest.skipUnless(os.name == "nt", "Windows handle-release behavior")
    def test_apply_rolls_back_windows_mutation_when_bound_release_fails(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        from agent_handoff_toolkit import installer

        original_release = installer._windows_release_resources
        release_failed = False

        def release_then_fail(
            handles: tuple[int, ...], guards: tuple[Path, ...]
        ) -> None:
            nonlocal release_failed
            original_release(handles, guards)
            if not release_failed:
                release_failed = True
                raise ApplyError("simulated Windows release failure")

        with mock.patch.object(
            installer, "_windows_release_resources", side_effect=release_then_fail
        ):
            with self.assertRaisesRegex(
                ApplyError, "simulated Windows release failure"
            ):
                apply_plan(plan)

        self.assertTrue(release_failed)
        self.assertFalse((consumer / "docs" / "first.md").exists())
        self.assertFalse((consumer / "docs" / "second.md").exists())
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )
        self.assertEqual(list(consumer.rglob("*.agent-handoff-tmp-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows handle-release behavior")
    def test_apply_reports_primary_release_and_rollback_errors_in_order(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        from agent_handoff_toolkit import installer

        original_replace = installer._atomic_replace_windows
        original_release = installer._windows_release_resources
        release_stage: str | None = None

        def fail_second_write(bound: object, content: bytes) -> None:
            nonlocal release_stage
            if bound.target.relative_to(consumer).as_posix() == "docs/second.md":
                release_stage = "apply"
                raise OSError("primary apply failure")
            original_replace(bound, content)

        def fail_rollback_remove(bound: object) -> None:
            nonlocal release_stage
            release_stage = "rollback"
            raise OSError("rollback mutation failure")

        def release_then_fail(
            handles: tuple[int, ...], guards: tuple[Path, ...]
        ) -> None:
            nonlocal release_stage
            original_release(handles, guards)
            if release_stage is not None:
                stage = release_stage
                release_stage = None
                raise ApplyError(f"{stage} release failure")

        with (
            mock.patch.object(
                installer, "_atomic_replace_windows", side_effect=fail_second_write
            ),
            mock.patch.object(
                installer, "_atomic_remove_windows", side_effect=fail_rollback_remove
            ),
            mock.patch.object(
                installer,
                "_windows_release_resources",
                side_effect=release_then_fail,
            ),
        ):
            with self.assertRaises(ApplyError) as raised:
                apply_plan(plan)

        message = str(raised.exception)
        expected = (
            "primary apply failure",
            "apply release failure",
            "rollback mutation failure",
            "rollback release failure",
        )
        positions = tuple(message.find(fragment) for fragment in expected)
        self.assertTrue(all(position >= 0 for position in positions), message)
        self.assertEqual(positions, tuple(sorted(positions)), message)

    def test_apply_rolls_back_created_and_replaced_files_after_write_failure(
        self,
    ) -> None:
        consumer = self.make_consumer()
        initial = self.make_install_source(
            "rollback-v1",
            "0.2.0",
            {"first.md": b"first v1\n"},
            [self.copy_artifact_for_install("first.md", "docs/first.md")],
        )
        updated = self.make_install_source(
            "rollback-v2",
            "0.2.1",
            {"first.md": b"first v2\n", "second.md": b"second v2\n"},
            [
                self.copy_artifact_for_install("first.md", "docs/first.md"),
                self.copy_artifact_for_install("second.md", "docs/second.md"),
            ],
        )
        apply_plan(build_plan(initial, consumer, "v0.2.0", "install"))
        existing = consumer / "docs" / "first.md"
        state_path = consumer / ".agent-handoff-toolkit" / "install-state.json"
        state_before = state_path.read_bytes()
        plan = build_plan(updated, consumer, "v0.2.1", "sync")
        self.assertFalse(plan.conflicts)

        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def fail_second_target(target: Path, content: bytes, target_root: Path) -> None:
            if target.relative_to(consumer).as_posix() == "docs/second.md":
                raise OSError("simulated write failure")
            original(target, content, target_root)

        with mock.patch.object(
            installer, "_atomic_replace", side_effect=fail_second_target
        ):
            with self.assertRaisesRegex(ApplyError, "simulated write failure"):
                apply_plan(plan)

        self.assertEqual(existing.read_bytes(), b"first v1\n")
        self.assertFalse((consumer / "docs" / "second.md").exists())
        self.assertEqual(state_path.read_bytes(), state_before)
        self.assertEqual(list(consumer.rglob("*.agent-handoff-tmp-*")), [])

    def test_apply_removes_successfully_created_target_during_rollback(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def fail_after_first_creation(
            target: Path, content: bytes, target_root: Path
        ) -> None:
            if target.relative_to(consumer).as_posix() == "docs/second.md":
                raise OSError("later write failure")
            original(target, content, target_root)

        with mock.patch.object(
            installer, "_atomic_replace", side_effect=fail_after_first_creation
        ):
            with self.assertRaisesRegex(ApplyError, "later write failure"):
                apply_plan(plan)

        self.assertFalse((consumer / "docs" / "first.md").exists())
        self.assertFalse((consumer / "docs" / "second.md").exists())
        self.assertFalse(
            (consumer / ".agent-handoff-toolkit" / "install-state.json").exists()
        )

    def test_apply_removes_temp_file_after_post_temp_replace_failure(self) -> None:
        consumer = self.make_consumer()
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        from agent_handoff_toolkit import installer

        with mock.patch.object(
            installer.os, "replace", side_effect=OSError("replace failure")
        ):
            with self.assertRaisesRegex(ApplyError, "replace failure"):
                apply_plan(plan)

        self.assertFalse((consumer / "docs" / "first.md").exists())
        self.assertEqual(list(consumer.rglob("*.agent-handoff-tmp-*")), [])

    def test_apply_reports_primary_and_rollback_failure(self) -> None:
        consumer = self.make_consumer()
        initial = self.make_install_source(
            "rollback-error-v1",
            "0.2.0",
            {"first.md": b"first v1\n"},
            [self.copy_artifact_for_install("first.md", "docs/first.md")],
        )
        updated = self.make_install_source(
            "rollback-error-v2",
            "0.2.1",
            {"first.md": b"first v2\n", "second.md": b"second v2\n"},
            [
                self.copy_artifact_for_install("first.md", "docs/first.md"),
                self.copy_artifact_for_install("second.md", "docs/second.md"),
            ],
        )
        apply_plan(build_plan(initial, consumer, "v0.2.0", "install"))
        plan = build_plan(updated, consumer, "v0.2.1", "sync")

        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def fail_apply_and_rollback(
            target: Path, content: bytes, target_root: Path
        ) -> None:
            relative = target.relative_to(consumer).as_posix()
            if relative == "docs/second.md":
                raise OSError("primary failure")
            if relative == "docs/first.md" and content == b"first v1\n":
                raise OSError("rollback failure")
            original(target, content, target_root)

        with mock.patch.object(
            installer, "_atomic_replace", side_effect=fail_apply_and_rollback
        ):
            with self.assertRaisesRegex(
                ApplyError, "primary failure.*rollback failure"
            ):
                apply_plan(plan)

    def test_apply_rejects_conflicted_plans_without_writing(self) -> None:
        consumer = self.make_consumer()
        target = consumer / "docs" / "first.md"
        target.parent.mkdir()
        target.write_bytes(b"consumer\n")
        plan = build_plan(self.source_v1, consumer, "v0.2.0", "install")

        with self.assertRaisesRegex(ApplyError, "conflicts"):
            apply_plan(plan)

        self.assertEqual(target.read_bytes(), b"consumer\n")


class CliTests(InstallerFixture, unittest.TestCase):
    """The command surface renders plans before applying their exact changes."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_v1 = self.make_cli_source("cli-v1", "0.2.0", "v1")
        self.source_v2 = self.make_cli_source("cli-v2", "0.2.1", "v2")
        self.consumer = self.root / "consumer"
        self.consumer.mkdir()

    def make_cli_source(self, name: str, version: str, label: str) -> Path:
        source = self.make_install_source(
            name,
            version,
            {"contract.md": f"contract {label}\n".encode()},
            [self.copy_artifact_for_install()],
        )
        shutil.copytree(ROOT / "src", source / "src")
        return source

    def run_cli(
        self,
        command: str,
        *mode: str,
        release: str,
        source: Path | None,
        package_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            sys.executable,
            "-m",
            "agent_handoff_toolkit",
            command,
            "--target",
            str(self.consumer),
            "--release",
            release,
            *mode,
        ]
        if source is not None:
            arguments.extend(("--source-root", str(source)))
        package_root = (
            package_root
            if package_root is not None
            else self.source_v1
            if source is None
            else ROOT
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(package_root / "src")
        return subprocess.run(
            arguments,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )

    def test_install_requires_exactly_one_mode(self) -> None:
        result = self.run_cli("install", release="v0.2.0", source=self.source_v1)

        self.assertEqual(result.returncode, 2, result.stderr)
        both_modes = self.run_cli(
            "install",
            "--dry-run",
            "--apply",
            release="v0.2.0",
            source=self.source_v1,
        )
        self.assertEqual(both_modes.returncode, 2, both_modes.stderr)
        self.assertFalse((self.consumer / ".agent-handoff-toolkit").exists())

    def test_install_dry_run_prints_plan_without_writing(self) -> None:
        result = self.run_cli(
            "install", "--dry-run", release="v0.2.0", source=self.source_v1
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CHANGE docs/contract.md", result.stdout)
        self.assertFalse((self.consumer / "docs" / "contract.md").exists())
        self.assertFalse((self.consumer / ".agent-handoff-toolkit").exists())

    def test_install_apply_creates_state_after_printing_plan(self) -> None:
        result = self.run_cli(
            "install", "--apply", release="v0.2.0", source=self.source_v1
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CHANGE docs/contract.md", result.stdout)
        self.assertEqual(
            (self.consumer / "docs" / "contract.md").read_bytes(), b"contract v1\n"
        )
        self.assertTrue(
            (self.consumer / ".agent-handoff-toolkit" / "install-state.json").is_file()
        )

    def test_sync_check_returns_one_for_safe_available_changes(self) -> None:
        installed = self.run_cli(
            "install", "--apply", release="v0.2.0", source=self.source_v1
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        result = self.run_cli(
            "sync", "--check", release="v0.2.1", source=self.source_v2
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("CHANGE docs/contract.md", result.stdout)
        self.assertEqual(
            (self.consumer / "docs" / "contract.md").read_bytes(), b"contract v1\n"
        )

    def test_sync_check_returns_zero_when_current(self) -> None:
        installed = self.run_cli(
            "install", "--apply", release="v0.2.0", source=self.source_v1
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        result = self.run_cli(
            "sync", "--check", release="v0.2.0", source=self.source_v1
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CURRENT", result.stdout)

    def test_sync_apply_writes_safe_available_changes(self) -> None:
        installed = self.run_cli(
            "install", "--apply", release="v0.2.0", source=self.source_v1
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)

        result = self.run_cli(
            "sync", "--apply", release="v0.2.1", source=self.source_v2
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CHANGE docs/contract.md", result.stdout)
        self.assertEqual(
            (self.consumer / "docs" / "contract.md").read_bytes(), b"contract v2\n"
        )

    def test_cli_returns_two_for_release_mismatch_and_conflicts(self) -> None:
        mismatch = self.run_cli(
            "install", "--dry-run", release="v9.9.9", source=self.source_v1
        )
        self.assertEqual(mismatch.returncode, 2, mismatch.stderr)
        self.assertIn("release-mismatch", mismatch.stdout)

        target = self.consumer / "docs" / "contract.md"
        target.parent.mkdir()
        target.write_bytes(b"consumer content\n")
        conflict = self.run_cli(
            "install", "--dry-run", release="v0.2.0", source=self.source_v1
        )
        self.assertEqual(conflict.returncode, 2, conflict.stderr)
        self.assertIn("unowned-copy-conflict", conflict.stdout)
        self.assertEqual(target.read_bytes(), b"consumer content\n")

    def test_managed_command_maps_broken_installer_imports_to_exit_two(self) -> None:
        for name, replacement in (("missing", None), ("corrupt", b"not valid Python")):
            with self.subTest(name=name):
                package_root = self.root / f"broken-package-{name}"
                shutil.copytree(ROOT / "src", package_root / "src")
                installer = (
                    package_root / "src" / "agent_handoff_toolkit" / "installer.py"
                )
                if replacement is None:
                    installer.unlink()
                else:
                    installer.write_bytes(replacement)

                result = self.run_cli(
                    "install",
                    "--dry-run",
                    release="v0.2.0",
                    source=self.source_v1,
                    package_root=package_root,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_default_source_root_is_inferred_from_the_source_checkout(self) -> None:
        result = self.run_cli(
            "install",
            "--dry-run",
            release="v0.2.0",
            source=None,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CHANGE docs/contract.md", result.stdout)
        self.assertFalse((self.consumer / "docs" / "contract.md").exists())


class ManifestTests(unittest.TestCase):
    """Regression tests for source manifest validation failures."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "a.md").write_bytes(b"alpha\n")
        (self.root / "A.md").write_bytes(b"alpha uppercase\n")
        (self.root / "b.md").write_bytes(b"bravo\n")

    def copy_artifact(self, source: str, target: str) -> dict[str, object]:
        return {
            "source": source,
            "sha256": text_sha256((self.root / source).read_bytes()),
            "install": {
                "mode": "copy",
                "transform": "none",
                "targets": [target],
            },
        }

    def make_source(
        self, artifacts: list[dict[str, object]] | None = None, **overrides: object
    ) -> Path:
        manifest: dict[str, object] = {
            "manifest_version": 2,
            "toolkit_version": "0.2.0",
            "record_schema_version": 1,
            "runtime": {"python_command": "python", "minimum_version": "3.11"},
            "text_hash": "utf8-lf-sha256-v1",
            "artifacts": artifacts or [self.copy_artifact("a.md", "docs/a.md")],
        }
        manifest.update(overrides)
        distribution = self.root / "distribution"
        distribution.mkdir(exist_ok=True)
        (distribution / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8", newline="\n"
        )
        return self.root

    def test_text_hash_normalizes_all_line_endings(self) -> None:
        expected = hashlib.sha256(b"one\ntwo\n").hexdigest()
        self.assertEqual(text_sha256(b"one\r\ntwo\r"), expected)
        self.assertEqual(normalize_text(b"one\r\ntwo\r"), b"one\ntwo\n")

    def test_text_hash_rejects_malformed_utf8(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            text_sha256(b"\xff")

    def test_load_manifest_requires_version_two_and_derives_release(self) -> None:
        root = self.make_source()
        manifest = load_manifest(root)
        self.assertEqual(manifest.toolkit_version, "0.2.0")
        self.assertEqual(manifest.release, "v0.2.0")
        self.assertEqual(manifest.python_command, "python")
        self.assertEqual(manifest.minimum_python, "3.11")

        self.make_source(manifest_version=1)
        with self.assertRaisesRegex(ManifestError, "manifest version"):
            load_manifest(root)

    def test_load_manifest_rejects_source_hash_mismatch(self) -> None:
        root = self.make_source()
        manifest_path = root / "distribution" / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["artifacts"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

        with self.assertRaisesRegex(ManifestError, "source hash"):
            load_manifest(root)

    def test_duplicate_targets_are_rejected_case_insensitively(self) -> None:
        root = self.make_source(
            artifacts=[
                self.copy_artifact("a.md", "Docs/File.md"),
                self.copy_artifact("b.md", "docs/file.md"),
            ]
        )
        with self.assertRaisesRegex(ManifestError, "target collision"):
            load_manifest(root)

    def test_duplicate_sources_are_rejected_case_insensitively(self) -> None:
        root = self.make_source(
            artifacts=[
                self.copy_artifact("a.md", "docs/a.md"),
                self.copy_artifact("A.md", "docs/b.md"),
            ]
        )
        with self.assertRaisesRegex(ManifestError, "source collision"):
            load_manifest(root)

    def test_unsafe_source_and_target_paths_are_rejected(self) -> None:
        cases = (
            ("../a.md", "docs/a.md", "unsafe source path"),
            ("a.md", "../docs/a.md", "unsafe target path"),
            ("a.md", "/docs/a.md", "unsafe target path"),
            ("a.md", "docs//a.md", "unsafe target path"),
        )
        for source, target, message in cases:
            with self.subTest(source=source, target=target):
                artifact = self.copy_artifact("a.md", target)
                artifact["source"] = source
                root = self.make_source(artifacts=[artifact])
                with self.assertRaisesRegex(ManifestError, message):
                    load_manifest(root)

    def test_windows_unsafe_target_filename_forms_are_rejected(self) -> None:
        unsafe_targets = (
            "docs/file.txt:owned",
            "docs/NUL",
            "docs/nul.txt",
            "docs/CON.json",
            "docs/PRN.md",
            "docs/AUX",
            "docs/COM1",
            "docs/LPT9.log",
            "docs/bad<name>.md",
            'docs/bad"name.md',
            "docs/bad|name.md",
            "docs/bad?name.md",
            "docs/bad*name.md",
            "docs/control\nname.md",
            "docs/name.",
            "docs/name ",
        )
        for target in unsafe_targets:
            with self.subTest(target=repr(target)):
                root = self.make_source(artifacts=[self.copy_artifact("a.md", target)])
                with self.assertRaisesRegex(ManifestError, "unsafe target path"):
                    load_manifest(root)

    def test_trailing_dot_alias_is_rejected_before_collision_planning(self) -> None:
        root = self.make_source(
            artifacts=[
                self.copy_artifact("a.md", "docs/name"),
                self.copy_artifact("b.md", "docs/name."),
            ]
        )

        with self.assertRaisesRegex(ManifestError, "unsafe target path"):
            load_manifest(root)

    def test_unknown_mode_transform_or_hash_strategy_is_rejected(self) -> None:
        for update, message in (
            ({"text_hash": "future-hash"}, "text hash"),
            ({"mode": "replace-everything"}, "mode"),
            ({"transform": "magic"}, "transform"),
        ):
            with self.subTest(update=update):
                artifact = self.copy_artifact("a.md", "docs/a.md")
                install = artifact["install"]
                assert isinstance(install, dict)
                for key in ("mode", "transform"):
                    if key in update:
                        install[key] = update[key]
                root = self.make_source(
                    artifacts=[artifact],
                    **(
                        {"text_hash": update["text_hash"]}
                        if "text_hash" in update
                        else {}
                    ),
                )
                with self.assertRaisesRegex(ManifestError, message):
                    load_manifest(root)

    def test_source_symlink_escape_is_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.md"
        outside.write_text("outside\n", encoding="utf-8", newline="\n")
        self.addCleanup(outside.unlink)
        link = self.root / "escaped.md"
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        artifact = self.copy_artifact("a.md", "docs/a.md")
        artifact["source"] = "escaped.md"
        artifact["sha256"] = text_sha256(outside.read_bytes())
        root = self.make_source(artifacts=[artifact])
        with self.assertRaisesRegex(ManifestError, "source escapes"):
            load_manifest(root)

    def test_manifest_rejects_duplicate_keys_and_non_finite_json(self) -> None:
        root = self.make_source()
        manifest_path = root / "distribution" / "manifest.json"
        payload = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(
            payload.replace(
                '"toolkit_version": "0.2.0",',
                '"toolkit_version": "0.2.0", "toolkit_version": "0.2.0",',
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ManifestError, "duplicate JSON key"):
            load_manifest(root)

        manifest_path.write_text(
            payload.replace(
                '"record_schema_version": 1', '"record_schema_version": NaN'
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ManifestError, "non-finite JSON constant"):
            load_manifest(root)


class StateTests(unittest.TestCase):
    """Regression tests for deterministic installed-state parsing and output."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write_state(self, payload: object) -> Path:
        state_path = self.root / ".agent-handoff-toolkit" / "install-state.json"
        state_path.parent.mkdir(exist_ok=True)
        state_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        return state_path

    def valid_payload(self) -> dict[str, object]:
        return {
            "state_version": 1,
            "release": "v0.2.0",
            "toolkit_version": "0.2.0",
            "record_schema_version": 1,
            "targets": [
                {
                    "target": "docs/a.md",
                    "source": "docs/a.md",
                    "mode": "copy",
                    "installed_sha256": "a" * 64,
                }
            ],
        }

    def test_load_state_returns_none_when_state_is_missing(self) -> None:
        self.assertIsNone(load_state(self.root))

    def test_load_state_rejects_malformed_or_unknown_state_versions(self) -> None:
        state_path = self.root / ".agent-handoff-toolkit" / "install-state.json"
        state_path.parent.mkdir()
        state_path.write_text("{", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(StateError, "invalid state JSON"):
            load_state(self.root)

        payload = self.valid_payload()
        payload["state_version"] = 2
        self.write_state(payload)
        with self.assertRaisesRegex(StateError, "state version"):
            load_state(self.root)

    def test_load_state_rejects_duplicate_or_unsafe_target_entries(self) -> None:
        payload = self.valid_payload()
        payload["targets"] = [payload["targets"][0], payload["targets"][0]]
        self.write_state(payload)
        with self.assertRaisesRegex(StateError, "target collision"):
            load_state(self.root)

        payload = self.valid_payload()
        target = payload["targets"][0]
        assert isinstance(target, dict)
        target["target"] = "../escaped.md"
        self.write_state(payload)
        with self.assertRaisesRegex(StateError, "unsafe target path"):
            load_state(self.root)

    def test_load_state_rejects_duplicate_keys_and_non_finite_json(self) -> None:
        payload = json.dumps(self.valid_payload())
        state_path = self.root / ".agent-handoff-toolkit" / "install-state.json"
        state_path.parent.mkdir()
        state_path.write_text(
            payload.replace(
                '"toolkit_version": "0.2.0",',
                '"toolkit_version": "0.2.0", "toolkit_version": "0.2.0",',
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(StateError, "duplicate JSON key"):
            load_state(self.root)

        state_path.write_text(
            payload.replace(
                '"record_schema_version": 1', '"record_schema_version": NaN'
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(StateError, "non-finite JSON constant"):
            load_state(self.root)

    def test_load_state_rejects_state_file_symlink_or_junction_escape(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        linked_directory = self.root / ".agent-handoff-toolkit"
        try:
            linked_directory.symlink_to(outside, target_is_directory=True)
        except OSError:
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked_directory), str(outside)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            if junction.returncode != 0:
                self.skipTest("symlink and junction creation unavailable")
            self.addCleanup(
                lambda: subprocess.run(
                    ["cmd", "/c", "rmdir", str(linked_directory)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
            )
        (outside / "install-state.json").write_text(
            json.dumps(self.valid_payload()), encoding="utf-8", newline="\n"
        )
        self.addCleanup(lambda: (outside / "install-state.json").unlink())

        with self.assertRaisesRegex(StateError, "state path escapes"):
            load_state(self.root)

    def test_load_state_round_trips_json_fragments_and_validates_hashes(self) -> None:
        payload = self.valid_payload()
        target = payload["targets"][0]
        assert isinstance(target, dict)
        target["mode"] = "merge-json"
        target["owned_fragment"] = {"hooks": [{"matcher": "Write"}]}
        self.write_state(payload)
        state = load_state(self.root)
        assert state is not None
        self.assertEqual(
            state.targets[0].owned_fragment, {"hooks": [{"matcher": "Write"}]}
        )

        target["installed_sha256"] = "A" * 64
        self.write_state(payload)
        with self.assertRaisesRegex(StateError, "sha256"):
            load_state(self.root)

    def test_render_state_is_deterministic(self) -> None:
        state = InstalledState(
            state_version=1,
            release="v0.2.0",
            toolkit_version="0.2.0",
            record_schema_version=1,
            targets=(
                TargetState("z.md", "z.md", "copy", "f" * 64),
                TargetState("a.md", "a.md", "copy", "e" * 64),
            ),
        )
        rendered = render_state(state)
        self.assertEqual(rendered, render_state(state))
        self.assertLess(rendered.index(b'"a.md"'), rendered.index(b'"z.md"'))
        self.assertNotIn(b"installed_at", rendered)
        self.assertNotIn(str(self.root).encode(), rendered)
        self.assertTrue(rendered.endswith(b"\n"))

    def test_merge_json_owned_fragment_must_be_an_object(self) -> None:
        for fragment in (None, [], "fragment", 1):
            with self.subTest(fragment=fragment):
                state = InstalledState(
                    state_version=1,
                    release="v0.2.0",
                    toolkit_version="0.2.0",
                    record_schema_version=1,
                    targets=(
                        TargetState(
                            "settings.json",
                            "fragment.json",
                            "merge-json",
                            "d" * 64,
                            fragment,
                        ),
                    ),
                )
                with self.assertRaisesRegex(StateError, "owned fragment.*object"):
                    render_state(state)

                payload = self.valid_payload()
                target = payload["targets"][0]
                assert isinstance(target, dict)
                target["mode"] = "merge-json"
                target["owned_fragment"] = fragment
                self.write_state(payload)
                with self.assertRaisesRegex(StateError, "owned fragment.*object"):
                    load_state(self.root)

    def test_render_state_rejects_invalid_contract_values(self) -> None:
        valid_target = TargetState("a.md", "a.md", "copy", "a" * 64)
        cases = (
            (
                InstalledState(
                    1,
                    "v0.2.0",
                    "0.2.0",
                    1,
                    (TargetState("../a.md", "a.md", "copy", "a" * 64),),
                ),
                "unsafe target path",
            ),
            (
                InstalledState(
                    1,
                    "v0.2.0",
                    "0.2.0",
                    1,
                    (TargetState("a.md", "/a.md", "copy", "a" * 64),),
                ),
                "unsafe source path",
            ),
            (
                InstalledState(
                    1,
                    "v0.2.0",
                    "0.2.0",
                    1,
                    (TargetState("a.md", "a.md", "copy", "A" * 64),),
                ),
                "sha256",
            ),
            (
                InstalledState(
                    1,
                    "v0.2.0",
                    "0.2.0",
                    1,
                    (TargetState("a.md", "a.md", "unknown", "a" * 64),),
                ),
                "unknown mode",
            ),
            (InstalledState(2, "v0.2.0", "0.2.0", 1, (valid_target,)), "state version"),
            (InstalledState(1, "v0.2.1", "0.2.0", 1, (valid_target,)), "release"),
            (
                InstalledState(
                    1,
                    "v0.2.0",
                    "0.2.0",
                    1,
                    (
                        valid_target,
                        TargetState("A.md", "A.md", "copy", "b" * 64),
                    ),
                ),
                "target collision",
            ),
        )
        for state, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(StateError, message):
                    render_state(state)

    def test_render_state_rejects_non_finite_json(self) -> None:
        state = InstalledState(
            state_version=1,
            release="v0.2.0",
            toolkit_version="0.2.0",
            record_schema_version=1,
            targets=(
                TargetState(
                    "settings.json",
                    "fragment.json",
                    "merge-json",
                    "d" * 64,
                    {"limit": float("nan")},
                ),
            ),
        )
        with self.assertRaises(ValueError):
            render_state(state)


if __name__ == "__main__":
    unittest.main()
