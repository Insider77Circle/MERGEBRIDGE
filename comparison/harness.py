#!/usr/bin/env python3
"""MERGEBRIDGE comparison harness — coop issue #715 (hbrodin's ask).

Three arms run the SAME scenarios over the SAME base file:

  Arm A — coop's current push/pull, source-faithful file-level model using
          the real rsync binary with coop's exact argument sets
          (see coop-semantics.md for the source citations this mirrors).
  Arm B — simpler sync: sha256 change detection against a stored base
          snapshot + three-way merge (git merge-file / diff3); unmergeable
          conflicts preserve both versions (conflict copy) and are reported.
  Arm C — the MERGEBRIDGE CRDT prototype (target/release/replica), driven
          through tools/harness.py exactly as its own test suite drives it.

Scenarios (base file: config.yaml, identical everywhere):
  S1a concurrent edits, DIFFERENT regions of the same file
  S1b concurrent edits, SAME line of the same file
  S2  guest editor whole-file replace (temp + rename) vs host in-place edit
  S3  guest-only edit, host untouched (control)
  S4  host edits config.yaml; guest creates a NEW file (untracked)

Run:  python3 comparison/harness.py     (writes comparison/results.json)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = HERE / "work"
sys.path.insert(0, str(ROOT / "tools"))
from harness import Replica, sync_pair  # noqa: E402  (prototype's own driver)

# ---------------------------------------------------------------- fixture

BASE = (
    "# mergebridge comparison fixture\n"
    "service:\n"
    "  name: coop-demo\n"
    "  port: 8080\n"
    "  replicas: 2\n"
    "logging:\n"
    "  level: info\n"
    "  format: text\n"
    "features:\n"
    "  cache: false\n"
    "  telemetry: false\n"
)
CFG = "config.yaml"
NEWFILE = "agent_notes.txt"
NEWFILE_CONTENT = "agent notes: investigated the failing check.\n"

HOST_TIMEOUT_LINE = "  timeout: 30\n"          # inserted after replicas line
GUEST_LOG_LINE = "  file: /var/log/app.log\n"   # inserted after format line
GUEST_WORKERS_LINE = "  workers: 4\n"           # inserted after replicas line

# S2: the guest editor's whole-file rewrite (two value changes, whole file
# written to a temp file and renamed over the original).
GUEST_REWRITE = BASE.replace("level: info", "level: debug").replace(
    "cache: false", "cache: true")

T0, T1, T2 = 1_700_000_000, 1_700_000_100, 1_700_000_200  # base/host/guest mtimes


def off_after(needle: str) -> int:
    return BASE.index(needle) + len(needle)


OFF_REPLICAS = off_after("  replicas: 2\n")
OFF_FORMAT = off_after("  format: text\n")
OFF_LEVEL_INFO = off_after("  level: info")

# Per-scenario edit scripts, expressed once, applied per arm.
# File-level arms apply them as text insertions / rewrites on plain files;
# Arm C applies the insertions through the replica command channel (the
# prototype CLI exposes insert/create-text only — no delete/replace — so
# every scenario edit is an insertion by construction; S2's guest rewrite
# is the deliberate exception: it happens OUTSIDE the guest replica, which
# is exactly the editor case hbrodin asked about).
SCENARIOS = {
    "S1a": dict(
        title="concurrent edits, different regions",
        host_insert=(OFF_REPLICAS, HOST_TIMEOUT_LINE),
        guest_insert=(OFF_REPLICAS + len(HOST_TIMEOUT_LINE), GUEST_LOG_LINE),
        # NOTE: guest offset is in the GUEST's own copy, which never saw the
        # host insert — recomputed per arm below via markers instead.
        host_marker="timeout: 30", guest_marker="file: /var/log/app.log"),
    "S1b": dict(
        title="concurrent edits, same line",
        host_insert=(OFF_LEVEL_INFO, "_HOST"),
        guest_insert=(OFF_LEVEL_INFO, "_GUEST"),
        host_marker="_HOST", guest_marker="_GUEST"),
    "S2": dict(
        title="guest whole-file replace vs host in-place edit",
        host_insert=(OFF_REPLICAS, HOST_TIMEOUT_LINE),
        guest_rewrite=GUEST_REWRITE,
        host_marker="timeout: 30", guest_marker="level: debug"),
    "S3": dict(
        title="guest-only edit (control)",
        guest_insert=(OFF_REPLICAS, GUEST_WORKERS_LINE),
        host_marker=None, guest_marker="workers: 4"),
    "S4": dict(
        title="host edits config; guest creates new untracked file",
        host_insert=(OFF_REPLICAS, HOST_TIMEOUT_LINE),
        guest_create=(NEWFILE, NEWFILE_CONTENT),
        host_marker="timeout: 30", guest_marker=NEWFILE),
}


def insert_into(text: str, offset: int, payload: str) -> str:
    return text[:offset] + payload + text[offset:]


def guest_insert_offset(scn: str) -> int:
    """Offset of the guest's insertion in the GUEST's own copy of BASE
    (the guest never saw the host's concurrent insert)."""
    if scn == "S1a":
        return OFF_FORMAT
    return SCENARIOS[scn]["guest_insert"][0]


def apply_file_edits(scn: str, host_dir: Path, guest_dir: Path):
    """Apply the scenario's edits to plain-file host/guest trees."""
    s = SCENARIOS[scn]
    if "host_insert" in s:
        p = host_dir / CFG
        p.write_text(insert_into(BASE, s["host_insert"][0], s["host_insert"][1]))
        os.utime(p, (T1, T1))
    if "guest_insert" in s:
        p = guest_dir / CFG
        p.write_text(insert_into(BASE, guest_insert_offset(scn),
                                 s["guest_insert"][1]))
        os.utime(p, (T2, T2))
    if "guest_rewrite" in s:
        # editor save: write temp file in the same directory, rename over
        tmp = guest_dir / ".config.yaml.tmp"
        tmp.write_text(s["guest_rewrite"])
        os.replace(tmp, guest_dir / CFG)
        os.utime(guest_dir / CFG, (T2, T2))
    if "guest_create" in s:
        name, content = s["guest_create"]
        p = guest_dir / name
        p.write_text(content)
        os.utime(p, (T2, T2))


def score(scn: str, final_cfg: str | None, extra_files: dict) -> dict:
    """extra_files: {filename: content} present on the scored side beyond
    config.yaml (used for S4's new file and Arm B conflict copies)."""
    s = SCENARIOS[scn]
    out = {}
    if s["host_marker"] is None:
        out["host_edits_preserved"] = None  # host made no edit (control)
    elif s["host_marker"] == NEWFILE:
        out["host_edits_preserved"] = None
    else:
        out["host_edits_preserved"] = bool(
            final_cfg and s["host_marker"] in final_cfg)
    if s["guest_marker"] == NEWFILE:
        out["guest_edits_preserved"] = NEWFILE in extra_files
    else:
        out["guest_edits_preserved"] = bool(
            final_cfg and s["guest_marker"] in final_cfg)
    return out


# ---------------------------------------------------------------- Arm A
# Source-faithful model of coop push/pull (src/workspace.rs in trailofbits/
# coop, cloned 2026-11-24). Uses the real rsync with coop's argument sets;
# the SSH transport is replaced by local paths (file-level model).
#
# Mirrors:
#   rsync_base_args  (workspace.rs:541-558): -az, .gitignore filter rule,
#                    DEFAULT_EXCLUDES (workspace.rs:23-29)
#   copy_workspace   (workspace.rs:161-170): Copy mode, no --delete
#   rsync_to_guest   (workspace.rs:591-618): --delete only in Mirror mode
#   push             (workspace.rs:270-298): check_guest_dirty unless --force,
#                    then Mirror rsync
#   check_guest_dirty(workspace.rs:912-966): git status --porcelain
#                    --untracked-files=no + unpushed-commit count, only if
#                    .git exists; any output aborts the push
#   pull             (workspace.rs:305-337): refuse non-empty dest unless
#                    --force (pull_destination_is_nonempty, 968-985)
#   rsync_pull       (workspace.rs:620-648): stage guest->staging with
#                    exclude_git=true (rsync_pull_args, 650-654), then
#                    install_staged_pull (667-681) = rsync -a
#                    --exclude=.[gG][iI][tT] staging->dest, NO --delete

GIT_EXCLUDE = ".[gG][iI][tT]"
DEFAULT_EXCLUDES = ["node_modules/", "target/", "__pycache__/", ".venv/",
                    ".coop/"]


def rsync(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"rsync failed {args}: {r.stderr}")


def coop_base_args(exclude_git: bool) -> list[str]:
    args = ["rsync", "-az", "--filter=:- .gitignore"]
    if exclude_git:
        args.append(f"--exclude={GIT_EXCLUDE}")
    else:
        args.append("--filter=+ /.git/***")
    for exc in DEFAULT_EXCLUDES:
        args.append(f"--exclude={exc}")
    return args


def coop_copy_in(host: Path, guest: Path) -> None:
    """copy_workspace: Copy mode (no --delete), .git included."""
    rsync(coop_base_args(exclude_git=False) + ["--", f"{host}/", f"{guest}/"])


def coop_check_guest_dirty(guest: Path) -> tuple[bool, str]:
    """Returns (blocked, detail). Mirrors check_guest_dirty."""
    if not (guest / ".git").exists():
        return False, "no .git in guest workspace — check passes vacuously"
    r = subprocess.run(
        ["git", "-C", str(guest), "status", "--porcelain",
         "--untracked-files=no"], capture_output=True, text=True)
    if r.returncode != 0:
        return True, f"git status probe failed (fail-closed): {r.stderr.strip()}"
    out = r.stdout.strip()
    if out:
        return True, f"guest dirty (tracked changes): {out!r}"
    return False, "guest clean per git status (untracked files not inspected)"


def coop_pull_force(host: Path, guest: Path, staging: Path) -> None:
    """pull with --force (the non-empty-destination refusal bypassed)."""
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # stage 1: guest -> staging, git always excluded
    rsync(coop_base_args(exclude_git=True) + ["--", f"{guest}/", f"{staging}/"])
    # stage 2: install staging -> dest, -a, git excluded, no --delete
    rsync(["rsync", "-a", f"--exclude={GIT_EXCLUDE}", "--",
           f"{staging}/", f"{host}/"])


def coop_push(host: Path, guest: Path, force: bool) -> tuple[str, str]:
    """Returns (outcome, detail): 'blocked' or 'pushed'."""
    if not force:
        blocked, detail = coop_check_guest_dirty(guest)
        if blocked:
            return "blocked", detail
    rsync(coop_base_args(exclude_git=False) + ["--delete", "--",
                                               f"{host}/", f"{guest}/"])
    return "pushed", "mirror rsync (-az --delete) host -> guest"


def arm_a_setup(root: Path) -> tuple[Path, Path]:
    host, guest = root / "host", root / "guest"
    host.mkdir(parents=True)
    guest.mkdir(parents=True)
    cfg = host / CFG
    cfg.write_text(BASE)
    os.utime(cfg, (T0, T0))
    # The fixture workspace is a git repo with the base committed — the
    # case where coop's push guard has its best chance. (Without .git the
    # guard is vacuous; see coop-semantics.md gap 2.)
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "fixture@example.invalid"],
                ["git", "config", "user.name", "fixture"],
                ["git", "add", CFG],
                ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=host, env=env, check=True,
                       capture_output=True, text=True)
    coop_copy_in(host, guest)
    return host, guest


