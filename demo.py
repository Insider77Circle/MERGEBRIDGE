#!/usr/bin/env python3
"""MERGEBRIDGE narrated demonstration.

The scenario coop's Firecracker path cannot represent today: a human edits
a file on the host WHILE an agent edits the same file in the guest.

  Act 1 — the copy model (rsync semantics, last copy wins): one edit dies.
  Act 2 — MERGEBRIDGE (Automerge replicas over a socket): both edits live.

Run:  python3 demo.py
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
from harness import Replica, sync_pair, ROOT  # noqa: E402

WORK = ROOT / "demo-work"
SOCK = WORK / "relay.sock"
BASE = "The quick brown fox jumps over the lazy dog.\n"


def main():
    print("=" * 68)
    print("ACT 1 — THE COPY MODEL (what coop does on Firecracker today)")
    print("=" * 68)
    host_copy = "HOST: reviewed at dawn. " + BASE
    guest_copy = BASE + " GUEST: agent was here."
    print(f"host edits its copy : {host_copy!r}")
    print(f"guest edits its copy: {guest_copy!r}")
    final = guest_copy  # sync-out overwrites the host copy
    print(f"after sync-out      : {final!r}")
    print(">> the host's edit is GONE. Last copy won; nobody was told.\n")

    print("=" * 68)
    print("ACT 2 — MERGEBRIDGE (two Automerge replicas, one document)")
    print("=" * 68)
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    host = Replica("host", WORK / "host", SOCK)
    guest = Replica("guest", WORK / "guest", SOCK)
    try:
        host.cmd("init-fixture")
        sync_pair(host, guest)
        print("both replicas hold the fixture workspace (notes.txt + logo.bin ref)")
        host.cmd("insert notes.txt 0 HOST: reviewed at dawn. ")
        guest.cmd(f"insert notes.txt {len(BASE)}  GUEST: agent was here.")
        print("host and guest edit notes.txt SIMULTANEOUSLY, while disconnected")
        sync_pair(host, guest)
        merged = (WORK / "host" / "ws" / "notes.txt").read_text()
        print(f"after one sync round: {merged!r}")
        print(">> BOTH edits survive. Same document, both replicas, no loss.")
        print(f"   heads — host: {host.cmd('heads')}")
        print(f"   heads — guest: {guest.cmd('heads')}")
    finally:
        host.quit()
        guest.quit()


if __name__ == "__main__":
    main()
