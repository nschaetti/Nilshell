"""Authorization tests exercise combinations, revocation and identity boundaries."""

import json
import sys
from dataclasses import replace

import pytest

from nilshell_engine.kernel import CATALOG, Kernel, KernelError
from nilshell_engine.permissions import Caller, Permissions
from nilshell_engine.rpc import Engine
from nilshell_engine.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "state")
    kernel = Kernel(store)
    directory = tmp_path / "docs"
    directory.mkdir()
    file = directory / "paper.txt"
    file.write_text("paper")
    caller = Caller("assistant-a", "provider-a", "model-a", "reader")
    return store, kernel, directory, file, caller


def save(kernel, **values):
    return kernel.permissions.save({"name": "Test", "effect": "allow", **values})


def decision(kernel, file, caller, **args):
    normalized, _ = kernel.normalize("fs.read", {"path": str(file), **args})
    return kernel.permissions.evaluate("fs.read", normalized, caller)["effect"]


def test_combined_identity_directory_and_parameters(setup, tmp_path):
    _, kernel, root, file, caller = setup
    save(
        kernel,
        assistant_id=caller.assistant_id,
        provider_id=caller.provider_id,
        model=caller.model,
        tool_id=caller.tool_id,
        syscall="fs.read",
        directory=str(root),
        parameters={"max_bytes": {"op": "lte", "value": 100}},
    )
    assert decision(kernel, file, caller, max_bytes=100) == "allow"
    assert decision(kernel, file, caller, max_bytes=101) == "ask"
    for field in ("assistant_id", "provider_id", "model", "tool_id"):
        assert decision(kernel, file, replace(caller, **{field: "other"}), max_bytes=100) == "ask"
    sibling = tmp_path / "docs-private"
    sibling.mkdir()
    other = sibling / "paper.txt"
    other.write_text("private")
    assert decision(kernel, other, caller, max_bytes=100) == "ask"


def test_deny_wins_and_cannot_be_overridden_by_confirmation(setup):
    _, kernel, _, file, caller = setup
    save(kernel)
    deny = save(kernel, effect="deny", syscall="fs.read", model=caller.model)
    assert decision(kernel, file, caller) == "deny"
    with pytest.raises(KernelError) as exc:
        kernel.prepare("fs.read", {"path": str(file)}, caller)
    assert exc.value.code == "policy_denied"
    assert kernel.history()[0]["state"] == "denied"
    kernel.permissions.delete(deny["id"])
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    deny = save(kernel, effect="deny")
    with pytest.raises(KernelError) as exc:
        kernel.execute(request["id"], True, "permanent")
    assert exc.value.code == "policy_denied"
    assert len(kernel.permissions.rules()) == 2


def test_explicit_ask_wins_over_allow(setup):
    _, kernel, _, file, caller = setup
    save(kernel)
    save(kernel, effect="ask", tool_id=caller.tool_id)
    assert decision(kernel, file, caller) == "ask"
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    with pytest.raises(KernelError):
        kernel.execute(request["id"])
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    assert kernel.execute(request["id"], True)["result"]["content"] == "paper"


def test_allow_needs_no_confirmation_but_can_be_refused(setup):
    _, kernel, _, file, caller = setup
    save(kernel, syscall="fs.read")
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    assert request["permission"]["effect"] == "allow"
    assert kernel.execute(request["id"])["result"]["content"] == "paper"
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    with pytest.raises(KernelError):
        kernel.execute(request["id"], False)


def test_disable_and_revoke_pending_requests_are_rechecked(setup):
    _, kernel, _, file, caller = setup
    rule = save(kernel, syscall="fs.read")
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    kernel.permissions.save({**rule, "enabled": False})
    with pytest.raises(KernelError):
        kernel.execute(request["id"])
    kernel.permissions.save({**rule, "enabled": True})
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    kernel.permissions.delete(rule["id"])
    with pytest.raises(KernelError):
        kernel.execute(request["id"])