def run_arm_a(scn: str) -> dict:
    res = {"arm": "A-coop-push-pull"}
    # --- pull direction (fresh fixture) ---
    root = WORK / "armA-pull" / scn
    shutil.rmtree(root, ignore_errors=True)
    host, guest = arm_a_setup(root)
    apply_file_edits(scn, host, guest)
    coop_pull_force(host, guest, root / "staging")
    final = (host / CFG).read_text()
    extra = {p.name: p.read_text() for p in host.iterdir()
             if p.is_file() and p.name != CFG}
    res["pull_force"] = {
        **score(scn, final, extra),
        "conflict_surfaced": False,
        "note": "pull --force: no per-file check exists to surface anything",
        "final_config": final,
    }
    # --- push direction (fresh fixture) ---
    root = WORK / "armA-push" / scn
    shutil.rmtree(root, ignore_errors=True)
    host, guest = arm_a_setup(root)
    apply_file_edits(scn, host, guest)
    outcome, detail = coop_push(host, guest, force=False)
    if outcome == "blocked":
        res["push"] = {
            "outcome": "blocked by check_guest_dirty",
            "conflict_surfaced": True,
            "detail": detail,
            "note": "guest work survives in the guest; nothing is merged; "
                    "coop's message directs the user to pull into a new "
                    "empty review directory or push --force",
        }
        # what the --force override would do:
        outcome2, detail2 = coop_push(host, guest, force=True)
        gfinal = (guest / CFG).read_text()
        gextra = {p.name: p.read_text() for p in guest.iterdir()
                  if p.is_file() and p.name != CFG}
        res["push_force_override"] = {
            **score(scn, gfinal, gextra),
            "conflict_surfaced": False,
            "detail": detail2,
            "final_config": gfinal,
            "guest_extra_files_after": sorted(gextra),
        }
    else:
        gfinal = (guest / CFG).read_text()
        gextra = {p.name: p.read_text() for p in guest.iterdir()
                  if p.is_file() and p.name != CFG}
        res["push"] = {
            "outcome": "proceeded (guard did not fire)",
            **score(scn, gfinal, gextra),
            "conflict_surfaced": False,
            "detail": detail,
            "final_config": gfinal,
            "guest_extra_files_after": sorted(gextra),
        }
    return res


