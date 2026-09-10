"""Behavioral coverage for private, atomic lifecycle registry storage."""

from dataclasses import replace
import errno
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_handoff_toolkit.lifecycle import (
    ChainState,
    EnforcementMode,
    LifecycleMutation,
)
from agent_handoff_toolkit.lifecycle_storage import (
    LifecycleStorageError,
    LocalLifecycleStorage,
    RegistryEnvelope,
    StaleLifecycleState,
    resolve_lifecycle_state_root,
)


def register(storage, session, auth="auth-a", root="root-a"):
    snapshot = storage.load_snapshot(session)
    chain = ChainState(auth, root, ("a" * 64,), 1, "active", "record-1")
    return storage.compare_and_swap(
        session,
        0,
        snapshot.session.targeted_revision,
        LifecycleMutation(
            replace(
                snapshot.session,
                targeted_revision=snapshot.session.targeted_revision + 1,
                mode=EnforcementMode.TRACKED,
                authorization_id=auth,
                chain_revision=1,
            ),
            chain,
        ),
    )


def race_worker(repo, state, session, barrier, queue):
    try:
        storage = LocalLifecycleStorage(Path(repo), state_root=Path(state))
        snapshot = storage.load_snapshot(session)
        barrier.wait(timeout=20)
        storage.compare_and_swap(
            session,
            snapshot.chain.targeted_revision,
            snapshot.session.targeted_revision,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=snapshot.session.targeted_revision + 1,
                    chain_revision=snapshot.chain.targeted_revision + 1,
                ),
                replace(
                    snapshot.chain,
                    targeted_revision=snapshot.chain.targeted_revision + 1,
                    current_record_reference="record-2",
                ),
            ),
        )
        queue.put("pass")
    except StaleLifecycleState:
        queue.put("stale")
    except Exception as error:
        queue.put(type(error).__name__)


class LifecycleStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repository-private-identifier"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.storage = LocalLifecycleStorage(self.repo, state_root=self.state)

    def tearDown(self):
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(self.root))
        self.temp.cleanup()

    def test_empty_exact_canonical_round_trip(self):
        expected = b'{"chains":{},"registry_version":1,"revision":0,"sessions":{}}'
        self.assertEqual(RegistryEnvelope().to_bytes(), expected)
        self.assertEqual(RegistryEnvelope.from_bytes(expected).to_bytes(), expected)
        snapshot = self.storage.load_snapshot("private-session")
        self.assertIsNone(snapshot.chain)
        self.assertEqual(snapshot.session.mode, EnforcementMode.UNTRACKED)

    def test_rejects_corrupt_envelopes(self):
        invalid = [
            b"\xff",
            b'{"chains":{},"registry_version":1,"revision":0}',
            b'{"chains":{},"registry_version":1,"revision":0,"sessions":{},"extra":0}',
            b'{"chains":{},"registry_version":1,"revision":NaN,"sessions":{}}',
            b'{"chains":{},"registry_version":1,"revision":Infinity,"sessions":{}}',
            b'{"chains":{},"registry_version":1,"revision":0,"revision":1,"sessions":{}}',
            b" " * (1024 * 1024 + 1),
        ]
        for value in invalid:
            with (
                self.subTest(value=value[:100]),
                self.assertRaises(LifecycleStorageError),
            ):
                RegistryEnvelope.from_bytes(value)

    def test_nested_validation_and_content_field_rejection(self):
        register(self.storage, "s")
        original = json.loads(self.storage.registry_path.read_bytes())
        key = next(iter(original["sessions"]))
        alterations = [
            ("chains", "auth-a", "unknown", "sentinel-" + secrets.token_hex(8)),
            ("chains", "auth-a", "status", "invalid"),
            ("chains", "auth-a", "targeted_revision", True),
            ("chains", "auth-a", "scope_digests", ["bad"]),
            ("chains", "auth-a", "authorization_id", "different"),
            ("sessions", key, "mode", "invalid"),
            ("sessions", key, "session_key", "raw-session"),
            ("sessions", key, "tool_input", {"sentinel": secrets.token_hex(8)}),
            ("sessions", key, "current_user_message", secrets.token_hex(8)),
            ("sessions", key, "pending_decision_reference", {"question": "invalid"}),
        ]
        for group, item, field, value in alterations:
            data = json.loads(json.dumps(original))
            data[group][item][field] = value
            with self.subTest(field=field), self.assertRaises(LifecycleStorageError):
                RegistryEnvelope.from_bytes(json.dumps(data).encode())
        del original["chains"]["auth-a"]["scope_digests"]
        with self.assertRaises(LifecycleStorageError):
            RegistryEnvelope.from_bytes(json.dumps(original).encode())

    def test_atomic_publication_privacy_sorted_keys_and_no_residue(self):
        register(self.storage, "private-session-z", "auth-z", "root-z")
        register(self.storage, "private-session-a")
        data = self.storage.registry_path.read_bytes()
        envelope = RegistryEnvelope.from_bytes(data)
        self.assertEqual(envelope.revision, 2)
        self.assertEqual(data, envelope.to_bytes())
        self.assertLess(data.index(b'"auth-a":'), data.index(b'"auth-z":'))
        self.assertNotIn(b"private-session", data)
        self.assertNotIn(self.storage.secret, data)
        names = [path.name for path in self.state.iterdir()]
        self.assertEqual(
            sorted(names), ["registry.json", "registry.lock", "secret.key"]
        )
        self.assertFalse(any("private" in name for name in names))

    def test_failed_replace_and_fsync_preserve_old_envelope_and_clean_temp(self):
        snapshot = register(self.storage, "s")
        before = self.storage.registry_path.read_bytes()
        mutation = LifecycleMutation(replace(snapshot.session, targeted_revision=2))
        for operation in ("os.replace", "os.fsync"):
            with patch(
                "agent_handoff_toolkit.lifecycle_storage." + operation,
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(LifecycleStorageError):
                    self.storage.compare_and_swap("s", 1, 1, mutation)
            self.assertEqual(self.storage.registry_path.read_bytes(), before)
            self.assertFalse(list(self.state.glob("*.tmp")))

    def test_stale_target_rejected_but_unrelated_update_preserved(self):
        a = register(self.storage, "a")
        register(self.storage, "b", "auth-b", "root-b")
        mutation = LifecycleMutation(replace(a.session, targeted_revision=2))
        result = self.storage.compare_and_swap("a", 1, 1, mutation)
        self.assertEqual(result.session.targeted_revision, 2)
        with self.assertRaises(StaleLifecycleState):
            self.storage.compare_and_swap("a", 1, 1, mutation)
        self.assertEqual(
            self.storage.load_snapshot("b").chain.authorization_id, "auth-b"
        )
        self.assertEqual(
            RegistryEnvelope.from_bytes(
                self.storage.registry_path.read_bytes()
            ).revision,
            3,
        )

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX directory fsync")
    def test_unsupported_directory_fsync_preserves_successful_publication(self):
        snapshot = register(self.storage, "s")
        real_fsync = os.fsync

        def sync(descriptor):
            import stat

            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.EINVAL, "directory fsync unsupported")
            real_fsync(descriptor)

        with patch(
            "agent_handoff_toolkit.lifecycle_storage.os.fsync", side_effect=sync
        ):
            result = self.storage.compare_and_swap(
                "s",
                1,
                1,
                LifecycleMutation(replace(snapshot.session, targeted_revision=2)),
            )
        self.assertEqual(result.session.targeted_revision, 2)

    def join(self, session, chain):
        snapshot = self.storage.load_snapshot(session)
        return self.storage.compare_and_swap(
            session,
            chain.targeted_revision,
            0,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=1,
                    mode=EnforcementMode.TRACKED,
                    authorization_id=chain.authorization_id,
                    chain_revision=chain.targeted_revision,
                )
            ),
        )

    def test_join_adds_nonexpiring_lease_complete_rejects_join(self):
        a = register(self.storage, "a")
        b = self.join("b", a.chain)
        self.assertEqual(self.storage.load_snapshot("a"), a)
        self.assertEqual(self.storage.load_snapshot("b"), b)
        done = replace(a.chain, status="complete", targeted_revision=2)
        self.storage.compare_and_swap(
            "a",
            1,
            1,
            LifecycleMutation(
                replace(
                    a.session,
                    targeted_revision=2,
                    chain_revision=2,
                    mode=EnforcementMode.COMPLETE,
                ),
                done,
            ),
        )
        with self.assertRaises(LifecycleStorageError):
            self.join("c", done)
        envelope = RegistryEnvelope.from_bytes(self.storage.registry_path.read_bytes())
        self.assertNotIn(a.session.session_key, envelope.active_leases("auth-a"))

    def test_transition_supersedes_redirects_and_rejects_old_revision(self):
        a = register(self.storage, "a")
        b = self.join("b", a.chain)
        successor = ChainState(
            "auth-next", "root-next", ("b" * 64,), 1, "active", "record-next"
        )
        self.storage.compare_and_swap(
            "a",
            1,
            1,
            LifecycleMutation(
                replace(
                    a.session,
                    targeted_revision=2,
                    authorization_id="auth-next",
                    chain_revision=1,
                ),
                successor,
            ),
        )
        envelope = RegistryEnvelope.from_bytes(self.storage.registry_path.read_bytes())
        self.assertEqual(envelope.chains["auth-a"].status, "superseded")
        self.assertEqual(
            envelope.chains["auth-a"].successor_authorization_id, "auth-next"
        )
        redirected = self.storage.load_snapshot("b")
        self.assertEqual(redirected.chain.authorization_id, "auth-next")
        self.assertEqual(redirected.session.pending_transition_reference, "auth-next")
        with self.assertRaises(StaleLifecycleState):
            self.storage.compare_and_swap(
                "b", 1, 1, LifecycleMutation(replace(b.session, targeted_revision=2))
            )

    def test_rejects_duplicate_root_and_invalid_revision_or_scope_mutation(self):
        a = register(self.storage, "a")
        with self.assertRaises(LifecycleStorageError):
            register(self.storage, "b", "auth-b", "root-a")
        for mutation in (
            LifecycleMutation(a.session),
            LifecycleMutation(
                replace(a.session, targeted_revision=2),
                replace(a.chain, scope_digests=("b" * 64,)),
            ),
            LifecycleMutation(
                replace(
                    a.session,
                    targeted_revision=2,
                    mode=EnforcementMode.UNTRACKED,
                    authorization_id=None,
                    chain_revision=None,
                )
            ),
        ):
            with self.assertRaises(LifecycleStorageError):
                self.storage.compare_and_swap("a", 1, 1, mutation)

    def test_multiprocess_same_chain_one_winner(self):
        a = register(self.storage, "a")
        self.join("b", a.chain)
        self.run_race(["a", "b"], ["pass", "stale"])

    def test_multiprocess_unrelated_chains_both_persist(self):
        register(self.storage, "a")
        register(self.storage, "b", "auth-b", "root-b")
        self.run_race(["a", "b"], ["pass", "pass"])
        self.assertEqual(self.storage.load_snapshot("a").chain.targeted_revision, 2)
        self.assertEqual(self.storage.load_snapshot("b").chain.targeted_revision, 2)

    def run_race(self, sessions, expected):
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [
            context.Process(
                target=race_worker,
                args=(str(self.repo), str(self.state), s, barrier, queue),
            )
            for s in sessions
        ]
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        queue.close()
        self.assertEqual(sorted(results), expected)

    def test_secret_randomness_length_and_malformed_secret(self):
        other = LocalLifecycleStorage(self.repo, state_root=self.root / "other")
        self.assertEqual(len(self.storage.secret), 32)
        self.assertNotEqual(self.storage.secret, other.secret)
        self.assertNotEqual(self.storage.session_key("s"), other.session_key("s"))
        (self.state / "secret.key").write_bytes(b"short")
        with self.assertRaises(LifecycleStorageError):
            LocalLifecycleStorage(self.repo, state_root=self.state)

    def test_digest_looking_raw_ids_are_not_treated_as_already_hashed(self):
        key = self.storage.session_key("s")
        snapshot = self.storage.load_snapshot(key)
        self.assertNotEqual(snapshot.session.session_key, key)
        self.assertEqual(snapshot.session.session_key, self.storage.session_key(key))
        mutation = LifecycleMutation(replace(snapshot.session, targeted_revision=1))
        with self.assertRaises(LifecycleStorageError):
            self.storage.compare_and_swap("s", 0, 0, mutation)

    def test_secret_replaced_or_deleted_rejected_by_live_storage(self):
        secret = self.state / "secret.key"
        original = secret.read_bytes()
        for replacement in (secrets.token_bytes(32), None):
            secret.write_bytes(original)
            if replacement is None:
                secret.unlink()
            else:
                secret.write_bytes(replacement)
            with (
                self.subTest(deleted=replacement is None),
                self.assertRaises(LifecycleStorageError),
            ):
                self.storage.load_snapshot("s")

    def test_existing_registry_without_secret_fails_closed(self):
        register(self.storage, "s")
        (self.state / "secret.key").unlink()
        with self.assertRaises(LifecycleStorageError):
            LocalLifecycleStorage(self.repo, state_root=self.state)
        self.assertFalse((self.state / "secret.key").exists())

    def test_lock_adapters_select_correct_exclusive_and_unlock_operations(self):
        from agent_handoff_toolkit.lifecycle_storage import (
            _lock_descriptor,
            _unlock_descriptor,
        )

        with tempfile.TemporaryFile(dir=self.root) as lock:
            calls = []
            windows = SimpleNamespace(
                LK_LOCK=101,
                LK_UNLCK=102,
                locking=lambda fd, kind, count: calls.append((fd, kind, count)),
            )
            posix = SimpleNamespace(
                LOCK_EX=201,
                LOCK_UN=202,
                flock=lambda fd, kind: calls.append((fd, kind)),
            )
            with patch.dict(sys.modules, {"msvcrt": windows, "fcntl": posix}):
                _lock_descriptor(lock.fileno(), windows=True)
                _unlock_descriptor(lock.fileno(), windows=True)
                _lock_descriptor(lock.fileno(), windows=False)
                _unlock_descriptor(lock.fileno(), windows=False)
            self.assertEqual(
                calls,
                [
                    (lock.fileno(), 101, 1),
                    (lock.fileno(), 102, 1),
                    (lock.fileno(), 201),
                    (lock.fileno(), 202),
                ],
            )

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_root_rejected(self):
        junction = self.root / "junction"
        command = (
            "New-Item -ItemType Junction -Path '"
            + str(junction).replace("'", "''")
            + "' -Target '"
            + str(self.state).replace("'", "''")
            + "' | Out-Null"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
        )
        try:
            with self.assertRaises(LifecycleStorageError):
                LocalLifecycleStorage(self.repo, state_root=junction)
        finally:
            self.assertTrue(junction.parent.resolve().is_relative_to(self.root))
            junction.rmdir()

    def test_parent_substitution_during_publication_is_blocked(self):
        snapshot = register(self.storage, "s")
        before = self.storage.registry_path.read_bytes()
        real_fsync = os.fsync
        moved = self.root / "moved-during-publication"
        attempts = []

        def substitute(descriptor):
            if not attempts:
                attempts.append(True)
                try:
                    self.state.rename(moved)
                    self.state.mkdir()
                except OSError:
                    attempts.append("prevented")
            return real_fsync(descriptor)

        with patch(
            "agent_handoff_toolkit.lifecycle_storage.os.fsync", side_effect=substitute
        ):
            if os.name == "nt":
                self.storage.compare_and_swap(
                    "s",
                    1,
                    1,
                    LifecycleMutation(replace(snapshot.session, targeted_revision=2)),
                )
                self.assertEqual(attempts, [True, "prevented"])
            else:
                with self.assertRaises(LifecycleStorageError):
                    self.storage.compare_and_swap(
                        "s",
                        1,
                        1,
                        LifecycleMutation(
                            replace(snapshot.session, targeted_revision=2)
                        ),
                    )
                self.assertEqual(list(self.state.iterdir()), [])
                self.assertEqual((moved / "registry.json").read_bytes(), before)
                self.assertFalse(list(moved.glob("*.tmp")))

    def test_non_git_posix_default_fallback(self):
        from agent_handoff_toolkit import lifecycle_storage

        with (
            patch.object(lifecycle_storage, "_WINDOWS", False, create=True),
            patch.dict(
                os.environ,
                {
                    "XDG_STATE_HOME": str(self.root / "posix-cache"),
                    "LOCALAPPDATA": str(self.root / "windows-cache"),
                },
            ),
        ):
            path = resolve_lifecycle_state_root(self.repo)
        self.assertEqual(
            path.parent, self.root / "posix-cache" / "agent-handoff-toolkit"
        )

    def test_git_common_directory_must_be_valid_metadata(self):
        fake = self.root / "fake-metadata"
        fake.mkdir()
        result = subprocess.CompletedProcess([], 0, str(fake) + "\n", "")
        with patch(
            "agent_handoff_toolkit.lifecycle_storage.subprocess.run",
            return_value=result,
        ):
            with self.assertRaises(LifecycleStorageError):
                resolve_lifecycle_state_root(self.repo)

    @unittest.skipIf(
        os.name == "nt",
        "Windows uses containment and reparse checks, not POSIX mode parity",
    )
    def test_posix_insecure_secret_permissions_rejected(self):
        secret = self.state / "secret.key"
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        secret.chmod(0o644)
        with self.assertRaises(LifecycleStorageError):
            LocalLifecycleStorage(self.repo, state_root=self.state)

    @unittest.skipIf(
        os.name == "nt", "Windows directory ACLs do not have POSIX mode parity"
    )
    def test_posix_insecure_state_directory_rejected(self):
        self.state.chmod(0o777)
        with self.assertRaises(LifecycleStorageError):
            self.storage.load_snapshot("s")

    def test_missing_parents_created_and_registry_hardlink_rejected(self):
        nested = self.root / "missing" / "parents" / "state"
        storage = LocalLifecycleStorage(self.repo, state_root=nested)
        register(storage, "s")
        os.link(storage.registry_path, self.root / "registry-alias")
        with self.assertRaises(LifecycleStorageError):
            storage.load_snapshot("s")

    def test_raw_content_fields_rejected_and_sentinels_not_persisted(self):
        sentinel = secrets.token_hex(20)
        for content_field in (
            "prompt",
            "reply",
            "transcript",
            "credential",
            "customer_data",
            "tool_input",
        ):
            value = {
                "registry_version": 1,
                "revision": 0,
                "chains": {},
                "sessions": {},
                content_field: sentinel,
            }
            with (
                self.subTest(field=content_field),
                self.assertRaises(LifecycleStorageError),
            ):
                RegistryEnvelope.from_bytes(json.dumps(value).encode())
        register(self.storage, sentinel)
        self.assertNotIn(sentinel.encode(), self.storage.registry_path.read_bytes())

    def test_existing_parent_substitution_rejected(self):
        moved = self.root / "old-state"
        self.state.rename(moved)
        self.state.mkdir()
        with self.assertRaises(LifecycleStorageError):
            self.storage.load_snapshot("s")
        self.assertEqual(list(self.state.iterdir()), [])

    def test_symlink_state_root_rejected(self):
        link = self.root / "link"
        try:
            link.symlink_to(self.state, target_is_directory=True)
        except OSError as error:
            self.skipTest(
                f"Directory symlinks unavailable: {error.winerror if os.name == 'nt' else error.errno}"
            )
        with self.assertRaises(LifecycleStorageError):
            LocalLifecycleStorage(self.repo, state_root=link)

    def test_corrupt_registry_fails_closed_on_read_and_update(self):
        snapshot = register(self.storage, "a")
        self.storage.registry_path.write_bytes(b"broken")
        with self.assertRaises(LifecycleStorageError):
            self.storage.load_snapshot("a")
        with self.assertRaises(LifecycleStorageError):
            self.storage.compare_and_swap(
                "a",
                1,
                1,
                LifecycleMutation(replace(snapshot.session, targeted_revision=2)),
            )

    def test_git_checkout_and_linked_worktree_share_root(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init")
        git(
            "-c",
            "user.name=Storage Test",
            "-c",
            "user.email=storage@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        )
        linked = self.root / "linked"
        git("worktree", "add", "--detach", str(linked))
        expected = self.repo / ".git" / "agent-handoff-toolkit" / "lifecycle"
        self.assertEqual(resolve_lifecycle_state_root(self.repo), expected)
        self.assertEqual(resolve_lifecycle_state_root(linked), expected)

    def test_git_root_resolver_rejects_non_directory_state_parent(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "init"], check=True, capture_output=True
        )
        (self.repo / ".git" / "agent-handoff-toolkit").write_bytes(b"opaque")
        with self.assertRaises(LifecycleStorageError):
            resolve_lifecycle_state_root(self.repo)

    def test_non_git_platform_fallback_hides_repository(self):
        cache = self.root / "cache"
        cache.mkdir()
        with patch.dict(
            os.environ, {"LOCALAPPDATA": str(cache), "XDG_STATE_HOME": str(cache)}
        ):
            resolved = resolve_lifecycle_state_root(self.repo)
            again = resolve_lifecycle_state_root(self.repo)
        self.assertEqual(resolved, again)
        self.assertTrue(resolved.is_relative_to(cache))
        self.assertEqual(len(resolved.name), 64)
        self.assertNotIn(self.repo.name, str(resolved))


if __name__ == "__main__":
    unittest.main()
