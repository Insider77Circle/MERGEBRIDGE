#!/usr/bin/env python3
"""MERGEBRIDGE prototype — automated proof tests.

(a) Convergence under concurrency: simultaneous edits to the SAME text file
    while disconnected both survive the merge (no silent loss).
(b) Divergent creation: files created on opposite sides while disconnected
    appear on both replicas after sync, byte-identical.
(c) Binary handling: binaries are hash references in the CRDT only — the
    binary bytes never appear inside the saved document; content crosses
    out-of-band and verifies by hash.
(d) FALSIFIER CONTRAST: the same concurrent-edit scenario under the copy
    model (last-copy-wins, rsync semantics) LOSES one side's edit, while
    MERGEBRIDGE (a) keeps both. If (a) fails, the concept fails.

Run:  python3 tests/run_tests.py     (exit 0 = all pass)
"""
import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from harness import Replica, sync_pair, ROOT  # noqa: E402

WORK = ROOT / "test-work"
SOCK = WORK / "relay.sock"
MARKER = b"MERGEBRIDGE-BINARY-MARKER"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def notes_text(dump: str) -> str:
    for line in dump.splitlines():
        if line.startswith("FILE notes.txt TEXT "):
            return line[len("FILE notes.txt TEXT "):].replace("\\n", "\n")
    return ""


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    host = Replica("host", WORK / "host", SOCK)
    guest = Replica("guest", WORK / "guest", SOCK)
    try:
        host.cmd("init-fixture")
        sync_pair(host, guest)
        d_host, d_guest = host.cmd("dump"), guest.cmd("dump")
        check("setup: replicas converge on the fixture", d_host == d_guest)

        # ---------- (a) convergence under concurrency ----------
        base = notes_text(d_host)
        host.cmd("insert notes.txt 0 HOST: reviewed at dawn. ")
        guest.cmd(f"insert notes.txt {len(base)}  GUEST: agent was here.")
        # (still disconnected — the edits above happened with no sync)
        sync_pair(host, guest)
        d_host, d_guest = host.cmd("dump"), guest.cmd("dump")
        merged = notes_text(d_host)
        check("(a) merged document identical on both replicas", d_host == d_guest)
        check("(a) host edit survives the merge", "HOST: reviewed at dawn." in merged)
        check("(a) guest edit survives the merge", "GUEST: agent was here." in merged)
        ws_host = (WORK / "host" / "ws" / "notes.txt").read_text()
        ws_guest = (WORK / "guest" / "ws" / "notes.txt").read_text()
        check("(a) projected files identical and complete",
              ws_host == ws_guest == merged)

        # ---------- (b) divergent creation ----------
        host.cmd("create-text host_notes.txt host created this file")
        guest.cmd("create-text guest_notes.txt guest created this file")
        sync_pair(host, guest)
        d_host, d_guest = host.cmd("dump"), guest.cmd("dump")
        both = ("FILE host_notes.txt TEXT host created this file" in d_host
                and "FILE guest_notes.txt TEXT guest created this file" in d_host)
        check("(b) both divergently-created files present after sync", both)
        check("(b) document state identical on both replicas", d_host == d_guest)
        files_ok = all(
            (WORK / side / "ws" / f).read_text() == content
            for side in ("host", "guest")
            for f, content in (("host_notes.txt", "host created this file"),
                               ("guest_notes.txt", "guest created this file")))
        check("(b) projected files byte-identical on both sides", files_ok)

        # stability: an extra sync round changes nothing
        h1 = host.cmd("heads")
        sync_pair(host, guest)
        check("(b) heads stable across a redundant sync", host.cmd("heads") == h1
              and guest.cmd("heads") == h1)

        # ---------- (c) binary handling ----------
        doc_bytes = (WORK / "host" / "doc.automerge").read_bytes()
        check("(c) binary bytes are NOT inside the CRDT document",
              MARKER not in doc_bytes,
              f"scanned {len(doc_bytes)} document bytes for the marker")
        ref = None
        for line in d_host.splitlines():
            if line.startswith("FILE logo.bin BINARY sha256="):
                ref = line.split("sha256=")[1].split(" ")[0]
        guest_blob = (WORK / "guest" / "ws" / "logo.bin").read_bytes()
        actual = hashlib.sha256(guest_blob).hexdigest()
        check("(c) document carries the sha256 reference", ref is not None)
        check("(c) guest's out-of-band copy verifies against the reference",
              ref == actual and MARKER in guest_blob)

        # ---------- (d) falsifier contrast: the copy model ----------
        copy_host = base  # both sides start from the same copied file
        copy_guest = base
        copy_host = "HOST: reviewed at dawn. " + copy_host          # host edits its copy
        copy_guest = copy_guest + " GUEST: agent was here."        # guest edits its copy
        final = copy_guest  # sync-out: guest copy overwrites host copy (last copy wins)
        copy_loses = "HOST: reviewed at dawn." not in final
        check("(d) COPY MODEL loses the host edit (last-copy-wins)", copy_loses,
              "host edit absent from the post-sync file")
        check("(d) MERGEBRIDGE keeps both edits (contrast with (a))",
              "HOST: reviewed at dawn." in merged and "GUEST: agent was here." in merged)
    finally:
        host.quit()
        guest.quit()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED CHECKS:")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