# ---------------------------------------------------------------- Arm B
# Simpler sync: sha256 vs stored base snapshot + git merge-file (diff3).
# Changed on one side only -> take that side. Changed on both -> three-way
# merge; clean merge wins, otherwise the host version stays in place, the
# guest version is preserved as <file>.conflict-guest, and the conflict is
# reported. New file on one side -> copied across.

def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def arm_b_sync(host: Path, guest: Path, base_dir: Path) -> dict:
    names = set()
    for d in (host, guest, base_dir):
        names |= {p.name for p in d.iterdir() if p.is_file()}
    report = {}
    for name in sorted(names):
        hp, gp, bp = host / name, guest / name, base_dir / name
        h = hp.read_text() if hp.exists() else None
        g = gp.read_text() if gp.exists() else None
        b = bp.read_text() if bp.exists() else None
        h_changed = b is None and h is not None or (
            b is not None and h is not None and sha(h) != sha(b))
        g_changed = b is None and g is not None or (
            b is not None and g is not None and sha(g) != sha(b))
        if h is not None and g is not None and h == g:
            report[name] = "identical"
        elif g_changed and not h_changed:
            hp.write_text(g)
            report[name] = "guest version taken"
        elif h_changed and not g_changed:
            gp.write_text(h)
            report[name] = "host version kept, guest updated"
        elif h_changed and g_changed and b is not None:
            tmp = WORK / "armB-merge"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "cur").write_text(h)
            (tmp / "base").write_text(b)
            (tmp / "other").write_text(g)
            r = subprocess.run(
                ["git", "merge-file", "-p", str(tmp / "cur"), str(tmp / "base"),
                 str(tmp / "other")], capture_output=True, text=True)
            if r.returncode == 0:
                hp.write_text(r.stdout)
                gp.write_text(r.stdout)
                report[name] = "three-way merge, clean"
            elif r.returncode > 0:
                (host / f"{name}.conflict-guest").write_text(g)
                report[name] = ("CONFLICT: host version kept in place, "
                                "guest version preserved as "
                                f"{name}.conflict-guest")
            else:
                report[name] = f"merge-file error: {r.stderr.strip()}"
        elif h is None and g is not None:
            hp.write_text(g)
            report[name] = "guest-only file copied to host"
        elif g is None and h is not None:
            gp.write_text(h)
            report[name] = "host-only file copied to guest"
        else:
            report[name] = "unchanged"
    return report


