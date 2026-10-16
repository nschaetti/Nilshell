"""JSON-lines RPC over inherited stdio; no local HTTP server or listening port."""

import json
import os
import sys
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

import keyring
from pydantic import BaseModel, Field

from . import providers
from .kernel import Kernel, KernelError
from .store import Store


class ProviderInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["openai", "ollama"]
    base_url: str
    api_key: str | None = None
    clear_key: bool = False


class AssistantInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    provider_id: str
    model: str = Field(min_length=1, max_length=300)
    system_prompt: str = Field(default="", max_length=100000)


class Engine:
    def __init__(self, directory, emit):
        self.store = Store(Path(directory))
        self.kernel = Kernel(self.store)
        self.emit = emit
        self.lock = threading.Lock()
        self.active = None
        self.session_keys = {}

    def get_key(self, identifier):
        if identifier in self.session_keys:
            return self.session_keys[identifier]
        try:
            return keyring.get_password("Nilshell", identifier) or ""
        except Exception:
            return ""

    def dispatch(self, method, params):
        if method == "system.catalog":
            return self.kernel.catalog()
        if method == "system.history":
            return self.kernel.history()
        if method == "system.prepare":
            return self.kernel.prepare(params["syscall"], params["arguments"])
        if method == "system.execute":
            return self.kernel.execute(params["id"], params.get("approved", False))
        if method == "bootstrap":
            return {
                "providers": [
                    {**p, "has_key": bool(self.get_key(p["id"]))}
                    for p in self.store.all("providers")
                ],
                "assistants": self.store.all("assistants"),
                "conversations": self.store.all("conversations"),
            }
        if method == "provider.save":
            data = ProviderInput.model_validate(params).model_dump()
            data["base_url"] = providers.validate_url(data["base_url"])
            data["id"] = data["id"] or str(uuid4())
            warning = None
            if data["api_key"] is not None or data["clear_key"]:
                value = "" if data["clear_key"] else data["api_key"]
                self.session_keys[data["id"]] = value
                try:
                    # Empty entries also replace previously saved credentials.
                    keyring.set_password("Nilshell", data["id"], value)
                except Exception:
                    warning = (
                        "Trousseau indisponible : clé conservée uniquement pour cette session."
                    )
            saved = self.store.save_provider(data)
            return {**saved, "has_key": bool(self.get_key(saved["id"])), "warning": warning}
        if method == "provider.models":
            provider = self.store.provider(params["id"])
            return providers.models(provider, self.get_key(provider["id"]))
        if method == "assistant.save":
            return self.store.save_assistant(AssistantInput.model_validate(params).model_dump())
        if method == "conversation.create":
            return self.store.new_conversation(params["assistant_id"])
        if method == "conversation.get":
            return self.store.conversation(params["id"])
        if method == "conversation.delete":
            with self.lock:
                if self.active and self.active[0] == params["id"]:
                    raise ValueError("Arrête d’abord la génération")
                self.store.delete_conversation(params["id"])
            return True
        if method == "chat.cancel":
            with self.lock:
                if self.active and self.active[0] == params["id"]:
                    self.active[1].set()
            return True
        if method == "chat.start":
            content = params["content"].strip()
            if not content or len(content) > 200000:
                raise ValueError("Message vide ou trop long")
            with self.lock:
                if self.active:
                    raise ValueError("Une génération est déjà en cours")
                conversation = self.store.conversation(params["id"])
                assistant = conversation["assistant"]
                provider = self.store.provider(assistant["provider_id"])
                cancel = threading.Event()
                self.store.add_message(params["id"], "user", content)
                message_id = self.store.add_message(params["id"], "assistant", "", "streaming")
                self.active = (params["id"], cancel)
                thread = threading.Thread(
                    target=self.generate,
                    args=(conversation, provider, content, message_id, cancel),
                    daemon=True,
                )
                thread.start()
            return {"message_id": message_id}
        raise ValueError("Méthode inconnue")

    def generate(self, conversation, provider, content, message_id, cancelled):
        identifier = conversation["id"]
        text = ""
        status = "complete"
        error = None
        assistant = conversation["assistant"]
        messages = [{"role": "system", "content": assistant["system_prompt"]}]
        messages += [
            {"role": m["role"], "content": m["content"]}
            for m in conversation["messages"]
            if m["status"] == "complete"
        ]
        messages.append({"role": "user", "content": content})
        try:
            for delta in providers.stream(
                provider, self.get_key(provider["id"]), assistant["model"], messages, cancelled
            ):
                text += delta
                # Persist each received delta so an engine crash preserves partial output.
                self.store.update_message(message_id, text, "streaming")
                self.emit(
                    {
                        "event": "chat.delta",
                        "conversation_id": identifier,
                        "message_id": message_id,
                        "delta": delta,
                    }
                )
            if cancelled.is_set():
                status = "interrupted"
        except Exception as exc:
            status = "error"
            error = str(exc) if isinstance(exc, providers.ProviderError) else "Erreur de génération"
        finally:
            self.store.update_message(message_id, text, status)
            with self.lock:
                self.active = None
            self.emit(
                {
                    "event": "chat.done",
                    "conversation_id": identifier,
                    "message_id": message_id,
                    "status": status,
                    "error": error,
                }
            )


def main():
    output_lock = threading.Lock()

    def emit(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    directory = os.environ.get("NILSHELL_DATA_DIR")
    if not directory:
        raise RuntimeError("NILSHELL_DATA_DIR must be supplied by the desktop host")
    engine = Engine(directory, emit)

    def handle(request):
        identifier = request.get("id")
        try:
            result = engine.dispatch(request["method"], request.get("params", {}))
            emit({"id": identifier, "result": result})
        except Exception as exc:
            # Validation errors can include input values: never serialize them.
            safe = (
                str(exc)
                if type(exc) is ValueError
                or isinstance(exc, (providers.ProviderError, KernelError))
                else "Opération impossible. Vérifie les paramètres et la connexion."
            )
            emit(
                {
                    "id": identifier,
                    "error": safe,
                    "error_code": exc.code if isinstance(exc, KernelError) else "operation_failed",
                }
            )

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError()
            threading.Thread(target=handle, args=(request,), daemon=True).start()
        except ValueError:
            emit({"id": None, "error": "Requête JSON invalide"})
