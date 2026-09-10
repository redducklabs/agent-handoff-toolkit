"""Private local lifecycle state, serialized by one locked atomic publication.

Public session arguments are always raw host IDs. ``session_key`` derives the
persisted HMAC; digest-looking raw IDs receive exactly the same treatment.
Leases are non-complete session memberships and have no wall-clock expiry.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
from types import MappingProxyType

from .lifecycle import (
    AuthorityCategory,
    AuthorizationProposal,
    ChainState,
    DecisionRequest,
    EnforcementMode,
    LifecycleMutation,
    LifecycleSnapshot,
    SessionState,
    RecordReference,
)
from .lineage import canonical_json_bytes, validate_hex_digest

MAX_REGISTRY_BYTES = 1024 * 1024
_WINDOWS = os.name == "nt"


class LifecycleStorageError(ValueError):
    """State is unsafe, invalid, unreadable, or could not be published."""


class StaleLifecycleState(LifecycleStorageError):
    """The targeted chain or session changed; reconcile before retrying."""


def _revision(value):
    if type(value) is not int or value < 0:
        raise LifecycleStorageError("revision must be a non-negative integer")
    return value


def _plain(value):
    if isinstance(
        value,
        (
            ChainState,
            SessionState,
            DecisionRequest,
            RecordReference,
            AuthorizationProposal,
        ),
    ):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (EnforcementMode, AuthorityCategory)):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _model(value, model):
    if not isinstance(value, dict) or set(value) != {
        item.name for item in fields(model)
    }:
        raise LifecycleStorageError("state contains unknown or missing fields")
    value = dict(value)
    if model is SessionState:
        value["mode"] = EnforcementMode(value["mode"])
        if value["pending_decision_reference"] is not None:
            value["pending_decision_reference"] = _model(
                value["pending_decision_reference"], DecisionRequest
            )
        if value["pending_transition_reference"] is not None:
            value["pending_transition_reference"] = _model(
                value["pending_transition_reference"], AuthorizationProposal
            )
    if model is AuthorizationProposal and value["selected_record"] is not None:
        value["selected_record"] = _model(value["selected_record"], RecordReference)
    if model is ChainState and value["current_record_reference"] is not None:
        value["current_record_reference"] = _model(
            value["current_record_reference"], RecordReference
        )
    if model is ChainState and value["publication_evidence"] is not None:
        value["publication_evidence"] = _model(
            value["publication_evidence"], AuthorizationProposal
        )
    if model is DecisionRequest:
        value["category"] = AuthorityCategory(value["category"])
    if model is ChainState and not isinstance(value["scope_digests"], list):
        raise LifecycleStorageError("scope digests must be an array")
    return model(**value)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleStorageError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise LifecycleStorageError("non-finite JSON is forbidden")


@dataclass(frozen=True)
class RegistryEnvelope:
    registry_version: int = 1
    revision: int = 0
    chains: Mapping[str, ChainState] = field(default_factory=dict)
    sessions: Mapping[str, SessionState] = field(default_factory=dict)

    def __post_init__(self):
        try:
            if type(self.registry_version) is not int or self.registry_version != 1:
                raise LifecycleStorageError("unsupported registry version")
            _revision(self.revision)
            if not isinstance(self.chains, Mapping) or not isinstance(
                self.sessions, Mapping
            ):
                raise LifecycleStorageError("registry entries must be maps")
            chains, sessions = dict(self.chains), dict(self.sessions)
            active_roots = set()
            for key, chain in chains.items():
                if not isinstance(chain, ChainState) or key != chain.authorization_id:
                    raise LifecycleStorageError("chain identity mismatch")
                _model(_plain(chain), ChainState)
                if chain.status == "superseded":
                    successor = chains.get(chain.successor_authorization_id)
                    if successor is None or successor.authorization_id == key:
                        raise LifecycleStorageError("missing successor chain")
                elif chain.successor_authorization_id is not None:
                    raise LifecycleStorageError(
                        "only superseded chains identify successors"
                    )
                if chain.status == "active":
                    identity = (chain.locked_root_id, chain.scope_digests[0])
                    if identity in active_roots:
                        raise LifecycleStorageError(
                            "duplicate active root and scope definition"
                        )
                    active_roots.add(identity)
                visited = {key}
                cursor = chain
                while cursor.successor_authorization_id is not None:
                    if cursor.successor_authorization_id in visited:
                        raise LifecycleStorageError("successor cycle")
                    visited.add(cursor.successor_authorization_id)
                    cursor = chains[cursor.successor_authorization_id]
            for key, session in sessions.items():
                validate_hex_digest(key, label="session map key")
                if not isinstance(session, SessionState) or key != session.session_key:
                    raise LifecycleStorageError("session identity mismatch")
                _model(_plain(session), SessionState)
                if session.authorization_id is not None:
                    chain = chains.get(session.authorization_id)
                    if chain is None or chain.status == "superseded":
                        raise LifecycleStorageError(
                            "session must reference a current chain"
                        )
                    if session.chain_revision > chain.targeted_revision:
                        raise LifecycleStorageError(
                            "session revision is ahead of its chain"
                        )
                    if (
                        session.mode is EnforcementMode.COMPLETE
                        and chain.status != "complete"
                    ):
                        raise LifecycleStorageError(
                            "complete session requires a complete chain"
                        )
                    request = session.pending_decision_reference
                    if request and request.authorization_id != session.authorization_id:
                        raise LifecycleStorageError(
                            "decision belongs to a different chain"
                        )
            object.__setattr__(self, "chains", MappingProxyType(chains))
            object.__setattr__(self, "sessions", MappingProxyType(sessions))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            if isinstance(error, LifecycleStorageError):
                raise
            raise LifecycleStorageError("invalid lifecycle envelope") from error

    def active_leases(self, authorization_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key, session in self.sessions.items()
                if session.authorization_id == authorization_id
                and session.mode is not EnforcementMode.COMPLETE
            )
        )

    def to_bytes(self) -> bytes:
        data = canonical_json_bytes(
            {
                "registry_version": self.registry_version,
                "revision": self.revision,
                "chains": {key: _plain(value) for key, value in self.chains.items()},
                "sessions": {
                    key: _plain(value) for key, value in self.sessions.items()
                },
            }
        )
        if len(data) > MAX_REGISTRY_BYTES:
            raise LifecycleStorageError("registry exceeds size limit")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> RegistryEnvelope:
        try:
            if not isinstance(data, bytes) or len(data) > MAX_REGISTRY_BYTES:
                raise LifecycleStorageError("registry exceeds size limit")
            value = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_constant=_nonfinite,
            )
            if not isinstance(value, dict) or set(value) != {
                "registry_version",
                "revision",
                "chains",
                "sessions",
            }:
                raise LifecycleStorageError(
                    "registry contains unknown or missing fields"
                )
            for name, model in (("chains", ChainState), ("sessions", SessionState)):
                if not isinstance(value[name], dict):
                    raise LifecycleStorageError("registry entries must be maps")
                value[name] = {
                    key: _model(item, model) for key, item in value[name].items()
                }
            return cls(**value)
        except (
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
            OverflowError,
        ) as error:
            if isinstance(error, LifecycleStorageError):
                raise
            raise LifecycleStorageError("invalid lifecycle registry") from error


def _parts(path):
    path = Path(os.path.abspath(path))
    return [*reversed(path.parents), path]


def _check_path(path, *, directory=False):
    """Reject all symlink/reparse components, even links remaining in bounds."""
    for item in _parts(path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise LifecycleStorageError("linked or reparse state path is forbidden")
        if item != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise LifecycleStorageError("state parent is not a directory")
    return info


def _identity(path):
    info = _check_path(path, directory=True)
    return info.st_dev, info.st_ino


def _private_directory(path):
    info = _check_path(path, directory=True)
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise LifecycleStorageError("state directory must be owner-only")


def _open_directory(path):
    """Walk each POSIX component relative to the pinned prior descriptor."""
    components = _parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(components[0], flags)
    try:
        for component in components[1:]:
            child = os.open(component.name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir_private(path):
    path = Path(os.path.abspath(path))
    for item in _parts(path):
        if not item.exists():
            try:
                with _directory_guard(item.parent):
                    if os.name == "nt":
                        item.mkdir(mode=0o700)
                    else:
                        descriptor = _open_directory(item.parent)
                        try:
                            os.mkdir(item.name, mode=0o700, dir_fd=descriptor)
                        finally:
                            os.close(descriptor)
            except FileExistsError:
                pass
        _check_path(item, directory=True)
    return path


@contextmanager
def _directory_guard(path):
    """Pin Windows ancestors against rename/delete while a transaction runs.

    POSIX descriptors pin directories; no-follow opens plus identity rechecks
    reject substitution. Windows sharing flags enforce the rename exclusion.
    """
    handles = []
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            create.restype = wintypes.HANDLE
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            for item in _parts(path):
                handle = create(str(item), 0x80, 3, None, 3, 0x02200000, None)
                if handle == wintypes.HANDLE(-1).value:
                    raise LifecycleStorageError("cannot pin state directory")
                handles.append(handle)
                _check_path(item, directory=True)
        else:
            close = os.close
            for item in _parts(path):
                handles.append(
                    os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                )
                _check_path(item, directory=True)
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def _open_private(path, flags, *, mode=0o600):
    _check_path(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        info = _check_path(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise LifecycleStorageError("state file must be a single-link regular file")
        if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise LifecycleStorageError("state file must be owner-only")
    open_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    parent_descriptor = None
    if os.name == "nt":
        descriptor = os.open(path, open_flags, mode)
    else:
        parent_descriptor = _open_directory(path.parent)
        try:
            descriptor = os.open(path.name, open_flags, mode, dir_fd=parent_descriptor)
        finally:
            os.close(parent_descriptor)
    try:
        before = os.fstat(descriptor)
        after = _check_path(path)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise LifecycleStorageError("state file changed during open")
        if os.name != "nt" and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
            raise LifecycleStorageError("state file must be owner-only")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _lock_descriptor(descriptor, *, windows):
    if windows:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_descriptor(descriptor, *, windows):
    if windows:
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def _exclusive_lock(path):
    descriptor = _open_private(path, os.O_RDWR | os.O_CREAT)
    try:
        _lock_descriptor(descriptor, windows=os.name == "nt")
        try:
            yield
        finally:
            _unlock_descriptor(descriptor, windows=os.name == "nt")
    finally:
        os.close(descriptor)


def _read_secret(path):
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        value = source.read(33)
    if len(value) != 32:
        raise LifecycleStorageError("invalid local HMAC secret")
    return value


def _secret(path):
    try:
        descriptor = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        with os.fdopen(descriptor, "wb") as output:
            output.write(secrets.token_bytes(32))
            output.flush()
            os.fsync(output.fileno())
    return _read_secret(path)


def resolve_lifecycle_state_root(repo_root: Path) -> Path:
    """Resolve shared Git metadata or a private repository-HMAC fallback."""
    try:
        repo = Path(os.path.abspath(repo_root))
        _check_path(repo, directory=True)
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except FileNotFoundError:
            result = None
        if result is not None and result.returncode == 0:
            raw = result.stdout.strip()
            if not raw or "\n" in raw or "\r" in raw:
                raise LifecycleStorageError("invalid Git common directory")
            common = Path(raw)
            if not common.is_absolute():
                common = repo / common
            common = Path(os.path.abspath(common))
            _check_path(common, directory=True)
            # Git common metadata must identify an actual object database.
            _check_path(common / "objects", directory=True)
            _check_path(common / "refs", directory=True)
            if not (common / "HEAD").is_file():
                raise LifecycleStorageError("invalid Git common metadata")
            target = common / "agent-handoff-toolkit" / "lifecycle"
            for component in (target.parent, target):
                if component.exists() or component.is_symlink():
                    _check_path(component, directory=True)
            return target
        if (repo / ".git").exists() or (repo / ".git").is_symlink():
            raise LifecycleStorageError("Git metadata could not be resolved safely")
        if _WINDOWS:
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                raise LifecycleStorageError("LOCALAPPDATA is required for local state")
            cache = Path(base) / "agent-handoff-toolkit" / "state"
        else:
            base = os.environ.get("XDG_STATE_HOME")
            cache = (
                Path(base) if base else Path.home() / ".local" / "state"
            ) / "agent-handoff-toolkit"
        if not cache.is_absolute():
            raise LifecycleStorageError("state fallback must be absolute")
        cache = _mkdir_private(cache)
        _private_directory(cache)
        with _directory_guard(cache), _exclusive_lock(cache / "registry.lock"):
            secret_path = cache / "secret.key"
            if not secret_path.exists() and any(
                entry.name != "registry.lock" for entry in cache.iterdir()
            ):
                # Without this key, existing repository directory names cannot
                # be recovered. A new key would silently abandon tracked state.
                raise LifecycleStorageError(
                    "established fallback state has no HMAC secret"
                )
            secret = _secret(secret_path)
        digest = hmac.new(
            secret,
            b"repository\0" + os.path.normcase(str(repo)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        target = cache / digest
        if target.exists() or target.is_symlink():
            _check_path(target, directory=True)
        return target
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, LifecycleStorageError):
            raise
        raise LifecycleStorageError("cannot resolve lifecycle state root") from error


class LocalLifecycleStorage:
    def __init__(self, repo_root: Path, *, state_root: Path | None = None):
        try:
            self.state_root = _mkdir_private(
                state_root
                if state_root is not None
                else resolve_lifecycle_state_root(repo_root)
            )
            _private_directory(self.state_root)
            self._identities = {
                path: _identity(path) for path in _parts(self.state_root)
            }
            self.registry_path = self.state_root / "registry.json"
            with self._locked():
                secret_path = self.state_root / "secret.key"
                if self.registry_path.exists() and not secret_path.exists():
                    raise LifecycleStorageError("existing registry has no HMAC secret")
                self.secret = _secret(secret_path)
        except OSError as error:
            raise LifecycleStorageError(
                "cannot initialize lifecycle storage"
            ) from error

    @contextmanager
    def _guard(self):
        with _directory_guard(self.state_root):
            self._verify_identity()
            if hasattr(self, "secret") and not hmac.compare_digest(
                self.secret, _read_secret(self.state_root / "secret.key")
            ):
                raise LifecycleStorageError("local HMAC secret changed")
            yield

    @contextmanager
    def _locked(self):
        with self._guard(), _exclusive_lock(self.state_root / "registry.lock"):
            # POSIX flock pins the lock file, not its pathname or parent.
            # Reconcile the directory identity after waiting for that lock and
            # again before exposing any result read under it.
            self._verify_identity()
            yield
            self._verify_identity()

    def _verify_identity(self):
        _private_directory(self.state_root)
        if any(
            _identity(path) != identity for path, identity in self._identities.items()
        ):
            raise LifecycleStorageError("state directory was substituted")

    def session_key(self, raw_session_id: str) -> str:
        if (
            not isinstance(raw_session_id, str)
            or not raw_session_id
            or len(raw_session_id.encode("utf-8")) > 4096
        ):
            raise LifecycleStorageError("invalid raw session identifier")
        return hmac.new(
            self.secret, b"session\0" + raw_session_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _load(self):
        try:
            descriptor = _open_private(self.registry_path, os.O_RDONLY)
        except FileNotFoundError:
            return RegistryEnvelope()
        with os.fdopen(descriptor, "rb") as source:
            data = source.read(MAX_REGISTRY_BYTES + 1)
        return RegistryEnvelope.from_bytes(data)

    def _snapshot(self, envelope, key):
        session = envelope.sessions.get(
            key, SessionState(key, 0, EnforcementMode.UNTRACKED)
        )
        return LifecycleSnapshot(envelope.chains.get(session.authorization_id), session)

    def load_snapshot(self, session_key: str) -> LifecycleSnapshot:
        """Read by raw host session ID; returned session identity is its HMAC."""
        try:
            key = self.session_key(session_key)
            with self._locked():
                return self._snapshot(self._load(), key)
        except OSError as error:
            raise LifecycleStorageError("cannot read lifecycle state") from error

    def compare_and_swap(
        self,
        session_key: str,
        expected_chain_revision: int,
        expected_session_revision: int,
        mutation: LifecycleMutation,
    ) -> LifecycleSnapshot:
        """Apply a complete mutation after comparing targeted live revisions."""
        try:
            _revision(expected_chain_revision)
            _revision(expected_session_revision)
            key = self.session_key(session_key)
            if (
                not isinstance(mutation, LifecycleMutation)
                or mutation.session.session_key != key
            ):
                raise LifecycleStorageError("mutation session identity mismatch")
            with self._locked():
                envelope = self._load()
                current = self._snapshot(envelope, key)
                target = current.chain or envelope.chains.get(
                    mutation.session.authorization_id
                )
                revision = target.targeted_revision if target else 0
                if (
                    revision != expected_chain_revision
                    or current.session.targeted_revision != expected_session_revision
                ):
                    raise StaleLifecycleState("targeted lifecycle revision changed")
                updated = self._apply(envelope, current, mutation)
                self._publish(updated.to_bytes())
                return self._snapshot(updated, key)
        except OSError as error:
            raise LifecycleStorageError("cannot publish lifecycle state") from error

    def load_registry(self) -> RegistryEnvelope:
        """Return a validated immutable view under the registry lock."""
        try:
            with self._locked():
                return self._load()
        except OSError as error:
            raise LifecycleStorageError("cannot read lifecycle registry") from error

    def load_chain(self, authorization_id: str) -> ChainState | None:
        """Look up one authorization without scanning record files."""
        return self.load_registry().chains.get(authorization_id)

    def _apply(self, envelope, current, mutation):
        session, chain = mutation.session, mutation.chain
        if session.targeted_revision != current.session.targeted_revision + 1:
            raise LifecycleStorageError("session revision must advance exactly once")
        if current.chain is not None and session.mode is EnforcementMode.UNTRACKED:
            raise LifecycleStorageError("tracked sessions cannot become untracked")
        chains, sessions = dict(envelope.chains), dict(envelope.sessions)
        if chain is not None:
            if chain.authorization_id != session.authorization_id:
                raise LifecycleStorageError("mutation chain/session mismatch")
            existing = chains.get(chain.authorization_id)
            if existing is not None:
                if existing.status != "active":
                    raise LifecycleStorageError(
                        "terminal chains cannot be changed or joined"
                    )
                if (chain.locked_root_id, chain.scope_digests) != (
                    existing.locked_root_id,
                    existing.scope_digests,
                ):
                    raise LifecycleStorageError(
                        "scope changes require a successor authorization"
                    )
                if chain.publication_evidence != existing.publication_evidence and (
                    existing.publication_evidence is None
                    or chain.publication_evidence is not None
                    or chain.current_record_reference
                    == existing.current_record_reference
                ):
                    raise LifecycleStorageError(
                        "publication evidence can close only when publishing its successor record"
                    )
                if (
                    chain != existing
                    and chain.targeted_revision != existing.targeted_revision + 1
                ):
                    raise LifecycleStorageError(
                        "chain revision must advance exactly once"
                    )
            elif chain.targeted_revision != 1 or chain.status != "active":
                raise LifecycleStorageError(
                    "new chains must start active at revision one"
                )
            if chain.status == "superseded":
                raise LifecycleStorageError(
                    "supersession requires a successor mutation"
                )
            chains[chain.authorization_id] = chain
        if (
            current.chain is not None
            and current.chain.authorization_id != session.authorization_id
        ):
            old = current.chain
            if (
                old.status != "active"
                or chain is None
                or chain.authorization_id in envelope.chains
            ):
                raise LifecycleStorageError(
                    "transition must create a new successor chain"
                )
            chains[old.authorization_id] = replace(
                old,
                status="superseded",
                targeted_revision=old.targeted_revision + 1,
                successor_authorization_id=chain.authorization_id,
            )
            for key, prior in sessions.items():
                if prior.authorization_id == old.authorization_id:
                    sessions[key] = replace(
                        prior,
                        authorization_id=chain.authorization_id,
                        chain_revision=chain.targeted_revision,
                        targeted_revision=prior.targeted_revision + 1,
                        pending_transition_reference=session.pending_transition_reference,
                        pending_decision_reference=None,
                        mode=EnforcementMode.TRACKED,
                    )
        if session.authorization_id is not None:
            target = chains.get(session.authorization_id)
            if target is None or target.status == "superseded":
                raise LifecycleStorageError("unknown or superseded authorization")
            if current.chain is None and target.status != "active":
                raise LifecycleStorageError("complete chains reject joins")
            if session.chain_revision != target.targeted_revision:
                raise StaleLifecycleState("session requires chain reconciliation")
            if (
                target.status == "complete"
                and session.mode is not EnforcementMode.COMPLETE
            ):
                raise LifecycleStorageError("completion must release the current lease")
        sessions[session.session_key] = session
        return RegistryEnvelope(
            revision=envelope.revision + 1, chains=chains, sessions=sessions
        )

    def _publish(self, data):
        temporary = self.state_root / (secrets.token_hex(16) + ".tmp")
        created = False
        parent_descriptor = None
        try:
            self._verify_identity()
            if os.name == "nt":
                descriptor = _open_private(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                )
            else:
                parent_descriptor = _open_directory(self.state_root)
                self._verify_identity()
                descriptor = os.open(
                    temporary.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            created = True
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._verify_identity()
            if self.registry_path.exists() or self.registry_path.is_symlink():
                _check_path(self.registry_path)
            if parent_descriptor is None:
                os.replace(temporary, self.registry_path)
            else:
                os.replace(
                    temporary.name,
                    self.registry_path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            created = False
            if parent_descriptor is not None:
                try:
                    os.fsync(parent_descriptor)
                except OSError as error:
                    # Some POSIX filesystems do not implement directory fsync.
                    # Actual I/O failures still fail closed after publication.
                    if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                        raise
        finally:
            try:
                if created:
                    if parent_descriptor is None:
                        self._verify_identity()
                        temporary.unlink()
                    else:
                        os.unlink(temporary.name, dir_fd=parent_descriptor)
            finally:
                if parent_descriptor is not None:
                    os.close(parent_descriptor)