def run_arm_b(scn: str) -> dict:
    root = WORK / "armB" / scn
    shutil.rmtree(root, ignore_errors=True)
    host, guest, base_dir = root / "host", root / "guest", root / "base"
    for d in (host, guest, base_dir):
        d.mkdir(parents=True)
    (host / CFG).write_text(BASE)
    (guest / CFG).write_text(BASE)
    (base_dir / CFG).write_text(BASE)  # stored base snapshot at copy-in
    apply_file_edits(scn, host, guest)
    report = arm_b_sync(host, guest, base_dir)
    final = (host / CFG).read_text()
    extra = {p.name: p.read_text() for p in host.iterdir()
             if p.is_file() and p.name != CFG}
    conflict = any(v.startswith("CONFLICT") for v in report.values())
    scored = score(scn, final, extra)
    # If the guest's version lost the main file but survives whole in the
    # conflict copy, that is preservation-with-a-flag, not loss — record it
    # as such instead of letting the main-file marker check call it lost.
    cc = extra.get(f"{CFG}.conflict-guest")
    if (cc is not None and SCENARIOS[scn]["guest_marker"] not in (None, NEWFILE)
            and SCENARIOS[scn]["guest_marker"] in cc):
        scored["guest_edits_preserved"] = True
        scored["guest_preservation_form"] = (
            "conflict copy (main file keeps the host version)")
    return {
        "arm": "B-hash+three-way-merge",
        **scored,
        "conflict_surfaced": conflict,
        "per_file": report,
        "conflict_copy_present": f"{CFG}.conflict-guest" in extra,
        "final_config": final,
    }


