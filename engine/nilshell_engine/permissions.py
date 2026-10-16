"""Composable rules for trusted, host-issued syscall identities."""

import hashlib
import json
import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError


@dataclass(frozen=True)
class Caller:
    """Identity supplied by the host, never by model-generated tool arguments."""

    assistant_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    tool_id: str = "desktop.console"


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal["eq", "in", "lte", "gte", "prefix"]
    value: JsonValue


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str | None = None
    name: str = Field(min_length=1, max_length=150)
    effect: Literal["allow", "deny", "ask"]
    scope: Literal["session", "permanent"] = "permanent"
    enabled: bool = True
    resource_hint: str | None = Field(default=None, max_length=8192)
    assistant_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    tool_id: str | None = None
    syscall: str | None = None
    directory: str | None = None
    recursive: bool = True
    command: str | None = None
    arguments: list[str] | None = None
    argument_prefix: list[str] | None = None
    parameters: dict[str, Condition] = Field(default_factory=dict)
    call_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def digest_call(syscall, args, caller):
    data = {"syscall": syscall, "arguments": args, "caller": asdict(caller)}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def equal(left, right):
    # JSON booleans are not numeric authorization values (True != 1 here).
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
    return left == right


def condition_matches(actual, condition):
    op, value = condition["op"], condition["value"]
    if op == "eq":
        return equal(actual, value)
    if op == "in":
        return any(equal(actual, candidate) for candidate in value)
    if op in {"lte", "gte"}:
        if type(actual) not in {int, float}:
            return False
        return actual <= value if op == "lte" else actual >= value
    if op == "prefix":
        if type(actual) is not type(value) or not isinstance(actual, (str, list)):
            return False
        return actual[: len(value)] == value
    return False


