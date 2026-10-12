"""Explicit retirement of unchanged owned copies; no unowned deletion."""

import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import zipfile
from unittest.mock import patch

from agent_handoff_toolkit.installer import build_plan, apply_plan, ApplyError
from agent_handoff_toolkit.state import load_state
from test_installer import InstallerFixture


class RetirementTests(InstallerFixture, unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows deletion boundary")
    def test_windows_retirement_unlink_failure_restores_original(self):
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        original = Path.unlink
        failed = False

        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path.name.startswith(".old.py.agent-handoff-tmp-") and not failed:
                failed = True
                raise PermissionError("synthetic deletion failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_once), self.assertRaises(ApplyError):
            apply_plan(plan)
        self.assertTrue(failed)
        self.assertEqual((self.consumer / self.retired).read_bytes(), b"old\n")
        self.assertEqual(load_state(self.consumer).release, "v1.3.1")

    def test_authentic_old_release_upgrade_removes_provider_runtime(self):
        source = Path(__file__).resolve().parents[1]
        archive = self.root / "release.zip"
        subprocess.run(
            ["git", "archive", "--format=zip", "--output", str(archive), "v1.3.1"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        old = self.root / "release"
        with zipfile.ZipFile(archive) as content:
            content.extractall(old)
        consumer = self.root / "released-consumer"
        (consumer / ".git").mkdir(parents=True)
        result = subprocess.run(
            [
                sys.executable,
                str(old / "distribution/runner.py"),
                "install",
                "--target",
                str(consumer),
                "--release",
                "v1.3.1",
                "--apply",
            ],
            cwd=old,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        retired = json.loads((source / "distribution/manifest.json").read_text())[
            "retired_targets"
        ]
        self.assertTrue(all((consumer / name).is_file() for name in retired))
        config = consumer / ".agent-handoff-toolkit/jev.json"
        config.write_bytes(b'{"enabled":false}\n')
        plan = build_plan(source, consumer, "v1.4.0", "sync")
        self.assertFalse(plan.conflicts)
        apply_plan(plan)
        self.assertTrue(all(not (consumer / name).exists() for name in retired))
        self.assertEqual(config.read_bytes(), b'{"enabled":false}\n')
        current = build_plan(source, consumer, "v1.4.0", "sync")
        self.assertFalse(current.conflicts or current.changes)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.consumer = self.root / "consumer"
        self.consumer.mkdir()
        (self.consumer / ".git").mkdir()
        self.retired = ".agent-handoff-toolkit/old.py"
        self.old = self.make_install_source(
            "old",
            "1.3.1",
            {"old.py": b"old\n", "keep.py": b"keep\n"},
            [
                self.copy_artifact_for_install("old.py", self.retired),
                self.copy_artifact_for_install(
                    "keep.py", ".agent-handoff-toolkit/keep.py"
                ),
            ],
        )
        apply_plan(build_plan(self.old, self.consumer, "v1.3.1", "install"))
        self.new = self.make_install_source(
            "new",
            "1.4.0",
            {"keep.py": b"keep\n"},
            [
                self.copy_artifact_for_install(
                    "keep.py", ".agent-handoff-toolkit/keep.py"
                )
            ],
        )
        self.manifest = self.new / "distribution/manifest.json"
        data = json.loads(self.manifest.read_text())
        data["retired_targets"] = [self.retired]
        self.manifest.write_text(json.dumps(data))

    def test_retirement_preserves_local_files_and_becomes_current(self):
        config = self.consumer / ".agent-handoff-toolkit/jev.json"
        config.write_bytes(b'{"enabled":false}\n')
        credential = self.consumer / ".env"
        credential.write_bytes(b"local file\n")
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        self.assertFalse(plan.conflicts)
        self.assertTrue((self.consumer / self.retired).exists())
        apply_plan(plan)
        self.assertFalse((self.consumer / self.retired).exists())
        self.assertEqual(config.read_bytes(), b'{"enabled":false}\n')
        self.assertEqual(credential.read_bytes(), b"local file\n")
        self.assertNotIn(
            self.retired, {t.target for t in load_state(self.consumer).targets}
        )
        current = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        self.assertFalse(current.conflicts or current.changes)

    def test_modified_retired_file_is_a_conflict(self):
        path = self.consumer / self.retired
        path.write_bytes(b"local modification\n")
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        self.assertTrue(plan.conflicts)
        self.assertFalse(plan.changes)
        self.assertEqual(path.read_bytes(), b"local modification\n")

    def test_missing_retired_file_still_releases_ownership(self):
        (self.consumer / self.retired).unlink()
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        self.assertFalse(plan.conflicts)
        apply_plan(plan)
        self.assertNotIn(
            self.retired, {t.target for t in load_state(self.consumer).targets}
        )

    def test_retirement_stale_plan_preserves_concurrent_changes(self):
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        path = self.consumer / self.retired
        path.write_bytes(b"concurrent modification\n")
        with self.assertRaises(ApplyError):
            apply_plan(plan)
        self.assertEqual(path.read_bytes(), b"concurrent modification\n")

    def test_missing_target_created_after_planning_is_preserved(self):
        path = self.consumer / self.retired
        path.unlink()
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        path.write_bytes(b"new concurrent file\n")
        with self.assertRaises(ApplyError):
            apply_plan(plan)
        self.assertEqual(path.read_bytes(), b"new concurrent file\n")

    def test_later_failure_restores_deleted_file_and_state(self):
        plan = build_plan(self.new, self.consumer, "v1.4.0", "sync")
        state = self.consumer / ".agent-handoff-toolkit/install-state.json"
        before = state.read_bytes()
        from agent_handoff_toolkit import installer

        original = installer._atomic_replace

        def fail_state(path, content, root):
            if path == state:
                raise OSError("synthetic failure")
            return original(path, content, root)

        with (
            patch.object(installer, "_atomic_replace", fail_state),
            self.assertRaises(ApplyError),
        ):
            apply_plan(plan)
        self.assertEqual((self.consumer / self.retired).read_bytes(), b"old\n")
        self.assertEqual(state.read_bytes(), before)

    def test_unowned_retired_file_is_preserved(self):
        fresh = self.root / "fresh"
        (fresh / ".git").mkdir(parents=True)
        path = fresh / self.retired
        path.parent.mkdir()
        path.write_bytes(b"unowned\n")
        plan = build_plan(self.new, fresh, "v1.4.0", "install")
        self.assertTrue(plan.conflicts)
        self.assertFalse(plan.changes)
        self.assertEqual(path.read_bytes(), b"unowned\n")

    def test_retired_targets_reject_unsafe_and_colliding_names(self):
        for targets in (
            ["../outside"],
            [self.retired, self.retired.upper()],
            [".agent-handoff-toolkit/keep.py"],
        ):
            with self.subTest(targets=targets):
                data = json.loads(self.manifest.read_text())
                data["retired_targets"] = targets
                self.manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    build_plan(self.new, self.consumer, "v1.4.0", "sync")
