# Local syscall kernel

The kernel supplies a fixed registry of operations and [composable permissions](permissions.md). The [Python tool runtime](tools.md) now exposes selected tool functions to models and brokers their host operations. The **Système** desktop page is a manual integration console for reviewing and testing calls.

## Calls

| Call | Arguments | Behavior |
|---|---|---|
| `fs.stat` | `path` | Type, size and modification timestamp. |
| `fs.list` | `path`, `limit` (default 200, max 1000) | Non-recursive entries, explicit truncation; symlinks are listed without traversal. |
| `fs.read` | `path`, `max_bytes` (default 128 KiB, max 1 MiB) | UTF-8 regular files only, bounded read and truncation flag. |
| `fs.write` | `path`, `content`, `overwrite` (default false), optional `expected_sha256` | Atomic UTF-8 publication, max 1 MiB; no implicit overwrite. Existing POSIX permission bits are preserved, excluding special bits. New files are private (0600). |
| `fs.mkdir` | `path` | Create one directory (0700); parent must exist. |
| `process.run` | `argv`, `cwd`, `timeout_seconds` (default 15, max 60), `max_output_bytes` (default 64 KiB, max 1 MiB) | Direct program execution with argv, exit code, separate stdout/stderr, termination reason and truncation flag. |

Paths must be absolute; `~` is expanded. No environment-variable expansion. Commands use a bare executable name resolved through PATH or an absolute executable path. Arguments are never implicitly interpreted by a shell. An explicit interpreter such as `sh -c` is still a program with full user authority and must be reviewed as such.

## Request lifecycle

1. `system.catalog` returns the operation descriptions and strict JSON schemas. Unknown fields and coerced argument types are rejected.
2. `system.prepare` receives `{syscall, arguments}`. The engine validates types, resolves paths and executables, snapshots filesystem metadata and stores the immutable pending arguments in memory. It returns a request ID, the canonical arguments, caller identity and current permission decision for display. A matching deny rejects preparation. This step reads metadata but neither file contents nor command output.
3. The trusted desktop reviews those exact arguments and calls `system.execute` with `{id, approved: true}` or `{id, approved: false}`. Execution accepts no replacement arguments. Without an explicit approval, execution is permitted only by a current allow rule. Explicit deny rules cannot be overridden. Request IDs are consumed atomically once, expire after five minutes and do not survive restart.
4. Execution revalidates resource metadata before calling the operation. Changed resources require a new request. On POSIX, directory traversal uses directory descriptors and `O_NOFOLLOW`; final file reads reject special files and symlink substitution. Windows currently uses canonical-path checks and has weaker protection against concurrent path changes.
5. `system.history` returns the last 100 audit entries. The database keeps call name, resource path (executable only for commands), state, error code, timestamp and duration. It excludes file content, full command arguments, environment and stdout/stderr. Invalid prepares are logged without their input. Restart marks pending/running entries interrupted. Expiry is also checked lazily when executing or preparing another call.

A path review is not a filesystem snapshot or a transaction with other applications. Optional write hashes detect stale content before replacement; they cannot lock out concurrent writers. Commands can alter any resource accessible to the user, irrespective of the selected working directory.

## Process limits and scope

The engine passes only a small OS environment allowlist, not arbitrary inherited variables or configured provider keys. This is not credential isolation: a command can still access the user's files, keyring, network or other process resources.

Captured output is stored in temporary files rather than unbounded Python memory. The engine polls output size and timeout every 20 ms, terminates on limits, and bounds the returned bytes. A fast producer can exceed the threshold on disk between polls; there is no OS disk/CPU/memory quota. On POSIX it terminates the process group on completion or limits; descendants that deliberately detach can escape. On Windows it terminates only the direct process. Operations are synchronous RPC calls on engine worker threads; process execution is limited to 60 seconds. Closing the application is not a transactional rollback of filesystem changes or subprocess side effects.

## Authorization boundary and next milestone

Only the trusted desktop currently calls this RPC surface. `system.execute` must never be offered as an agent tool: a model-supplied `approved: true` is not human authorization. The permission engine binds assistant, provider/model and tool identity to rules with session or permanent scope. The Linux isolated runner separates untrusted tool requests from trusted desktop approval and supplies host-issued identities through its private broker channel. Decision models will add conditions to that policy and will not expand permissions. Arbitrary Python will need OS isolation before the broker can be treated as its only route to system access.

No delete, recursive mutation, app opening or kernel-mediated notification call is included in this milestone.
