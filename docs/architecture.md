# Architecture

## Desktop milestone (implemented)

- Svelte 5 / TypeScript webview inside Tauri 2.
- Rust owns the lifecycle of an embedded Python engine and forwards JSON-lines RPC over stdin/stdout. No HTTP listener, port, or browser-accessible local API.
- Python owns provider connections and a local SQLite database. OpenAI-compatible `/chat/completions` and native Ollama `/api/chat` are separate adapters.
- API keys use the OS keyring. If unavailable, they stay in engine memory for the current session; a visible warning explains this. Provider addresses, assistant definitions, conversation snapshots and messages use SQLite.
- A conversation snapshots its assistant configuration at creation. Editing an assistant affects new conversations. Provider credentials and endpoints are resolved at request time.
- Each streamed delta is persisted; startup marks unfinished messages interrupted. Only complete messages are included in subsequent requests. One generation at a time in this first milestone.
- Markdown is sanitized and remote images are disabled. The webview cannot use shell or filesystem plugins. Notifications are opt-in and contain no conversation content.

## Following milestones (not implemented)

1. A broker for system calls: filesystem operations, commands, application opening and notifications.
2. A policy engine over assistant identity, underlying model, tool identity, action, canonical path, executable, arguments and session. Check every syscall, not just tool activation.
3. Decision-model checks for selected calls. These add a condition to policy; they cannot override explicit denial or expand granted capabilities. Human questions suspend and resume the operation.
4. Python tools in isolated processes. Tools receive a capability-scoped broker client. An ordinary Python process is not a sandbox: OS-level restrictions are required before promising mediated-only access. Cross-platform sandbox mechanisms must be assessed separately.
5. Local knowledge: watch selected files/directories, index incremental versions, cite original paths/pages and open the source in its registered app through the broker.
6. Attachments, native Anthropic API, tool calling, permission UI and task notifications.

## Boundaries

The current milestone does not execute tools or arbitrary commands. Rust's sidecar process creation is a fixed application function, not an agent syscall. Local persistence does not imply local inference: prompts, history and future retrieved excerpts are sent to the selected provider. Plain HTTP is supported for local Ollama or trusted endpoints; use HTTPS for remote providers.

Cancellation stops consuming the stream at the next received line (or after the 120-second read timeout), persists partial output and closes the connection. It does not guarantee the remote provider stops computation or billing. No automatic context truncation: context-limit failures are surfaced to the user.

## RPC

Requests: `{ "id": "uuid", "method": "bootstrap", "params": {} }`.
Responses: `{ "id": "uuid", "result": ... }` or `{ "id": "uuid", "error": "safe message" }`.
Events: `chat.delta`, `chat.done`, `engine.stopped` (host-generated).

Methods: `bootstrap`, `provider.save`, `provider.models`, `assistant.save`, `conversation.create`, `conversation.get`, `conversation.delete`, `chat.start`, `chat.cancel`.
