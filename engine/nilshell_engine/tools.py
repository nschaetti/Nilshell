"""Tool registry and a fail-closed Linux sandbox connected to the syscall broker."""

import ast
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from shutil import which
from uuid import uuid4

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field
from referencing import Registry
from referencing.exceptions import NoSuchResource

MAX_LINE = 1048576
MAX_RESULT = 262144


def refuse_remote_schema(uri):
    raise NoSuchResource(ref=uri)


class ToolError(Exception):
    """An error safe to return as a tool result."""


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str | None = None
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=5000)
    code: str = Field(min_length=1, max_length=65536)
    parameters: dict
    enabled: bool = True


class ToolRegistry:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("UPDATE tool_runs SET state='interrupted' WHERE state='running'")

    def all(self):
        with self.store.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT definition FROM python_tools ORDER BY rowid")
            ]

    def get(self, identifier):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT definition FROM python_tools WHERE id=?", (identifier,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, values):
        try:
            data = ToolInput.model_validate(values).model_dump()
            schema = data["parameters"]
            if schema.get("type") != "object":
                raise ValueError()

            # External schema retrieval is never permitted in tool definitions.
            def check_refs(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"$ref", "$dynamicRef"} and (
                            not isinstance(item, str) or not item.startswith("#")
                        ):
                            raise ValueError()
                        check_refs(item)
                elif isinstance(value, list):
                    for item in value:
                        check_refs(item)

            check_refs(schema)
            Draft202012Validator.check_schema(schema)
            tree = ast.parse(data["code"])
            if not any(
                isinstance(node, ast.FunctionDef) and node.name == "run" for node in tree.body
            ):
                raise ValueError()
            encoded = json.dumps(data, allow_nan=False)
            if len(encoded) > 200000:
                raise ValueError()
        except Exception:
            raise ValueError(
                "Outil invalide : nom, schéma JSON objet et fonction run(arguments, system) requis."
            ) from None
        data["id"] = data["id"] or str(uuid4())
        with self.store.connect() as db:
            existing = db.execute(
                "SELECT id FROM python_tools WHERE name=?", (data["name"],)
            ).fetchone()
            if existing and existing[0] != data["id"]:
                raise ValueError("Ce nom d’outil existe déjà.")
            db.execute(
                """INSERT INTO python_tools VALUES (?,?,?) ON CONFLICT(id) DO UPDATE
                SET name=excluded.name,definition=excluded.definition""",
                (data["id"], data["name"], json.dumps(data)),
            )
        return data

    def delete(self, identifier):
        with self.store.connect() as db:
            db.execute("DELETE FROM python_tools WHERE id=?", (identifier,))
        return True

    def validate_arguments(self, tool, args):
        if not isinstance(args, dict) or len(json.dumps(args, allow_nan=False)) > 65536:
            raise ToolError("Arguments d’outil invalides ou trop volumineux.")
        try:
            Draft202012Validator(
                tool["parameters"], registry=Registry(retrieve=refuse_remote_schema)
            ).validate(args)
        except Exception:
            raise ToolError("Les arguments ne respectent pas le schéma de l’outil.") from None

    def start_run(self, conversation_id, message_id, tool, arguments):
        identifier = str(uuid4())
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO tool_runs(id,conversation_id,message_id,tool_id,name,arguments,state) VALUES (?,?,?,?,?,?,'running')",
                (
                    identifier,
                    conversation_id,
                    message_id,
                    tool["id"],
                    tool["name"],
                    json.dumps(arguments),
                ),
            )
        return identifier

    def finish_run(self, identifier, result, state):
        with self.store.connect() as db:
            db.execute(
                "UPDATE tool_runs SET state=?,result=? WHERE id=?",
                (state, json.dumps(result), identifier),
            )


def sandbox_command():
    if sys.platform != "linux" or not which("bwrap"):
        raise ToolError(
            "Outils Python : Linux et bubblewrap sont requis (sudo apt install bubblewrap)."
        )
    command = [
        which("bwrap"),
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--ro-bind-try",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        "268435456",
        "--tmpfs",
        "/tmp",
        "--size",
        "33554432",
        "--tmpfs",
        "/work",
        "--chdir",
        "/work",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "HOME",
        "/work",
        "--setenv",
        "PYTHONIOENCODING",
        "utf-8",
    ]
    if getattr(sys, "frozen", False):
        command += [
            "--ro-bind",
            sys.executable,
            "/runner",
            "--remount-ro",
            "/",
            "--remount-ro",
            "/dev",
            "--remount-ro",
            "/proc",
            "/runner",
            "--tool-runner",
        ]
    else:
        prefix = str(Path(sys.base_prefix).resolve())
        if prefix != "/usr":
            command += ["--ro-bind", prefix, prefix]
        worker = str(Path(__file__).with_name("tool_worker.py"))
        interpreter = str(Path(sys._base_executable).resolve())
        command += [
            "--ro-bind",
            worker,
            "/runner.py",
            "--remount-ro",
            "/",
            "--remount-ro",
            "/dev",
            "--remount-ro",
            "/proc",
            interpreter,
            "-I",
            "-S",
            "/runner.py",
        ]
    return command


