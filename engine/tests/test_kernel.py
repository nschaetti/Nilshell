"""Kernel contract: approval, filesystem boundaries, atomic writes and processes."""

import hashlib
import json
import os
import sys
import threading

import pytest

from nilshell_engine.kernel import Kernel, KernelError
from nilshell_engine.rpc import Engine
from nilshell_engine.store import Store


@pytest.fixture
def kernel(tmp_path):
    return Kernel(Store(tmp_path / "state"))


def invoke(kernel, name, args):
    prepared = kernel.prepare(name, args)
    return kernel.execute(prepared["id"], True)["result"]


def test_catalog_and_arguments(kernel, tmp_path):
    assert {item["name"] for item in kernel.catalog()} == {
        "fs.stat",
        "fs.list",
        "fs.read",
        "fs.write",
        "fs.mkdir",
        "process.run",
    }
    for name, args, code in [
        ("unknown", {}, "unknown_syscall"),
        ("fs.read", {"path": "relative"}, "invalid_path"),
        ("fs.read", {"path": str(tmp_path), "extra": True}, "invalid_arguments"),
        ("fs.list", {"path": str(tmp_path), "limit": "100"}, "invalid_arguments"),
        (
            "process.run",
            {"argv": ["missing-nilshell-executable"], "cwd": str(tmp_path)},
            "executable_missing",
        ),
    ]:
        with pytest.raises(KernelError) as exc:
            kernel.prepare(name, args)
        assert exc.value.code == code


def test_approval_denial_expiry_and_replay(kernel, tmp_path, monkeypatch):
    destination = tmp_path / "no-touch"
    args = {"path": str(destination), "content": "secret payload"}
    request = kernel.prepare("fs.write", args)
    with pytest.raises(KernelError, match="refusé"):
        kernel.execute(request["id"], False)
    assert not destination.exists()
    with pytest.raises(KernelError) as exc:
        kernel.execute(request["id"], True)
    assert exc.value.code == "approval_required"
    request = kernel.prepare("fs.write", args)
    monkeypatch.setattr("nilshell_engine.kernel.time.monotonic", lambda: 10**12)
    with pytest.raises(KernelError) as exc:
        kernel.execute(request["id"], True)
    assert exc.value.code == "approval_expired"
    assert not destination.exists()


def test_only_one_concurrent_approval_wins(kernel, tmp_path):
    request = kernel.prepare("fs.write", {"path": str(tmp_path / "once"), "content": "once"})
    barrier = threading.Barrier(3)
    outcomes = []

    def attempt():
        barrier.wait()
        try:
            kernel.execute(request["id"], True)
            outcomes.append("ok")
        except KernelError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["approval_required", "ok"]


def test_file_operations_and_bounded_utf8(kernel, tmp_path):
    folder = tmp_path / "workspace"
    invoke(kernel, "fs.mkdir", {"path": str(folder)})
    path = folder / "text.txt"
    result = invoke(kernel, "fs.write", {"path": str(path), "content": "aéz"})
    assert result["sha256"] == hashlib.sha256("aéz".encode()).hexdigest()
    assert invoke(kernel, "fs.stat", {"path": str(path)})["kind"] == "file"
    read = invoke(kernel, "fs.read", {"path": str(path), "max_bytes": 2})
    assert read["content"] == "a" and read["truncated"]
    assert invoke(kernel, "fs.read", {"path": str(path)})["content"] == "aéz"
    entries = invoke(kernel, "fs.list", {"path": str(folder)})["entries"]
    assert entries[0]["name"] == "text.txt"
    with pytest.raises(KernelError) as exc:
        kernel.prepare("fs.write", {"path": str(path), "content": "replace"})
    assert exc.value.code == "already_exists"
    invoke(
        kernel,
        "fs.write",
        {
            "path": str(path),
            "content": "new",
            "overwrite": True,
            "expected_sha256": result["sha256"],
        },
    )
    assert path.read_text() == "new"
    with pytest.raises(KernelError) as exc:
        invoke(
            kernel,
            "fs.write",
            {
                "path": str(path),
                "content": "bad",
                "overwrite": True,
                "expected_sha256": result["sha256"],
            },
        )
    assert exc.value.code == "content_changed"
    assert path.read_text() == "new"
    assert not list(folder.glob(".nilshell-*"))


def test_directory_listing_limit_and_binary_file(kernel, tmp_path):
    for index in range(4):
        (tmp_path / str(index)).write_text("x")
    result = invoke(kernel, "fs.list", {"path": str(tmp_path), "limit": 2})
    assert len(result["entries"]) == 2 and result["truncated"]
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(KernelError) as exc:
        invoke(kernel, "fs.read", {"path": str(path)})
    assert exc.value.code == "operation_failed"


def test_changed_target_is_rejected(kernel, tmp_path):
    path = tmp_path / "file"
    path.write_text("original")
    request = kernel.prepare("fs.read", {"path": str(path)})
    path.write_text("changed content")
    with pytest.raises(KernelError) as exc:
        kernel.execute(request["id"], True)
    assert exc.value.code == "resource_changed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and FIFO behavior")