# ---------------------------------------------------------------- Arm C
# The CRDT prototype as designed: two replica processes, edits through the
# replica command channel, sync via sync_pair, result read from the
# projected workspace files. S2's guest edit is an OUT-OF-BAND replace of
# the guest's projected file (what a real editor does); the prototype has
# no filesystem watcher/import, by design at this stage.

def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def run_arm_c(scn: str) -> dict:
    s = SCENARIOS[scn]
    root = WORK / "armC" / scn
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    sock = root / "relay.sock"
    host = Replica("host", root / "host", sock)
    guest = Replica("guest", root / "guest", sock)
    try:
        host.cmd(f"create-text {CFG} {esc(BASE)}")
        sync_pair(host, guest)
        base_ok = (root / "guest" / "ws" / CFG).read_text() == BASE
        if "host_insert" in s:
            off, payload = s["host_insert"]
            host.cmd(f"insert {CFG} {off} {esc(payload)}")
        if "guest_insert" in s:
            guest.cmd(f"insert {CFG} {guest_insert_offset(scn)} "
                      f"{esc(s['guest_insert'][1])}")
        if "guest_rewrite" in s:
            # editor save on the guest, outside the replica: temp + rename
            ws = root / "guest" / "ws"
            tmp = ws / ".config.yaml.tmp"
            tmp.write_text(s["guest_rewrite"])
            os.replace(tmp, ws / CFG)
        if "guest_create" in s:
            name, content = s["guest_create"]
            guest.cmd(f"create-text {name} {esc(content)}")
        sync_pair(host, guest)
        d_host, d_guest = host.cmd("dump"), guest.cmd("dump")
        final_host = (root / "host" / "ws" / CFG).read_text()
        final_guest = (root / "guest" / "ws" / CFG).read_text()
        extra = {}
        if (root / "host" / "ws" / NEWFILE).exists():
            extra[NEWFILE] = (root / "host" / "ws" / NEWFILE).read_text()
        return {
            "arm": "C-crdt-prototype",
            **score(scn, final_host, extra),
            "conflict_surfaced": False,
            "note": "the prototype has no conflict-reporting concept; "
                    "Automerge merges silently by construction",
            "setup_converged": base_ok,
            "replicas_converged": d_host == d_guest,
            "projections_identical": final_host == final_guest,
            "final_config": final_host,
        }
    finally:
        host.quit()
        guest.quit()


# ---------------------------------------------------------------- main

def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    results = {}
    for scn in SCENARIOS:
        print(f"=== {scn}: {SCENARIOS[scn]['title']} ===")
        results[scn] = {
            "title": SCENARIOS[scn]["title"],
            "A": run_arm_a(scn),
            "B": run_arm_b(scn),
            "C": run_arm_c(scn),
        }
        a = results[scn]["A"]["pull_force"]
        b = results[scn]["B"]
        c = results[scn]["C"]
        print(f"  A pull--force : host={a['host_edits_preserved']} "
              f"guest={a['guest_edits_preserved']}")
        print(f"  A push        : {results[scn]['A']['push']['outcome']}")
        print(f"  B merge       : host={b['host_edits_preserved']} "
              f"guest={b['guest_edits_preserved']} "
              f"conflict={b['conflict_surfaced']}")
        print(f"  C crdt        : host={c['host_edits_preserved']} "
              f"guest={c['guest_edits_preserved']}")
    out = HERE / "results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
