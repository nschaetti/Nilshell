# Composable permissions

The **Permissions** page creates, edits, disables and revokes rules. The **Système** console lets you test a rule with an assistant and tool identifier. Selected Python tools can now be called from conversations through the [Linux isolated runtime](tools.md).

## Identity and combinations

Every selector present in a rule must match (AND): assistant ID, provider ID, exact model identifier, exact tool ID, syscall, canonical directory, executable, command arguments and parameter conditions. Empty selectors are wildcards. Rules do not use glob patterns for identities or paths.

In the manual console, model and provider are derived by the engine from the selected stored assistant. A client-supplied model/provider cannot override them. The manual console tool identifier is a trusted test selector. During real tool execution, the host supplies the registered tool UUID and conversation assistant/provider/model identity. The isolated runner uses a separate broker-only interface; desktop approval and rule-management RPCs are not exposed to the model or tool code.

## Resolution

1. Any matching **deny** wins, including over a human one-call approval.
2. Otherwise any matching **ask** requires explicit confirmation, even if an allow rule also matches.
3. Otherwise any matching **allow** permits execution without a new confirmation.
4. No match defaults to **ask**.

There is no specificity score, ordering dependency or “last rule wins”. Preparation exposes the current decision and matched rule names. Execution reads the rules again; disabling, revoking or adding a deny rule blocks a pending call. Authorization occurs immediately before execution. Revocation does not cancel operations already running or undo side effects.

## Lifetime

- **Cette fois** approves only the current one-use request and creates no rule.
- **Cette session** stores an allow rule in engine memory, lost when Nilshell's engine exits.
- **Permanent** stores an allow rule in the local SQLite database.

The two memory buttons grant only the exact normalized call for the same assistant/provider/model/tool identity. Their rule stores a SHA-256 digest rather than a copy of content or command arguments. Changing a parameter or identity requires a new decision. A digest is not encryption and low-entropy values can be guessed. Explicit ask and deny rules retain their precedence over these convenience grants.

For broader permissions (for example, all reads under a research folder), create an explicit rule in **Permissions**. Editing an automatic exact-call rule keeps its digest restriction. Create a new rule to broaden it.

Session rules are listed together with permanent rules and can be disabled, edited or revoked. Changing scope moves the same rule; it does not leave an active duplicate.

## Directory and command semantics

Directory rules match canonical paths using path components, so `/docs` does not match `/docs-private`. Symlink targets are resolved before matching; an alias under an allowed directory cannot authorize a file outside it. The folder must exist when saving a rule.

Recursive rules cover the folder and all descendants. Nonrecursive rules permit the folder and its direct entries for file operations. `fs.list` and a command's `cwd` must equal the folder itself in nonrecursive rules, preventing directory enumeration at the next level. A directory condition on `process.run` checks only the working directory; it is not a sandbox for the program.

A command selector resolves a bare executable name or absolute path to the canonical executable path. `arguments` matches the exact argv tail (without the executable); `[]` means no arguments. `argument_prefix` matches the leading argv tail elements exactly. It is a literal list prefix, not shell syntax. Command selectors never match file syscalls.

## Parameter conditions

Example: permit `fs.read` for an assistant under `~/Documents/Research` only when:

```json
{
  "max_bytes": {"op": "lte", "value": 131072}
}
```

Supported operators: `eq`, `in`, `lte`, `gte`, `prefix`. Every named parameter must exist and every condition must match. Unknown parameters/operators and invalid bounds are rejected. Booleans do not compare equal to numbers. `prefix` is a literal string/list prefix; use the directory selector for filesystem boundaries. Conditions operate on normalized arguments, including defaults and resolved paths.

Custom conditions may themselves contain sensitive literal values; explicit rules are stored locally in plaintext. The audit log records caller identity, decision and winning rule IDs/names, not the rule conditions, file content or command output. Existing history is preserved by an additive database migration.

## Trusted desktop RPC

- `permissions.list`
- `permissions.save` with a strict rule object
- `permissions.delete` with `{id}`
- `system.prepare` additionally accepts optional `assistant_id` and `tool_id` for manual tests.
- `system.execute` accepts optional `approved` and `remember` (`once`, `session`, `permanent`). Omit `approved` when relying on an allow rule; `false` always refuses. Remembering requires an explicit `true` and occurs only after a successful operation.

The host-side `Caller` type is immutable and preparation freezes its identity for that request. This policy is a broker control, not OS isolation. Python tools must not be allowed to self-assert identities, edit rules, pass their own approval or bypass the broker using normal Python system APIs.
