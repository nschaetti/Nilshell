"""Local system-call broker. Only the trusted desktop can approve pending calls.

This broker is not a Python or subprocess sandbox. Agent tools will require a
separate isolated runtime and identity-bound authorization in a later milestone.
"""

import codecs
import hashlib
import os
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from shutil import which
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .permissions import Caller, Permissions


class KernelError(Exception):
    """A structured error whose message is safe to show in the desktop."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PathArgs(Arguments):
    path: str = Field(min_length=1, max_length=8192)


class ListArgs(PathArgs):
    limit: int = Field(default=200, ge=1, le=1000)


class ReadArgs(PathArgs):
    max_bytes: int = Field(default=131072, ge=1, le=1048576)


class WriteArgs(PathArgs):
    content: str = Field(max_length=1048576)
    overwrite: bool = False
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CommandArgs(Arguments):
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str = Field(min_length=1, max_length=8192)
    timeout_seconds: int = Field(default=15, ge=1, le=60)
    max_output_bytes: int = Field(default=65536, ge=1, le=1048576)


CATALOG = {
    "fs.stat": (PathArgs, "Informations sur un fichier ou dossier"),
    "fs.list": (ListArgs, "Lister un dossier sans parcourir ses sous-dossiers"),
    "fs.read": (ReadArgs, "Lire un fichier texte UTF-8"),
    "fs.write": (WriteArgs, "Créer ou remplacer un fichier texte UTF-8"),
    "fs.mkdir": (PathArgs, "Créer un dossier"),
    "process.run": (CommandArgs, "Lancer un programme avec des arguments, sans shell implicite"),
}


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or "\x00" in value:
        raise KernelError("invalid_path", "Un chemin absolu est requis (ou ~/…).")
    return path


def fingerprint(path):
    try:
        value = path.stat()
        return [value.st_dev, value.st_ino, value.st_mtime_ns, value.st_size]
    except FileNotFoundError:
        return None


@contextmanager
def directory_handle(path):
    """Anchor POSIX operations to a directory opened without following symlinks.

    Windows falls back to canonical-path checks. Neither path handling nor cwd
    isolates a launched program from the rest of the user's filesystem.
    """
    if os.name != "posix":
        yield None
        return
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def entry_name(path, fd):
    return path.name if fd is not None else str(path)


def open_regular(path, parent_fd):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(entry_name(path, parent_fd), flags, dir_fd=parent_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise KernelError("not_regular_file", "Seuls les fichiers ordinaires sont acceptés.")
    return os.fdopen(fd, "rb")


class Kernel:
    """Validate, prepare, approve once, execute and audit local calls."""

    def __init__(self, store):
        self.store = store
        self.pending = {}
        self.lock = threading.Lock()
        self.permissions = Permissions(store, CATALOG)
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS system_calls (
                id TEXT PRIMARY KEY, syscall TEXT NOT NULL, resource TEXT NOT NULL,
                state TEXT NOT NULL, code TEXT, duration_ms INTEGER,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            db.execute("""UPDATE system_calls SET state='interrupted',code='engine_restarted'
                WHERE state IN ('pending','running')""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(system_calls)")}
            for name in ("caller_json", "permission_json"):
                if name not in columns:
                    db.execute(f"ALTER TABLE system_calls ADD COLUMN {name} TEXT")

    def catalog(self):
        return [
            {"name": name, "description": description, "schema": model.model_json_schema()}
            for name, (model, description) in CATALOG.items()
        ]

    def normalize(self, syscall, values):
        if syscall not in CATALOG:
            raise KernelError("unknown_syscall", "Appel système inconnu.")
        try:
            args = CATALOG[syscall][0].model_validate(values).model_dump()
        except ValidationError:
            raise KernelError(
                "invalid_arguments", "Arguments invalides pour cet appel système."
            ) from None
        target = absolute_path(args["cwd"] if syscall == "process.run" else args["path"])
        try:
            canonical = target.resolve(strict=syscall not in {"fs.write", "fs.mkdir"})
            parent = canonical.parent.resolve(strict=True)
            if not parent.is_dir():
                raise KernelError("not_directory", "Le dossier parent est introuvable.")
            if syscall in {"fs.list", "process.run"} and not canonical.is_dir():
                raise KernelError("not_directory", "Un dossier est requis.")
            if (
                syscall in {"fs.read", "fs.write"}
                and canonical.exists()
                and not canonical.is_file()
            ):
                raise KernelError("not_regular_file", "Un fichier ordinaire est requis.")
            if syscall == "fs.write":
                if len(args["content"].encode("utf-8")) > 1048576:
                    raise KernelError("too_large", "Écriture limitée à 1 Mio.")
                if canonical.exists() and not args["overwrite"]:
                    raise KernelError(
                        "already_exists", "Le fichier existe. Active le remplacement explicitement."
                    )
                if args["expected_sha256"] and not args["overwrite"]:
                    raise KernelError(
                        "invalid_arguments", "L’empreinte requiert le remplacement explicite."
                    )
            if syscall == "fs.mkdir" and canonical.exists():
                raise KernelError("already_exists", "Ce chemin existe déjà.")
        except (OSError, RuntimeError):
            raise KernelError(
                "invalid_path", "Chemin inaccessible ou lien symbolique invalide."
            ) from None
        args["cwd" if syscall == "process.run" else "path"] = str(canonical)
        target_guard = fingerprint(canonical)
        guards = {
            "target": target_guard[:2] if canonical.is_dir() and target_guard else target_guard,
            "parent": fingerprint(parent)[:2],
        }
        if syscall == "process.run":
            if any("\x00" in arg or len(arg) > 32768 for arg in args["argv"]):
                raise KernelError("invalid_arguments", "Arguments de commande invalides.")
            program = args["argv"][0]
            if not Path(program).is_absolute() and ("/" in program or "\\" in program):
                raise KernelError(
                    "invalid_arguments", "Utilise un nom de programme ou un chemin absolu."
                )
            executable = which(program)
            if executable is None:
                raise KernelError("executable_missing", "Programme introuvable dans PATH.")
            executable = str(Path(executable).resolve(strict=True))
            args["argv"][0] = executable
            guards["executable"] = fingerprint(Path(executable))
        return args, guards

    def prepare(self, syscall, values, caller=None):
        caller = caller or Caller()
        identifier = str(uuid4())
        try:
            args, guards = self.normalize(syscall, values)
        except KernelError as exc:
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO system_calls(id,syscall,resource,state,code) VALUES (?,?,?,'error',?)",
                    (identifier, syscall if isinstance(syscall, str) else "invalid", "", exc.code),
                )
            raise
        decision = self.permissions.evaluate(syscall, args, caller)
        now = time.monotonic()
        resource = args.get("path", args.get("cwd", ""))
        if syscall == "process.run":
            resource = args["argv"][0]
        with self.lock:
            for key, value in list(self.pending.items()):
                if value["expires"] <= now:
                    self.pending.pop(key)
                    self.finish(key, "expired", "approval_expired", 0)
            if len(self.pending) >= 64:
                raise KernelError("too_many_requests", "Trop de demandes en attente.")
            self.pending[identifier] = {
                "syscall": syscall,
                "args": args,
                "guards": guards,
                "expires": now + 300,
                "caller": caller,
            }
            try:
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO system_calls(id,syscall,resource,state) VALUES (?,?,?,'pending')",
                        (identifier, syscall, resource),
                    )
            except sqlite3.Error:
                self.pending.pop(identifier)
                raise
        self.record_decision(identifier, caller, decision)
        if decision["effect"] == "deny":
            with self.lock:
                self.pending.pop(identifier, None)
            self.finish(identifier, "denied", "policy_denied", 0)
            raise KernelError("policy_denied", "Appel interdit par une règle de permission.")
        return {
            "id": identifier,
            "syscall": syscall,
            "caller": asdict(caller),
            "permission": decision,
            "arguments": deepcopy(args),
            "expires_in_seconds": 300,
            "warning": "La commande dispose des droits de ton utilisateur. Le dossier de travail ne limite pas ses accès."
            if syscall == "process.run"
            else None,
        }

    def finish(self, identifier, state, code, duration):
        with self.store.connect() as db:
            db.execute(
                "UPDATE system_calls SET state=?,code=?,duration_ms=? WHERE id=?",
                (state, code, duration, identifier),
            )

    def record_decision(self, identifier, caller, decision):
        import json

        with self.store.connect() as db:
            db.execute(
                "UPDATE system_calls SET caller_json=?,permission_json=? WHERE id=?",
                (json.dumps(asdict(caller)), json.dumps(decision), identifier),
            )

    def execute(self, identifier, approved=None, remember="once"):
        # Consume atomically before touching the filesystem; tickets cannot be replayed.
        with self.lock:
            request = self.pending.pop(identifier, None)
        if request is None:
            raise KernelError(
                "approval_required", "Autorisation absente, déjà utilisée ou perdue au redémarrage."
            )
        started = time.monotonic()
        try:
            if remember not in {"once", "session", "permanent"}:
                raise KernelError("invalid_scope", "Durée d’autorisation invalide.")
            if approved is not None and approved is not True and approved is not False:
                raise KernelError("denied", "Un accord explicite est requis.")
            if approved is False:
                raise KernelError("denied", "Appel refusé.")
            if request["expires"] <= started:
                raise KernelError(
                    "approval_expired", "Autorisation expirée. Prépare à nouveau l’appel."
                )
            args, guards = self.normalize(request["syscall"], request["args"])
            if args != request["args"] or guards != request["guards"]:
                raise KernelError(
                    "resource_changed",
                    "La ressource a changé depuis la demande. Prépare à nouveau l’appel.",
                )
            decision = self.permissions.evaluate(request["syscall"], args, request["caller"])
            self.record_decision(identifier, request["caller"], decision)
            if decision["effect"] == "deny":
                raise KernelError("policy_denied", "Appel interdit par une règle de permission.")
            if approved is not True and decision["effect"] != "allow":
                raise KernelError("denied", "Une confirmation est requise pour cet appel.")
            if remember != "once" and approved is not True:
                raise KernelError(
                    "denied", "Mémoriser un accord requiert une confirmation explicite."
                )
            self.finish(identifier, "running", None, 0)
            result = self.perform(request["syscall"], args)
            if approved is True and remember != "once":
                self.permissions.remember(request["syscall"], args, request["caller"], remember)
        except KernelError as exc:
            self.finish(
                identifier,
                "denied"
                if exc.code in {"denied", "approval_expired", "policy_denied"}
                else "error",
                exc.code,
                round((time.monotonic() - started) * 1000),
            )
            raise
        except (OSError, UnicodeError, ValueError):
            self.finish(
                identifier, "error", "operation_failed", round((time.monotonic() - started) * 1000)
            )
            raise KernelError(
                "operation_failed",
                "Opération impossible : accès, type de fichier ou encodage invalide.",
            ) from None
        self.finish(identifier, "complete", None, round((time.monotonic() - started) * 1000))
        return {"id": identifier, "syscall": request["syscall"], "result": result}

    def history(self):
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM system_calls ORDER BY rowid DESC LIMIT 100")
            ]

    def perform(self, syscall, args):
        if syscall == "process.run":
            return self.run_process(args)
        path = Path(args["path"])
        anchor = path if syscall == "fs.list" or path == path.parent else path.parent
        with directory_handle(anchor) as fd:
            name = entry_name(path, fd)
            if syscall == "fs.stat":
                value = (
                    os.fstat(fd)
                    if path == path.parent and fd is not None
                    else os.stat(name, dir_fd=fd, follow_symlinks=False)
                )
                return {
                    "path": str(path),
                    "kind": "directory"
                    if stat.S_ISDIR(value.st_mode)
                    else "file"
                    if stat.S_ISREG(value.st_mode)
                    else "other",
                    "size": value.st_size,
                    "modified_ns": value.st_mtime_ns,
                }
            if syscall == "fs.list":
                entries = []
                # scandir's iterator is bounded; never loads a whole large directory into memory.
                with os.scandir(fd if fd is not None else path) as iterator:
                    for item in iterator:
                        if len(entries) >= args["limit"]:
                            return {"path": str(path), "entries": entries, "truncated": True}
                        entries.append(
                            {
                                "name": item.name,
                                "path": str(path / item.name),
                                "kind": "symlink"
                                if item.is_symlink()
                                else "directory"
                                if item.is_dir(follow_symlinks=False)
                                else "file"
                                if item.is_file(follow_symlinks=False)
                                else "other",
                            }
                        )
                return {"path": str(path), "entries": entries, "truncated": False}
            if syscall == "fs.read":
                with open_regular(path, fd) as file:
                    raw = file.read(args["max_bytes"] + 1)
                truncated = len(raw) > args["max_bytes"]
                raw = raw[: args["max_bytes"]]
                # Incremental decode tolerates only an incomplete final UTF-8 character.
                text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not truncated)
                return {
                    "path": str(path),
                    "content": text,
                    "bytes_read": len(raw),
                    "truncated": truncated,
                }
            if syscall == "fs.mkdir":
                os.mkdir(name, mode=0o700, dir_fd=fd)
                return {"path": str(path)}
            if syscall == "fs.write":
                return self.write_file(path, fd, args)
        raise KernelError("unknown_syscall", "Appel système inconnu.")

    def write_file(self, path, parent_fd, args):
        content = args["content"].encode("utf-8")
        if args["expected_sha256"]:
            with open_regular(path, parent_fd) as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            if digest != args["expected_sha256"]:
                raise KernelError("content_changed", "L’empreinte du fichier ne correspond pas.")
        temporary = ".nilshell-" + uuid4().hex
        name = entry_name(path, parent_fd)
        temporary_name = temporary if parent_fd is not None else str(path.parent / temporary)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        existing_mode = None
        if args["overwrite"]:
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode):
                    raise KernelError(
                        "not_regular_file", "La destination n’est plus un fichier ordinaire."
                    )
                existing_mode = stat.S_IMODE(existing.st_mode) & 0o777
            except FileNotFoundError:
                pass
        created = False
        try:
            fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            created = True
            with os.fdopen(fd, "wb") as output:
                if existing_mode is not None and os.name == "posix":
                    os.fchmod(output.fileno(), existing_mode)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if args["overwrite"]:
                os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                # Atomic no-clobber publication; never replaces an existing destination.
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
        finally:
            try:
                if created:
                    os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        return {
            "path": str(path),
            "bytes_written": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def run_process(self, args):
        # Do not pass provider credentials or arbitrary host environment variables.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "HOME",
                "USER",
                "LOGNAME",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "TEMP",
                "TMP",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
            }
        }
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                args["argv"],
                cwd=args["cwd"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                start_new_session=os.name == "posix",
            )
            deadline = time.monotonic() + args["timeout_seconds"]
            reason = None
            while True:
                if (
                    os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
                    > args["max_output_bytes"]
                ):
                    reason = "output_limit"
                    break
                if process.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                time.sleep(0.02)
            # Clean up the process group as well as the direct child on POSIX.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait()
            stdout.seek(0)
            stderr.seek(0)
            raw_out = stdout.read(args["max_output_bytes"])
            raw_err = stderr.read(max(0, args["max_output_bytes"] - len(raw_out)))
            total = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
            return {
                "exit_code": process.returncode,
                "stdout": raw_out.decode("utf-8", errors="replace"),
                "stderr": raw_err.decode("utf-8", errors="replace"),
                "termination": reason,
                "truncated": total > len(raw_out) + len(raw_err),
            }
