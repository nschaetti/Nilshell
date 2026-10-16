import json
import threading

import httpx
import pytest

from nilshell_engine import providers
from nilshell_engine.rpc import Engine
from nilshell_engine.store import Store


def make_assistant(store):
    provider = store.save_provider(
        {"name": "Test", "kind": "openai", "base_url": "https://example.test/v1"}
    )
    return store.save_assistant(
        {
            "name": "Ada",
            "provider_id": provider["id"],
            "model": "test-model",
            "system_prompt": "Be precise.",
        }
    )


def test_persistence_snapshot_and_delete(tmp_path):
    store = Store(tmp_path)
    assistant = make_assistant(store)
    conversation = store.new_conversation(assistant["id"])
    store.save_assistant({**assistant, "system_prompt": "Changed"})
    store.add_message(conversation["id"], "user", "Hello")
    message = store.add_message(conversation["id"], "assistant", "Partial", "streaming")
    reloaded = Store(tmp_path).conversation(conversation["id"])
    assert reloaded["assistant"]["system_prompt"] == "Be precise."
    assert reloaded["title"] == "Hello"
    assert reloaded["messages"][-1]["status"] == "interrupted"
    store.update_message(message, "Complete", "complete")
    store.delete_conversation(conversation["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind,body",
    [
        (
            "openai",
            'data: {"choices":[{"delta":{"content":"Bonjour"},"finish_reason":null}]}\n\ndata: [DONE]\n\n',
        ),
        ("ollama", '{"message":{"content":"Bonjour"},"done":false}\n{"done":true}\n'),
    ],
)
def test_stream_protocols(kind, body):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=body)

    provider = {"kind": kind, "base_url": "https://example.test/v1"}
    text = "".join(
        providers.stream(
            provider,
            "secret",
            "model",
            [],
            threading.Event(),
            transport=httpx.MockTransport(respond),
        )
    )
    assert text == "Bonjour"
    assert requests[0].headers["Authorization"] == "Bearer secret"
    assert requests[0].url.path.endswith("/api/chat" if kind == "ollama" else "/chat/completions")


def test_errors_do_not_leak_response_body():
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="private-key"))
    with pytest.raises(providers.ProviderError) as exc:
        providers.models(
            {"kind": "openai", "base_url": "https://example.test"}, "private-key", transport
        )
    assert "private-key" not in str(exc.value)


def test_truncated_stream_is_error():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: {"choices":[]}\n')
    )
    with pytest.raises(providers.ProviderError):
        list(
            providers.stream(
                {"kind": "openai", "base_url": "https://example.test"},
                "",
                "model",
                [],
                threading.Event(),
                transport,
            )
        )


def test_cancelled_stream_has_no_delta():
    cancel = threading.Event()
    cancel.set()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="data: [DONE]\n"))
    assert (
        list(
            providers.stream(
                {"kind": "openai", "base_url": "https://example.test"},
                "",
                "model",
                [],
                cancel,
                transport,
            )
        )
        == []
    )


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "https://user:pass@example.test", "https://example.test?key=secret"],
)
def test_reject_unsafe_base_url(url):
    with pytest.raises(ValueError):
        providers.validate_url(url)


def test_secret_fallback_never_persists_key(tmp_path, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("No keyring")

    monkeypatch.setattr("nilshell_engine.rpc.keyring.set_password", unavailable)
    monkeypatch.setattr("nilshell_engine.rpc.keyring.get_password", unavailable)
    engine = Engine(tmp_path, lambda event: None)
    result = engine.dispatch(
        "provider.save",
        {
            "name": "Test",
            "kind": "openai",
            "base_url": "https://example.test",
            "api_key": "secret-value",
        },
    )
    assert result["warning"]
    assert engine.get_key(result["id"]) == "secret-value"
    assert b"secret-value" not in engine.store.path.read_bytes()
    assert "secret-value" not in json.dumps(engine.dispatch("bootstrap", {}))


def test_chat_lifecycle_and_concurrent_rejection(tmp_path, monkeypatch):
    events = []
    started = threading.Event()
    release = threading.Event()
    done = threading.Event()
    captured = []

    def emit(event):
        events.append(event)
        if event["event"] == "chat.done":
            done.set()

    def fake_stream(provider, key, model, messages, cancel):
        captured.extend(messages)
        started.set()
        release.wait(3)
        yield "Hello"

    monkeypatch.setattr(providers, "stream", fake_stream)
    monkeypatch.setattr("nilshell_engine.rpc.keyring.get_password", lambda *args: None)
    engine = Engine(tmp_path, emit)
    assistant = make_assistant(engine.store)
    conversation = engine.dispatch("conversation.create", {"assistant_id": assistant["id"]})
    engine.dispatch("chat.start", {"id": conversation["id"], "content": "Hi"})
    assert started.wait(2)
    with pytest.raises(ValueError):
        engine.dispatch("chat.start", {"id": conversation["id"], "content": "Again"})
    with pytest.raises(ValueError):
        engine.dispatch("conversation.delete", {"id": conversation["id"]})
    release.set()
    assert done.wait(3)
    assert captured == [
        {"role": "system", "content": "Be precise."},
        {"role": "user", "content": "Hi"},
    ]
    result = engine.store.conversation(conversation["id"])
    assert result["messages"][-1]["content"] == "Hello"
    assert result["messages"][-1]["status"] == "complete"
    assert events[-1]["event"] == "chat.done"