def test_symlink_resolution_and_non_regular_files(kernel, tmp_path):
    target = tmp_path / "target"
    target.write_text("original")
    link = tmp_path / "link"
    link.symlink_to(target)
    request = kernel.prepare("fs.read", {"path": str(link)})
    assert request["arguments"]["path"] == str(target)
    link.unlink()
    link.symlink_to("/etc/passwd")
    # Canonical arguments are frozen: replacing the original alias cannot redirect the read.
    assert kernel.execute(request["id"], True)["result"]["content"] == "original"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(KernelError):
        kernel.prepare("fs.read", {"path": str(fifo)})


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow traversal")
def test_directory_symlink_swap_after_validation_is_blocked(kernel, tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    target = folder / "file"
    target.write_text("safe")
    other = tmp_path / "other"
    other.mkdir()
    (other / "file").write_text("private")
    request = kernel.prepare("fs.read", {"path": str(target)})
    original = kernel.perform

    def race(name, args):
        folder.rename(tmp_path / "moved")
        folder.symlink_to(other, target_is_directory=True)
        return original(name, args)

    monkeypatch.setattr(kernel, "perform", race)
    with pytest.raises(KernelError) as exc:
        kernel.execute(request["id"], True)
    assert exc.value.code == "operation_failed"


def test_audit_omits_content_and_restarts_invalidate_tickets(kernel, tmp_path):
    request = kernel.prepare(
        "fs.write", {"path": str(tmp_path / "file"), "content": "private-body"}
    )
    journal = json.dumps(kernel.history())
    assert "private-body" not in journal and "pending" in journal
    restarted = Kernel(kernel.store)
    assert restarted.history()[0]["state"] == "interrupted"
    with pytest.raises(KernelError):
        restarted.execute(request["id"], True)


def test_command_argv_cwd_exit_code_and_environment(kernel, tmp_path, monkeypatch):
    monkeypatch.setenv("NILSHELL_PRIVATE_KEY", "super-secret")
    code = "import os,sys; print(os.getcwd()); print(sys.argv[1]); print(os.getenv('NILSHELL_PRIVATE_KEY')); print('err',file=sys.stderr); sys.exit(7)"
    result = invoke(
        kernel,
        "process.run",
        {"argv": [sys.executable, "-c", code, "a; $(touch nope)"], "cwd": str(tmp_path)},
    )
    assert result["exit_code"] == 7
    assert str(tmp_path) in result["stdout"]
    assert "a; $(touch nope)" in result["stdout"]
    assert "None" in result["stdout"] and "super-secret" not in result["stdout"]
    assert "err" in result["stderr"]
    assert not (tmp_path / "nope").exists()
    audit = json.dumps(kernel.history())
    assert code not in audit and "err" not in audit


def test_command_timeout_and_output_limit(kernel, tmp_path):
    result = invoke(
        kernel,
        "process.run",
        {
            "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
            "cwd": str(tmp_path),
            "timeout_seconds": 1,
        },
    )
    assert result["termination"] == "timeout"
    result = invoke(
        kernel,
        "process.run",
        {
            "argv": [sys.executable, "-c", "print('x' * 100000)"],
            "cwd": str(tmp_path),
            "max_output_bytes": 1024,
        },
    )
    assert result["truncated"]
    assert len(result["stdout"].encode()) + len(result["stderr"].encode()) <= 1024


def test_engine_rpc_exposes_kernel_without_unapproved_execution(tmp_path):
    engine = Engine(tmp_path / "state", lambda event: None)
    path = tmp_path / "file"
    request = engine.dispatch(
        "system.prepare",
        {"syscall": "fs.write", "arguments": {"path": str(path), "content": "test"}},
    )
    assert not path.exists()
    with pytest.raises(KernelError):
        engine.dispatch("system.execute", {"id": request["id"]})
    assert not path.exists()
    assert engine.dispatch("system.history", {})[0]["state"] == "denied"


def test_review_cannot_mutate_pending_arguments(kernel, tmp_path):
    path = tmp_path / "unchanged"
    request = kernel.prepare("fs.write", {"path": str(path), "content": "approved"})
    request["arguments"]["content"] = "substituted"
    kernel.execute(request["id"], True)
    assert path.read_text() == "approved"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_atomic_overwrite_preserves_existing_permissions(kernel, tmp_path):
    path = tmp_path / "script"
    path.write_text("original")
    path.chmod(0o750)
    invoke(kernel, "fs.write", {"path": str(path), "content": "new", "overwrite": True})
    assert path.stat().st_mode & 0o777 == 0o750


def test_temporary_name_collision_never_deletes_existing_file(kernel, tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "new-file"
    request = kernel.prepare("fs.write", {"path": str(path), "content": "test"})
    temporary = tmp_path / ".nilshell-collision"
    temporary.write_text("belongs-to-someone-else")
    monkeypatch.setattr("nilshell_engine.kernel.uuid4", lambda: SimpleNamespace(hex="collision"))
    with pytest.raises(KernelError):
        kernel.execute(request["id"], True)
    assert temporary.read_text() == "belongs-to-someone-else"
    assert not path.exists()