@pytest.mark.parametrize("scope", ["session", "permanent"])
def test_remember_is_exact_and_session_lifetime(setup, scope):
    store, kernel, _, file, caller = setup
    request = kernel.prepare("fs.read", {"path": str(file), "max_bytes": 100}, caller)
    kernel.execute(request["id"], True, scope)
    assert decision(kernel, file, caller, max_bytes=100) == "allow"
    assert decision(kernel, file, caller, max_bytes=101) == "ask"
    assert decision(kernel, file, replace(caller, tool_id="other"), max_bytes=100) == "ask"
    # Raw parameters/content are not copied into remembered rules.
    rule = kernel.permissions.rules()[0]
    assert rule["call_digest"] and rule["parameters"] == {}
    restarted = Kernel(store)
    assert decision(restarted, file, caller, max_bytes=100) == (
        "allow" if scope == "permanent" else "ask"
    )


def test_once_never_creates_rule_and_remember_requires_explicit_approval(setup):
    _, kernel, _, file, caller = setup
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    kernel.execute(request["id"], True)
    assert kernel.permissions.rules() == []
    save(kernel)
    request = kernel.prepare("fs.read", {"path": str(file)}, caller)
    with pytest.raises(KernelError):
        kernel.execute(request["id"], None, "permanent")
    assert len(kernel.permissions.rules()) == 1


def test_recursive_boundary_and_canonical_symlink(setup, tmp_path):
    _, kernel, root, file, caller = setup
    save(kernel, directory=str(root), recursive=False)
    assert decision(kernel, file, caller) == "allow"
    child = root / "nested"
    child.mkdir()
    nested = child / "file"
    nested.write_text("nested")
    assert decision(kernel, nested, caller) == "ask"
    if sys.platform != "win32":
        outside = tmp_path / "private"
        outside.write_text("private")
        link = root / "alias"
        link.symlink_to(outside)
        assert decision(kernel, link, caller) == "ask"


def test_command_executable_argv_and_cwd_combine(setup):
    _, kernel, root, _, caller = setup
    save(
        kernel,
        syscall="process.run",
        command=sys.executable,
        directory=str(root),
        arguments=["--version"],
        parameters={"timeout_seconds": {"op": "lte", "value": 10}},
    )

    def check(argv, **extra):
        args, _ = kernel.normalize(
            "process.run", {"argv": argv, "cwd": str(root), "timeout_seconds": 10, **extra}
        )
        return kernel.permissions.evaluate("process.run", args, caller)["effect"]

    assert check([sys.executable, "--version"]) == "allow"
    assert check([sys.executable, "-c", "print(1)"]) == "ask"
    assert check([sys.executable, "--version"], timeout_seconds=11) == "ask"
    assert check([sys.executable, "--version"], cwd=str(root.parent)) == "ask"
    kernel.permissions.delete(kernel.permissions.rules()[0]["id"])
    save(kernel, syscall="process.run", command=sys.executable, argument_prefix=["-c"])
    assert check([sys.executable, "-c", "print(1)"]) == "allow"
    assert check([sys.executable, "--version"]) == "ask"


@pytest.mark.parametrize(
    "parameters,args,expected",
    [
        ({"max_bytes": {"op": "eq", "value": 10}}, {"max_bytes": 10}, "allow"),
        ({"max_bytes": {"op": "in", "value": [10, 20]}}, {"max_bytes": 15}, "ask"),
        ({"max_bytes": {"op": "gte", "value": 10}}, {"max_bytes": 20}, "allow"),
        ({"max_bytes": {"op": "eq", "value": True}}, {"max_bytes": 1}, "ask"),
        ({"path": {"op": "prefix", "value": "/nonexistent"}}, {}, "ask"),
    ],
)
def test_parameter_operators(setup, parameters, args, expected):
    _, kernel, _, file, caller = setup
    save(kernel, syscall="fs.read", parameters=parameters)
    assert decision(kernel, file, caller, **args) == expected


