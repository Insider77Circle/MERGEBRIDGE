"""Orchestrates the two replica processes (host + guest) over their stdin
command channels. Sync sessions are choreographed the way coop would do it:
the host listens on the socket, the guest connects, the Automerge sync
protocol runs over the stream until both replicas converge."""
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "target" / "release" / "replica"


class Replica:
    def __init__(self, role: str, workdir: Path, socket: Path):
        self.role = role
        self.dir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        import os
        if os.environ.get("MB_DEBUG"):
            err = open(workdir / "stderr.log", "w")
        else:
            err = subprocess.STDOUT
        self.proc = subprocess.Popen(
            [str(BIN), "--role", role, "--dir", str(workdir),
             "--socket", str(socket)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, text=True, bufsize=1)
        self._lines = []
        self._cond = threading.Condition()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            with self._cond:
                self._lines.append(line.rstrip("\n"))
                self._cond.notify_all()

    def cmd(self, command: str, timeout: float = 120.0) -> str:
        """Send one command; return its output (excluding the DONE line)."""
        with self._cond:
            start = len(self._lines)
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        with self._cond:
            ok = self._cond.wait_for(
                lambda: any(l == "DONE" for l in self._lines[start:]),
                timeout=timeout)
            out = [l for l in self._lines[start:] if l != "DONE"]
        if not ok:
            raise TimeoutError(f"{self.role}: no DONE for {command!r}; got {out}")
        return "\n".join(out)

    def quit(self):
        try:
            self.cmd("quit", timeout=10)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=10)


def sync_pair(host: Replica, guest: Replica) -> tuple[str, str]:
    """Run one sync session: host accepts, guest connects, protocol runs.

    The guest must not connect before the host has bound the socket —
    wait for the socket file to appear (bind creates it) before starting
    the guest side, or the connect races the bind and fails."""
    import time
    result = {}
    errors = {}

    def run_host():
        try:
            result["host"] = host.cmd("sync")
        except Exception as e:  # surfaced below, not swallowed
            errors["host"] = e

    t = threading.Thread(target=run_host)
    t.start()
    sock = Path(host.proc.args[6]) if len(host.proc.args) > 6 else None
    # args: [bin, --role, role, --dir, dir, --socket, socket]
    deadline = time.time() + 30
    while sock is not None and not sock.exists() and time.time() < deadline:
        time.sleep(0.05)
    result["guest"] = guest.cmd("sync")
    t.join(timeout=120)
    if errors:
        raise RuntimeError(f"host sync failed: {errors['host']}")
    return result["host"], result["guest"]