class Permissions:
    """Persist permanent rules; keep session grants only in engine memory."""

    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog
        self.session = {}
        self.lock = threading.RLock()
        with store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS permission_rules (id TEXT PRIMARY KEY, rule_json TEXT NOT NULL)"
            )

    def rules(self):
        with self.lock, self.store.connect() as db:
            permanent = [
                json.loads(row[0])
                for row in db.execute("SELECT rule_json FROM permission_rules ORDER BY rowid")
            ]
            return json.loads(json.dumps(permanent + list(self.session.values())))

    def save(self, values):
        try:
            rule = Rule.model_validate(values).model_dump()
        except ValidationError:
            raise ValueError("Règle de permission invalide.") from None
        for key in (
            "assistant_id",
            "provider_id",
            "model",
            "tool_id",
            "syscall",
            "directory",
            "command",
        ):
            if rule[key] is not None and not rule[key].strip():
                raise ValueError("Un critère vide doit être omis ou null.")
        syscall = rule["syscall"]
        if syscall is not None and syscall not in self.catalog:
            raise ValueError("Appel système inconnu dans la règle.")
        existing = next((item for item in self.rules() if item["id"] == rule["id"]), None)
        keep_directory = (
            not rule["enabled"] and existing and rule["directory"] == existing["directory"]
        )
        keep_command = not rule["enabled"] and existing and rule["command"] == existing["command"]
        if rule["directory"] and not keep_directory:
            try:
                path = Path(rule["directory"]).expanduser()
                if not path.is_absolute() or not path.resolve(strict=True).is_dir():
                    raise ValueError()
                rule["directory"] = str(path.resolve(strict=True))
            except (OSError, RuntimeError, ValueError):
                raise ValueError(
                    "Le répertoire de la règle doit être un dossier absolu existant."
                ) from None
        if rule["command"] and not keep_command:
            program = rule["command"]
            if not Path(program).is_absolute() and ("/" in program or "\\" in program):
                raise ValueError("Utilise un nom de programme ou un chemin absolu.")
            executable = which(program)
            if executable is None:
                raise ValueError("Programme introuvable pour la règle.")
            rule["command"] = str(Path(executable).resolve(strict=True))
        if (
            rule["command"] or rule["arguments"] is not None or rule["argument_prefix"] is not None
        ) and syscall not in {None, "process.run"}:
            raise ValueError("Les critères de commande requièrent process.run.")
        fields = set().union(
            *(
                model.model_fields
                for name, (model, _) in self.catalog.items()
                if syscall in {None, name}
            )
        )
        for key, condition in rule["parameters"].items():
            op, value = condition["op"], condition["value"]
            try:
                json.dumps(value, allow_nan=False)
            except (ValueError, TypeError):
                raise ValueError(
                    "Les conditions doivent contenir des valeurs JSON finies."
                ) from None
            if key not in fields:
                raise ValueError("Paramètre inconnu dans la règle.")
            if op in {"lte", "gte"} and (
                type(value) not in {int, float} or not math.isfinite(value)
            ):
                raise ValueError("Une borne numérique finie est requise.")
            if op == "in" and (not isinstance(value, list) or not value or len(value) > 100):
                raise ValueError("L’opérateur in requiert une liste de 1 à 100 valeurs.")
            if op == "prefix" and not isinstance(value, (str, list)):
                raise ValueError("Un préfixe texte ou liste est requis.")
        rule["id"] = rule["id"] or str(uuid4())
        with self.lock, self.store.connect() as db:
            # Scope changes cannot leave a second copy of the same rule active.
            db.execute("DELETE FROM permission_rules WHERE id=?", (rule["id"],))
            self.session.pop(rule["id"], None)
            if rule["scope"] == "session":
                self.session[rule["id"]] = rule
            else:
                db.execute(
                    "INSERT INTO permission_rules VALUES (?,?)", (rule["id"], json.dumps(rule))
                )
        return json.loads(json.dumps(rule))

    def delete(self, identifier):
        with self.lock, self.store.connect() as db:
            self.session.pop(identifier, None)
            db.execute("DELETE FROM permission_rules WHERE id=?", (identifier,))
        return True

    def matches(self, rule, syscall, args, caller):
        if not rule["enabled"] or rule["syscall"] not in {None, syscall}:
            return False
        for key, actual in asdict(caller).items():
            if rule[key] is not None and rule[key] != actual:
                return False
        if rule["call_digest"] and rule["call_digest"] != digest_call(syscall, args, caller):
            return False
        if rule["directory"]:
            resource = Path(args["cwd"] if syscall == "process.run" else args["path"])
            root = Path(rule["directory"])
            if rule["recursive"]:
                if not resource.is_relative_to(root):
                    return False
            elif syscall in {"fs.list", "process.run"}:
                if resource != root:
                    return False
            elif resource != root and resource.parent != root:
                return False
        command_criteria = (
            rule["command"] or rule["arguments"] is not None or rule["argument_prefix"] is not None
        )
        if command_criteria:
            if syscall != "process.run":
                return False
            if rule["command"] and args["argv"][0] != rule["command"]:
                return False
            arguments = args["argv"][1:]
            if rule["arguments"] is not None and arguments != rule["arguments"]:
                return False
            prefix = rule["argument_prefix"]
            if prefix is not None and arguments[: len(prefix)] != prefix:
                return False
        return all(
            key in args and condition_matches(args[key], value)
            for key, value in rule["parameters"].items()
        )

    def evaluate(self, syscall, args, caller):
        with self.lock:
            matches = [rule for rule in self.rules() if self.matches(rule, syscall, args, caller)]
        for effect in ("deny", "ask", "allow"):
            selected = [rule for rule in matches if rule["effect"] == effect]
            if selected:
                return {
                    "effect": effect,
                    "rule_ids": [rule["id"] for rule in selected],
                    "rule_names": [rule["name"] for rule in selected],
                }
        return {"effect": "ask", "rule_ids": [], "rule_names": []}

    def remember(self, syscall, args, caller, scope):
        # Convenience buttons never silently generalize to a directory or arbitrary arguments.
        return self.save(
            {
                "name": f"Accord exact · {syscall}",
                "effect": "allow",
                "scope": scope,
                **asdict(caller),
                "syscall": syscall,
                "call_digest": digest_call(syscall, args, caller),
                "resource_hint": args["argv"][0] if syscall == "process.run" else args["path"],
            }
        )
