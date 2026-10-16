"""Provider adapters with streaming normalized to text deltas."""

import json
from urllib.parse import urlsplit

import httpx


def strict_json(value):
    def reject_constant(constant):
        raise ValueError("Non-finite JSON")

    return json.loads(value, parse_constant=reject_constant)


class ProviderError(Exception):
    """A safe error that never includes headers, keys, or response bodies."""


def validate_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL HTTP(S) requise")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("L’URL ne doit contenir ni identifiants, ni paramètres, ni fragment")
    return value


def headers(key):
    return {"Authorization": f"Bearer {key}"} if key else {}


def check_status(response):
    if response.status_code >= 400:
        explanations = {
            401: "Clé API absente ou invalide",
            403: "Accès refusé",
            404: "Endpoint ou modèle introuvable",
            429: "Quota ou limite de débit atteint",
        }
        raise ProviderError(
            explanations.get(
                response.status_code, f"Erreur du fournisseur (HTTP {response.status_code})"
            )
        )


def models(provider, key, transport=None):
    suffix = "/api/tags" if provider["kind"] == "ollama" else "/models"
    try:
        with httpx.Client(timeout=20, transport=transport) as client:
            response = client.get(provider["base_url"] + suffix, headers=headers(key))
            check_status(response)
            body = response.json()
            values = (
                [m["name"] for m in body.get("models", [])]
                if provider["kind"] == "ollama"
                else [m["id"] for m in body.get("data", [])]
            )
            return sorted(values)
    except httpx.HTTPError:
        raise ProviderError("Connexion impossible ou délai dépassé") from None


def stream(provider, key, model, messages, cancelled, transport=None):
    ollama = provider["kind"] == "ollama"
    url = provider["base_url"] + ("/api/chat" if ollama else "/chat/completions")
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), transport=transport) as client:
            with client.stream(
                "POST",
                url,
                headers=headers(key),
                json={"model": model, "messages": messages, "stream": True},
            ) as response:
                check_status(response)
                if cancelled.is_set():
                    return
                finished = False
                for line in response.iter_lines():
                    if cancelled.is_set():
                        return
                    if not line.strip() or (not ollama and not line.startswith("data:")):
                        continue
                    payload = line if ollama else line[5:].strip()
                    if payload == "[DONE]":
                        finished = True
                        break
                    data = strict_json(payload)
                    if "error" in data:
                        raise ProviderError("Le fournisseur a interrompu la génération")
                    if ollama:
                        content = data.get("message", {}).get("content", "")
                        finished = bool(data.get("done"))
                    else:
                        choices = data.get("choices", [])
                        content = choices[0].get("delta", {}).get("content", "") if choices else ""
                        if choices and choices[0].get("finish_reason") is not None:
                            finished = True
                    if isinstance(content, str) and content:
                        yield content
                    if ollama and finished:
                        break
                if not finished and not cancelled.is_set():
                    raise ProviderError("Flux interrompu avant la fin de la réponse")
    except httpx.HTTPError:
        raise ProviderError("Connexion interrompue ou délai dépassé") from None
    except (ValueError, KeyError, TypeError):
        raise ProviderError("Réponse du fournisseur invalide") from None


def stream_turn(provider, key, model, messages, cancelled, tools, transport=None):
    """Stream text and assemble one assistant turn, including fragmented tool calls."""
    ollama = provider["kind"] == "ollama"
    url = provider["base_url"] + ("/api/chat" if ollama else "/chat/completions")
    body = {"model": model, "messages": messages, "stream": True, "tools": tools}
    calls = {}
    content = ""
    thinking = ""
    finished = False
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=15), transport=transport) as client:
            with client.stream("POST", url, headers=headers(key), json=body) as response:
                check_status(response)
                for line in response.iter_lines():
                    if cancelled.is_set():
                        return
                    if not line.strip() or (not ollama and not line.startswith("data:")):
                        continue
                    payload = line if ollama else line[5:].strip()
                    if payload == "[DONE]":
                        finished = True
                        break
                    data = strict_json(payload)
                    if "error" in data:
                        raise ProviderError("Le fournisseur a interrompu la génération.")
                    if ollama:
                        delta = data.get("message", {})
                        finished = bool(data.get("done"))
                    else:
                        choices = data.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        finished = finished or choices[0].get("finish_reason") is not None
                    text = delta.get("content") or ""
                    if isinstance(text, str) and text:
                        content += text
                        yield {"text": text}
                    if isinstance(delta.get("thinking"), str):
                        thinking += delta["thinking"]
                    for position, call in enumerate(delta.get("tool_calls") or []):
                        index = call.get("index", len(calls) if ollama else position)
                        if not isinstance(index, int) or not 0 <= index < 16:
                            raise ProviderError("Trop d’appels d’outils dans une réponse.")
                        target = calls.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        function = call.get("function", {})
                        if ollama:
                            target["function"]["name"] = function.get("name", "")
                            target["function"]["arguments"] = json.dumps(
                                function.get("arguments", {})
                            )
                        else:
                            target["id"] += call.get("id") or ""
                            target["function"]["name"] += function.get("name") or ""
                            target["function"]["arguments"] += function.get("arguments") or ""
                        if (
                            len(target["function"]["arguments"]) > 65536
                            or len(target["function"]["name"]) > 64
                            or len(target["id"]) > 200
                        ):
                            raise ProviderError("Appel d’outil trop volumineux.")
                    if ollama and finished:
                        break
                if not finished and not cancelled.is_set():
                    raise ProviderError("Flux interrompu avant la fin de la réponse.")
        ordered = [calls[index] for index in sorted(calls)]
        if ollama:
            ordered = [
                {
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": json.loads(call["function"]["arguments"]),
                    }
                }
                for call in ordered
            ]
        elif any(not call["id"] for call in ordered) or len({c["id"] for c in ordered}) != len(
            ordered
        ):
            raise ProviderError("Identifiants d’appels d’outils invalides.")
        message = {"role": "assistant", "content": content}
        if ordered:
            message["tool_calls"] = ordered
        if thinking:
            message["thinking"] = thinking
        yield {"message": message}
    except httpx.HTTPError:
        raise ProviderError("Connexion interrompue ou délai dépassé.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("Réponse d’outil du fournisseur invalide.") from None
