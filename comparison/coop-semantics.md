# coop push/pull semantics — source read

Source: `trailofbits/coop` cloned 2026-11-24 (`git clone --depth 1`).
All line numbers refer to `src/workspace.rs` unless another file is named.
This document describes only what the code does; the comparison harness in
this directory models these semantics at the file level (see COMPARISON.md
for the fidelity basis).

## Why there is a copy step at all

- The backend trait declares `mounts_are_live()` (`src/backend.rs:1255`).
- `FirecrackerBackend` returns `false` (`src/backend.rs:1736`, impl block
  starting `src/backend.rs:1267`); `LimaBackend` returns `true`
  (`src/backend.rs:1972`). On Lima the host directory is mounted live in the
  guest and no sync step exists (`record_mount_state` is called directly,
  workspace.rs:381-382). On Firecracker the workspace crosses only via
  explicit rsync copies: `sync_mounts` at start (workspace.rs:343-352) and
  the `push`/`pull` commands thereafter.

## Transport common to all transfers

- Everything is rsync over SSH. Base argument set (`rsync_base_args`,
  workspace.rs:541-558): `-az` plus the SSH command; a `.gitignore` filter
  rule (`--filter=:- .gitignore`); `DEFAULT_EXCLUDES` = `node_modules/`,
  `target/`, `__pycache__/`, `.venv/`, `.coop/` (workspace.rs:23-29).
- Git metadata handling is direction-dependent (workspace.rs:545-553):
  with `exclude_git` an `--exclude=.[gG][iI][tT]` rule is added; otherwise a
  filter explicitly *includes* `/.git/***`.
- **No `--checksum` anywhere.** rsync therefore decides what to transfer
  with its default quick check — file size and mtime — not content hashes.
  coop keeps no base snapshot, no per-file hashes, and no record of what
  either side looked like at the last sync. There is nothing in the
  transfer path that can express "both sides changed this file."
- Both sides must have rsync; the guest is probed first
  (`require_guest_rsync`, workspace.rs:584-589).

## Copy-in (initial workspace transfer)

- `copy_workspace` (workspace.rs:161-170) rsyncs host → guest in
  `SyncMode::Copy`. In `rsync_to_guest` (workspace.rs:591-618), `--delete`
  is added **only** for `SyncMode::Mirror` (workspace.rs:603-605), so the
  initial copy does not delete guest files. There is **no cleanliness
  check** on this path.

## Push (host → guest) — `push`, workspace.rs:270-298

1. Unless `--force`, push first runs `check_guest_dirty`
   (workspace.rs:285-287).
2. The transfer itself is `rsync_push` → `SyncMode::Mirror`
   (workspace.rs:569-576), i.e. `rsync -az --delete …`: the guest tree is
   made to match the host tree, including **deleting guest files that are
   absent on the host**.

### The push guard: `check_guest_dirty`, workspace.rs:912-966

Runs over SSH inside the guest workspace:

- The entire check is conditional on a `.git` entry existing in the guest
  workspace (`if [ -e <guest_path>/.git ]`, workspace.rs:918-920). **No
  `.git` → no output → the check passes vacuously.** There is no fallback
  content check for non-git workspaces.
- Inside a git workspace it runs
  `git status --porcelain --untracked-files=no` (workspace.rs:923) and, if
  an upstream exists, counts unpushed commits (`git rev-list --count
  '@{u}..HEAD'`, workspace.rs:924-929).
- Any output → push aborts with the guest's status shown and instructions
  to pull into a *missing or empty review directory*, or to re-run
  `coop push --force` (workspace.rs:959-965).
- Probe failures (exit 20/21/22: cannot open dir, `git status` failed,
  `git rev-list` failed) also abort the push — the guard fails closed
  (workspace.rs:943-957).

### Gaps in the push guard

