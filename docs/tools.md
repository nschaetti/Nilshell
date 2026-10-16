# Python tools in conversations

## First use on Linux

Install Bubblewrap 0.8+ (the development toolchain is unchanged):

```bash
sudo apt install bubblewrap
```

Open **Outils Python**. The example `list_directory` is ready to save. It declares an object JSON Schema with a required `path` argument, and this function:

```python
def run(arguments, system):
    """List a directory through the Nilshell kernel."""
    return system.call("fs.list", path=arguments["path"], limit=100)
```

1. Save the tool. The runtime status must say the Linux sandbox is operational.
2. Open an assistant, check this tool and save.
3. Start a **new conversation** with that assistant. Tool selection is snapshotted along with its model and system prompt.
4. Ask, for example, “Liste les fichiers de ~/Documents”. The provider/model must support function tools.
5. The model requests the Python tool. For each host syscall, the engine evaluates the existing permission rules. If required, the chat displays the resolved arguments and asks for approval. Choose one-time, session, permanent, or refusal.
6. The result is recorded in the chat and returned to the selected provider so the model can continue answering.

The tool's stable UUID, visible in its editor, is its permission `tool_id`. Its function name is the name advertised to the model. Rules combine that UUID with the conversation's actual assistant/provider/model identity and the normalized syscall arguments. Tool code cannot select a different identity, approve its own request, or change permission rules through its protocol.

## Tool contract

`run(arguments, system)` is synchronous. It must return a JSON-serializable result. The initial runtime provides the Python standard library, not pip installation or per-tool third-party dependencies. Tool code can use imports and ordinary Python processing, but `system.call("fs.read", path=...)` and the other kernel operations are the route to the user's host resources.

Code is parsed and JSON Schema validated on save; saving does not execute it. Tool arguments are checked before execution. Schema references cannot fetch remote resources. Tools are advertised only when both selected on the assistant snapshot and globally enabled. At execution time the host verifies the definition is still enabled and unchanged since it was advertised. Disabling/deleting a tool prevents later syscalls and rejects an authorization still pending for it.

## Isolation and limits

A dedicated process is started using Bubblewrap with user, mount, PID, network, IPC and other supported namespaces; dropped capabilities; disabled nested user namespaces; a clean environment; a fresh 256 MiB `/tmp` and 32 MiB `/work`, and a read-only root filesystem; and no home, application data, keyring, desktop sockets or host network mounted. Read-only OS libraries under `/usr` and `/lib`, and the selected Python runtime, remain available. In development only the standalone worker source and Python installation are additionally mounted; packaged builds mount the embedded engine executable in worker mode.

Normal `open`, `os` and `subprocess` methods can operate **inside that private environment**. They cannot be honestly described as removed from Python. They do not gain access to arbitrary host files or commands in the host environment. Access to those resources goes through the separate broker protocol and its permissions. Kernel `process.run` deliberately executes a host command with the user's rights after authorization; its directory criterion is not a sandbox for the approved command.

There is **no unrestricted fallback** if Bubblewrap is absent, too old, or user namespaces are blocked. Ubuntu/AppArmor, containers, Flatpak or other confinement can prevent nested sandbox creation. The runtime check reports failure; resolve the platform configuration rather than disabling isolation. macOS and Windows chat support remains architecturally portable, but Python tool execution is currently disabled there pending equivalent native isolation.

Limits: 16 tools per assistant; 8 model/tool rounds and 32 tool executions per chat turn; 32 broker calls per execution; 60 seconds of worker elapsed time excluding broker/approval waits; 20 seconds CPU; 1 GiB address space; 8 MiB individual file size; 64 open descriptors; process-count limit 32; bounded protocol messages and a 256 KiB final tool result. These are per-process and per-mount limits, not an aggregate cgroup memory quota. Flooded stderr is capped by host monitoring. Ordinary `print` output is not interpreted as broker RPC. Unexpected messages, identity/approval fields or attempts to call desktop RPCs are rejected.

## Chat, approvals and history

OpenAI-compatible SSE tool-call fragments and native Ollama streaming tool messages are supported. Calls in a batch execute sequentially. Function-call IDs/results and Ollama thinking fields are retained in protocol history for subsequent requests. Visible tool runs store arguments, status and result, linked to the assistant message. This can contain sensitive data: unlike the minimal syscall audit, chat tool history stores actual results and is sent back to the chosen provider.

The desktop sends `chat.approve` only for an outstanding request, one time. The isolated worker receives neither its host approval ticket nor a desktop RPC endpoint. Rules are re-evaluated by the kernel at execution; an explicit deny cannot be overridden by clicking approval. Global tool deactivation is also checked after a pending approval.

Stop interrupts permission waits and worker execution. It cannot undo syscalls already performed. Tool runs survive interruption; a subsequent chat turn includes a bounded recovery note for failed/interrupted turns so the model knows tools may already have acted. Network-stream cancellation still waits for the next provider chunk/read timeout. A crash may leave a syscall's side effects uncertain; history is not a transaction or rollback mechanism.

## Trusted desktop RPC

`tools.list`, `tools.save`, `tools.delete`, `tools.runtime`; assistant `tool_ids`; `chat.approve` with `{id, approved, remember}`. Tool workers do not receive access to these methods. There is no desktop API for directly executing arbitrary Python without the sandbox.

## Validation

Unit tests cover schemas, selection snapshots, fragment assembly, host-issued identity, denial, approval/replay, deactivation, protocol spoofing, error results and the full model/tool/result loop using a controlled test executor. A separate native test exercises actual host-file/network/environment exclusion and the broker path. It skips with an explicit reason on incompatible development hosts; the dedicated GitHub Actions sandbox job requires it to pass. The local test environment may itself prohibit nested namespaces.
