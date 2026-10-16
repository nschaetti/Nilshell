"""JSON-lines RPC over inherited stdio; no local HTTP server or listening port."""

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

import keyring
from pydantic import BaseModel, Field

from . import providers
from .kernel import Kernel, KernelError
from .permissions import Caller
from .store import Store
from .tools import Sandbox, ToolError, ToolRegistry


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
    tool_ids: list[str] = Field(default_factory=list, max_length=16)


class Engine:
    def __init__(self, directory, emit):
        self.store = Store(Path(directory))
        self.kernel = Kernel(self.store)
        self.tools = ToolRegistry(self.store)
        self.sandbox = Sandbox()
        self.approvals = {}
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
        if method == "tools.list":
            return self.tools.all()
        if method == "tools.save":
            return self.tools.save(params)
        if method == "tools.delete":
            return self.tools.delete(params["id"])
        if method == "tools.runtime":
            return self.sandbox.status()
        if method == "chat.approve":
            if type(params.get("approved")) is not bool or params.get("remember", "once") not in {
                "once",
                "session",
                "permanent",
            }:
                raise ValueError("Décision d’autorisation invalide.")
            with self.lock:
                pending = self.approvals.get(params["id"])
                if pending is None or pending["decision"] is not None:
                    raise ValueError("Cette demande n’est plus en attente.")
                pending["decision"] = (params["approved"], params.get("remember", "once"))
            return True
        if method == "permissions.list":
            return self.kernel.permissions.rules()
        if method == "permissions.save":
            return self.kernel.permissions.save(params)
        if method == "permissions.delete":
            return self.kernel.permissions.delete(params["id"])
        if method == "system.catalog":
            return self.kernel.catalog()
        if method == "system.history":
            return self.kernel.history()
        if method == "system.prepare":
            # This selector belongs to the trusted desktop console. Future tools
            # must use a separate host-issued identity, not this RPC method.
            assistant_id = params.get("assistant_id")
            caller = Caller(tool_id=params.get("tool_id", "desktop.console"))
            if (
                not isinstance(caller.tool_id, str)
                or not caller.tool_id.strip()
                or len(caller.tool_id) > 150
            ):
                raise ValueError("Identifiant d’outil invalide.")
            if assistant_id:
                assistant = next(
                    (a for a in self.store.all("assistants") if a["id"] == assistant_id), None
                )
                if assistant is None:
                    raise ValueError("Assistant introuvable.")
                caller = Caller(
                    assistant_id=assistant["id"],
                    provider_id=assistant["provider_id"],
                    model=assistant["model"],
                    tool_id=caller.tool_id,
                )
            return self.kernel.prepare(params["syscall"], params["arguments"], caller)
        if method == "system.execute":
            return self.kernel.execute(
                params["id"], params.get("approved"), params.get("remember", "once")
            )
        if method == "bootstrap":
            return {
                "providers": [
                    {**p, "has_key": bool(self.get_key(p["id"]))}
                    for p in self.store.all("providers")
                ],
                "assistants": self.store.all("assistants"),
                "conversations": self.store.all("conversations"),
                "tools": self.tools.all(),
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

    def request_syscall(
        self, conversation_id, run_id, tool, caller, syscall, arguments, cancelled, alive=None
    ):
        current = self.tools.get(tool["id"])
        if current is None or not current["enabled"] or current != tool:
            raise ToolError("Outil désactivé ou modifié pendant l’exécution.")
        if cancelled.is_set():
            raise ToolError("Outil interrompu.")
        try:
            prepared = self.kernel.prepare(syscall, arguments, caller)
        except KernelError as exc:
            raise ToolError(str(exc)) from None
        approved, remember = None, "once"
        try:
            if prepared["permission"]["effect"] != "allow":
                pending = {"decision": None}
                with self.lock:
                    self.approvals[prepared["id"]] = pending
                self.emit(
                    {
                        "event": "chat.approval",
                        "conversation_id": conversation_id,
                        "run_id": run_id,
                        "request": prepared,
                    }
                )
                started = time.monotonic()
                while not cancelled.wait(0.1):
                    with self.lock:
                        decision = pending["decision"]
                    if decision is not None:
                        approved, remember = decision
                        break
                    if (alive is not None and not alive()) or time.monotonic() - started >= 300:
                        approved = False
                        break
                if cancelled.is_set():
                    approved = False
            current = self.tools.get(tool["id"])
            if (
                cancelled.is_set()
                or (alive is not None and not alive())
                or current is None
                or not current["enabled"]
                or current != tool
            ):
                approved = False
            return self.kernel.execute(prepared["id"], approved, remember)["result"]
        except KernelError as exc:
            raise ToolError(str(exc)) from None
        finally:
            with self.lock:
                self.approvals.pop(prepared["id"], None)
            self.emit(
                {
                    "event": "chat.approval_done",
                    "conversation_id": conversation_id,
                    "request_id": prepared["id"],
                }
            )

    def run_tool(self, conversation_id, message_id, tool, arguments, cancelled):
        run_id = self.tools.start_run(conversation_id, message_id, tool, arguments)
        self.emit(
            {
                "event": "chat.tool",
                "conversation_id": conversation_id,
                "run_id": run_id,
                "name": tool["name"],
                "tool_id": tool["id"],
                "arguments": arguments,
                "state": "running",
            }
        )
        try:
            self.tools.validate_arguments(tool, arguments)
            current = self.tools.get(tool["id"])
            if current is None or not current["enabled"] or current != tool:
                raise ToolError("Outil désactivé, supprimé ou modifié depuis le début du tour.")
            assistant = self.store.conversation(conversation_id)["assistant"]
            caller = Caller(
                assistant_id=assistant["id"],
                provider_id=assistant["provider_id"],
                model=assistant["model"],
                tool_id=tool["id"],
            )
            calls = 0

            def syscall(name, values, alive=None):
                nonlocal calls
                calls += 1
                if calls > 32:
                    raise ToolError("Limite de 32 appels système par exécution d’outil.")
                return self.request_syscall(
                    conversation_id, run_id, tool, caller, name, values, cancelled, alive
                )

            result = self.sandbox.run(tool, arguments, syscall, cancelled)
            state = "complete"
        except Exception as exc:
            result = {
                "error": str(exc) if isinstance(exc, ToolError) else "Échec de l’outil Python."
            }
            state = "interrupted" if cancelled.is_set() else "error"
        self.tools.finish_run(run_id, result, state)
        self.emit(
            {
                "event": "chat.tool",
                "conversation_id": conversation_id,
                "run_id": run_id,
                "name": tool["name"],
                "tool_id": tool["id"],
                "arguments": arguments,
                "result": result,
                "state": state,
            }
        )
        return result

    def generate(self, conversation, provider, content, message_id, cancelled):
        identifier = conversation["id"]
        text = ""
        status = "complete"
        error = None
        assistant = conversation["assistant"]
        messages = [{"role": "system", "content": assistant["system_prompt"]}]
        for message in conversation["messages"]:
            if message["status"] == "complete":
                if message.get("protocol_json"):
                    messages.extend(json.loads(message["protocol_json"]))
                else:
                    messages.append({"role": message["role"], "content": message["content"]})
            elif message["role"] == "assistant":
                runs = [
                    row
                    for row in conversation.get("tool_runs", [])
                    if row["message_id"] == message["id"]
                ]
                if runs:
                    report = [
                        {"tool": row["name"], "state": row["state"], "result": row["result"]}
                        for row in runs
                    ]
                    messages.append(
                        {
                            "role": "assistant",
                            "content": "Tour précédent interrompu ou en erreur. Des outils peuvent déjà avoir agi ; ne répète pas leurs actions sans vérifier. Journal : "
                            + json.dumps(report, ensure_ascii=False)[:131072],
                        }
                    )
        messages.append({"role": "user", "content": content})
        enabled = {
            tool["name"]: tool
            for tool in self.tools.all()
            if tool["id"] in assistant.get("tool_ids", []) and tool["enabled"]
        }
        specifications = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in enabled.values()
        ]
        protocol = []
        executions = 0
        try:
            for round_index in range(8):
                if cancelled.is_set():
                    break
                turn = None
                if specifications:
                    events = providers.stream_turn(
                        provider,
                        self.get_key(provider["id"]),
                        assistant["model"],
                        messages,
                        cancelled,
                        specifications,
                    )
                else:
                    events = (
                        {"text": delta}
                        for delta in providers.stream(
                            provider,
                            self.get_key(provider["id"]),
                            assistant["model"],
                            messages,
                            cancelled,
                        )
                    )
                for event in events:
                    if "message" in event:
                        turn = event["message"]
                    if "text" in event:
                        delta = event["text"]
                        text += delta
                        if len(text) > 200000:
                            raise providers.ProviderError("Réponse trop volumineuse.")
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
                    break
                if turn is None:
                    turn = {"role": "assistant", "content": text}
                protocol.append(turn)
                messages.append(turn)
                calls = turn.get("tool_calls", [])
                if not calls:
                    break
                for call in calls:
                    executions += 1
                    function = call["function"]
                    tool = enabled.get(function["name"])
                    try:
                        arguments = function["arguments"]
                        if isinstance(arguments, str):
                            arguments = providers.strict_json(arguments)
                        if tool is None:
                            raise ToolError("Outil non autorisé pour cet assistant.")
                        if executions > 32 or round_index == 7:
                            raise ToolError("Limite d’appels d’outils atteinte.")
                        if cancelled.is_set():
                            raise ToolError("Outil interrompu.")
                        result = self.run_tool(identifier, message_id, tool, arguments, cancelled)
                    except (ToolError, ValueError, TypeError):
                        result = {
                            "error": "Appel d’outil invalide, interrompu ou hors des limites autorisées."
                        }
                    reply = {"role": "tool", "content": json.dumps(result, ensure_ascii=False)}
                    if provider["kind"] == "ollama":
                        reply["tool_name"] = function["name"]
                    else:
                        reply["tool_call_id"] = call["id"]
                    protocol.append(reply)
                    messages.append(reply)
                self.store.set_protocol(message_id, protocol)
                self.store.update_message(message_id, text, "streaming")
                if round_index == 7:
                    raise providers.ProviderError("Limite de huit tours d’outils atteinte.")
                # Separate narration before and after tool execution in the visible text.
                if text and not text.endswith("\n\n"):
                    text += "\n\n"
                    self.emit(
                        {
                            "event": "chat.delta",
                            "conversation_id": identifier,
                            "message_id": message_id,
                            "delta": "\n\n",
                        }
                    )
            if cancelled.is_set():
                status = "interrupted"
            else:
                self.store.set_protocol(message_id, protocol)
        except Exception as exc:
            status = "error"
            error = (
                str(exc)
                if isinstance(exc, (providers.ProviderError, ToolError))
                else "Erreur de génération"
            )
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
                or isinstance(exc, (providers.ProviderError, KernelError, ToolError))
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