1. **Untracked files are deliberately invisible.** `--untracked-files=no`
   (workspace.rs:923) is a considered choice — the comment at
   workspace.rs:913-917 says untracked files are "almost always host-side
   noise." But a *new file the agent created in the guest* is also
   untracked: the guard does not fire for it, and the mirror push that
   follows deletes it from the guest via `--delete` without any warning.
2. **Git workspaces only.** A copied workspace without `.git` (or with
   `.git` excluded at copy-in) gets no guard at all.
3. **One direction only.** The guard protects the guest's work from push.
   There is no symmetric "host changed since copy-in" check anywhere.
4. **Point-in-time.** The check and the rsync are separate SSH operations;
   nothing locks the guest tree between them.
5. `--force` skips the guard entirely (workspace.rs:285), and the guard's
   own error message names `--force` as the override.

## Pull (guest → host) — `pull`, workspace.rs:305-337

1. Unless `--force`, pull refuses if the destination directory is
   non-empty — `pull_destination_is_nonempty` (workspace.rs:312,
   968-985) treats *any* directory entry as non-empty. The refusal text
   (workspace.rs:313-320) says coop "does not inspect the destination
   with Git because its files and repository metadata may be untrusted,"
   and offers `--force` as the override. This is an all-or-nothing,
   directory-granularity guard: it cannot distinguish "destination is the
   live workspace with new host edits" from "destination has one stale
   file."
2. Pull prints `PULL_UNTRUSTED_WARNING` (workspace.rs:325; text at
   workspace.rs:40-…) — the pull path is designed around an untrusted
   guest, not around merge correctness.
3. Transfer is two-stage (`rsync_pull`, workspace.rs:620-648):
   - Stage 1: rsync from the guest into a private host-side staging
     directory, using the base args with `exclude_git = true`
     **unconditionally** (`rsync_pull_args`, workspace.rs:650-654) — guest
     `.git` never crosses.
   - Stage 2: `install_staged_pull` (workspace.rs:667-681) runs a second,
     host-local rsync with exactly `-a --exclude=.[gG][iI][tT]`
     (`install_staged_pull_args`, workspace.rs:683-693) from staging into
     the destination.
4. **Stage 2 has no `--delete` and no conflict check of any kind.** Every
   guest file whose size or mtime differs from the destination file
   overwrites it. Files the guest never touched are also overwritten if
   the host edited them (host edit ⇒ size/mtime differ from the guest's
   stale copy ⇒ the stale copy wins). Host-only files survive (no
   `--delete`); guest-only files are added.

### Gaps on the pull side

1. **No per-file overwrite check at all** once the directory-level refusal
   is bypassed with `--force` (or the destination is empty). Nothing
   compares content, hashes, or a base version.
2. **Untouched-by-guest files are not protected**: pull installs the whole
   guest tree, so a host edit to a file the agent never opened is reverted
   to the guest's copy-in version.
3. The sanctioned safe flow — pull into a *new empty* directory — avoids
   loss by construction but produces two divergent trees and leaves
   reconciliation entirely to the user. coop itself never merges.

## Mount sync (Firecracker `up` path)

- `sync_mounts` / `sync_mount_contents` (workspace.rs:343-379) create the
  guest directories and call the same `rsync_push` (Mirror mode,
  `--delete`) per mount, with **no** `check_guest_dirty` call — the dirty
  guard exists only on the interactive `push` command path.

## Summary

| Property | Push | Pull |
|---|---|---|
| Transport | rsync `-az --delete` (Mirror) | rsync `-az` to staging, then rsync `-a` install |
| Pre-overwrite check | `check_guest_dirty`: git tracked modifications + unpushed commits, git workspaces only, skipped by `--force` | Destination non-empty refusal only; skipped by `--force`; no per-file check |
| Content comparison | rsync size+mtime quick check | rsync size+mtime quick check |
| Base snapshot / hashes | None | None |
| Deletions propagated | Yes (`--delete`) | No |
| Conflict surfacing | Refusal with guest git status (push only) | None per-file; directory refusal only |
| Merge capability | None | None |