@pytest.mark.parametrize(
    "values",
    [
        {"syscall": "typo"},
        {"effect": "typo"},
        {"directory": ""},
        {"command": ""},
        {"parameters": {"max_byes": {"op": "lte", "value": 10}}},
        {"parameters": {"max_bytes": {"op": "lte", "value": True}}},
        {"parameters": {"max_bytes": {"op": "in", "value": 10}}},
        {"parameters": {"path": {"op": "prefix", "value": 10}}},
        {"syscall": "fs.read", "command": sys.executable},
        {"unexpected": True},
        {"scope": "once"},
    ],
)
def test_invalid_rules_never_become_wildcards(setup, values):
    _, kernel, _, _, _ = setup
    with pytest.raises(ValueError):
        save(kernel, **values)
    assert kernel.permissions.rules() == []


def test_scope_changes_do_not_duplicate_rules(setup):
    store, kernel, _, _, _ = setup
    rule = save(kernel, scope="session")
    kernel.permissions.save({**rule, "scope": "permanent"})
    assert len(kernel.permissions.rules()) == 1
    assert len(Permissions(store, CATALOG).rules()) == 1
    kernel.permissions.save({**rule, "scope": "session"})
    assert len(kernel.permissions.rules()) == 1
    assert Permissions(store, CATALOG).rules() == []


def test_desktop_derives_model_and_provider_from_assistant(tmp_path):
    engine = Engine(tmp_path / "state", lambda event: None)
    provider = engine.store.save_provider(
        {"name": "p", "kind": "openai", "base_url": "https://example.test"}
    )
    assistant = engine.store.save_assistant(
        {"name": "a", "provider_id": provider["id"], "model": "real-model", "system_prompt": ""}
    )
    engine.kernel.permissions.save({"name": "Spoof guard", "effect": "deny", "model": "real-model"})
    file = tmp_path / "file"
    file.write_text("test")
    with pytest.raises(KernelError):
        engine.dispatch(
            "system.prepare",
            {
                "syscall": "fs.read",
                "arguments": {"path": str(file)},
                "assistant_id": assistant["id"],
                "model": "fake-model",
                "provider_id": "fake",
            },
        )
    row = engine.kernel.history()[0]
    assert json.loads(row["caller_json"])["model"] == "real-model"
    assert json.loads(row["caller_json"])["provider_id"] == provider["id"]
    assert json.loads(row["permission_json"])["effect"] == "deny"


def test_legacy_database_is_migrated_without_losing_history(tmp_path):
    store = Store(tmp_path / "state")
    with store.connect() as db:
        db.execute(
            "CREATE TABLE system_calls (id TEXT PRIMARY KEY, syscall TEXT, resource TEXT, state TEXT, code TEXT, duration_ms INTEGER, created_at TEXT)"
        )
        db.execute(
            "INSERT INTO system_calls VALUES ('old','fs.read','/old','complete',NULL,1,'2026-01-01')"
        )
    kernel = Kernel(store)
    assert kernel.history()[0]["id"] == "old"
    assert "permission_json" in kernel.history()[0]


def test_nonrecursive_grant_cannot_list_grandchildren(setup):
    _, kernel, root, _, caller = setup
    child = root / "child"
    child.mkdir()
    save(kernel, directory=str(root), recursive=False)
    for directory, expected in [(root, "allow"), (child, "ask")]:
        args, _ = kernel.normalize("fs.list", {"path": str(directory)})
        assert kernel.permissions.evaluate("fs.list", args, caller)["effect"] == expected


@pytest.mark.parametrize("value", [float("nan"), [float("inf")], {"x": float("-inf")}])
def test_nonfinite_json_is_rejected(setup, value):
    _, kernel, _, _, _ = setup
    with pytest.raises(ValueError):
        save(kernel, parameters={"max_bytes": {"op": "eq", "value": value}})
    assert kernel.permissions.rules() == []


def test_rule_can_be_disabled_after_directory_disappears(setup):
    _, kernel, root, file, _ = setup
    rule = save(kernel, directory=str(root))
    file.unlink()
    root.rmdir()
    disabled = kernel.permissions.save({**rule, "enabled": False})
    assert disabled["enabled"] is False
    assert kernel.permissions.delete(rule["id"])
