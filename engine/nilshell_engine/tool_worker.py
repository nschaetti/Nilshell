"""Standalone stdlib-only worker, launched exclusively inside the OS sandbox."""

import contextlib
import json
import resource
import sys

MAX_LINE = 1048576


def send(value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False)
    if len(data) > MAX_LINE:
        raise ValueError("Résultat trop volumineux")
    sys.__stdout__.write(data + "\n")
    sys.__stdout__.flush()


class System:
    """Request a host syscall; permissions and identity remain in the host."""

    def call(self, syscall, **arguments):
        send({"type": "syscall", "syscall": syscall, "arguments": arguments})
        line = sys.stdin.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE:
            raise RuntimeError("Canal du noyau interrompu")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "Appel système refusé"))
        return reply["result"]


def main():
    # Hard limits cannot be raised by tool code after capabilities are dropped.
    for kind, limit in (
        (resource.RLIMIT_CPU, 20),
        (resource.RLIMIT_AS, 1073741824),
        (resource.RLIMIT_FSIZE, 8388608),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_NPROC, 32),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (limit, limit))
    line = sys.stdin.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE:
        return
    payload = json.loads(line)
    try:
        namespace = {"__name__": "nilshell_tool"}
        # Printing is not the RPC protocol. No host paths/credentials are mounted.
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(payload["code"], "<tool>", "exec"), namespace)
            result = namespace["run"](payload["arguments"], System())
        send({"type": "result", "value": result})
    except BaseException as exc:
        send({"type": "error", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})


if __name__ == "__main__":
    main()