class Sandbox:
    def status(self):
        try:
            result = self.run(
                {"code": "def run(arguments, system):\n    return 'ready'"},
                {},
                lambda *args: None,
                threading.Event(),
                timeout=8,
                diagnostics=True,
            )
            return {"available": result == "ready", "message": "Sandbox Linux opérationnel."}
        except ToolError as exc:
            return {"available": False, "message": str(exc)}

    def run(self, tool, arguments, syscall, cancelled, timeout=60, diagnostics=False):
        command = sandbox_command()
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=environment,
                start_new_session=True,
            )
            lines = queue.Queue(maxsize=8)
            stop_reader = threading.Event()

            def read():
                while not stop_reader.is_set():
                    line = process.stdout.readline(MAX_LINE + 1)
                    while not stop_reader.is_set():
                        try:
                            lines.put(line, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    if not line or len(line) > MAX_LINE:
                        return

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            try:
                payload = json.dumps(
                    {"code": tool["code"], "arguments": arguments}, ensure_ascii=True
                )
                if len(payload) > MAX_LINE:
                    raise ToolError("Entrée d’outil trop volumineuse.")
                process.stdin.write((payload + "\n").encode())
                process.stdin.flush()
                while True:
                    if cancelled.is_set():
                        raise ToolError("Outil interrompu.")
                    if time.monotonic() > deadline or os.fstat(errors.fileno()).st_size > 2097152:
                        raise ToolError("Limite de temps ou de sortie de l’outil atteinte.")
                    try:
                        line = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if not line:
                        if diagnostics:
                            errors.seek(0)
                            detail = errors.read(2000).decode("utf-8", errors="replace").strip()
                            raise ToolError(
                                "Sandbox Linux indisponible : " + (detail or "processus arrêté")
                            )
                        raise ToolError(
                            "Sandbox impossible à démarrer ou outil arrêté. Vérifie bubblewrap et les espaces de noms utilisateur ; aucun repli sans isolation n’est autorisé."
                        )
                    if len(line) > MAX_LINE:
                        raise ToolError("Message d’outil trop volumineux.")
                    try:
                        message = json.loads(line)
                    except (ValueError, TypeError):
                        raise ToolError("Protocole d’outil invalide.") from None
                    if not isinstance(message, dict):
                        raise ToolError("Protocole d’outil invalide.")
                    if message.get("type") == "syscall" and set(message) == {
                        "type",
                        "syscall",
                        "arguments",
                    }:
                        started = time.monotonic()
                        try:
                            result = syscall(
                                message["syscall"],
                                message["arguments"],
                                lambda: process.poll() is None,
                            )
                            reply = {"ok": True, "result": result}
                        except Exception as exc:
                            reply = {
                                "ok": False,
                                "error": str(exc)
                                if isinstance(exc, (ToolError, ValueError))
                                else "Appel système refusé ou impossible.",
                            }
                        deadline += time.monotonic() - started
                        response = json.dumps(reply, ensure_ascii=True)
                        if len(response) > MAX_LINE:
                            response = json.dumps(
                                {"ok": False, "error": "Résultat système trop volumineux."}
                            )
                        process.stdin.write((response + "\n").encode())
                        process.stdin.flush()
                    elif message.get("type") == "result" and set(message) == {"type", "value"}:
                        if len(line) > MAX_RESULT:
                            raise ToolError("Résultat d’outil limité à 256 Kio.")
                        json.dumps(message["value"], allow_nan=False)
                        return message["value"]
                    elif message.get("type") == "error":
                        raise ToolError(str(message.get("error", "Erreur Python"))[:600])
                    else:
                        raise ToolError("Protocole d’outil invalide.")
            except (BrokenPipeError, OSError, ValueError):
                raise ToolError("Sandbox indisponible ou canal d’outil interrompu.") from None
            finally:
                stop_reader.set()
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                process.stdin.close()
                reader.join(timeout=2)
                process.stdout.close()
