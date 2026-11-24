# Comparison case for coop #715 — current push/pull vs simpler sync vs CRDT

Built in response to hbrodin's comment on trailofbits/coop#715 (2026-11-24):
start from a concrete workflow, test coop's *current* push/pull behavior
including its overwrite checks, compare simpler sync/conflict handling
against the CRDT prototype, and include the editor whole-file-replace case.
Local work only; nothing here has been posted.

**The concrete workflow** (the one the issue is about): on Firecracker the
workspace is copied into the guest once (`mounts_are_live = false`). The
agent works in the guest for a long stretch while the human keeps editing
the same project on the host. Afterwards the two trees have to come back
together. Every scenario below is that workflow, varying only *what* each
side did to one small tree: a single `config.yaml` (plus, in S4, one new
file). Same base content, same edits, all three arms.

**Reproduce:** `python3 comparison/harness.py` (rewrites
`comparison/results.json`; raw final file contents are in there). The CRDT
prototype's own suite was re-run before and after this work: 14/14 pass.

## coop's current behavior, in brief

Full source read with line citations: [coop-semantics.md](coop-semantics.md).
The short version:

- All transfers are rsync. **No content hashes, no base snapshot** — rsync's
  size+mtime quick check decides what moves, and nothing anywhere can
  express "both sides changed this file."
- **Push** (host→guest) is a mirror (`-az --delete`). Its one guard,
  `check_guest_dirty`, runs `git status --porcelain --untracked-files=no`
  in the guest and aborts the push if tracked files changed or commits are
  unpushed. Gaps: it only exists in git workspaces (no `.git` → vacuous
  pass), it deliberately does not see untracked files, and `--force` skips
  it.
- **Pull** (guest→host) has no per-file check at all. Its only guard is
  refusing to pull into a non-empty directory; with `--force` it installs
  the whole guest tree over the destination, overwriting host files the
  guest never touched (their stale copy-in versions win). The sanctioned
  safe flow is pull-into-a-new-empty-directory, which avoids loss by never
  merging — reconciliation is left to the user.

## The three arms

- **Arm A — coop push/pull as-is.** Source-faithful file-level model using
  the real rsync binary with coop's exact argument sets (each function in
  the harness cites the workspace.rs lines it mirrors). The fixture
  workspace is a git repo with the base committed — the case where coop's
  push guard has its best chance. Push and pull are run as separate fresh
  runs, since each is destructive in a different direction.
- **Arm B — simpler sync.** sha256 of every file compared against a stored
  base snapshot taken at copy-in. One side changed → that side wins. Both
  changed → three-way merge (`git merge-file`, i.e. diff3); a clean merge
  is installed on both sides, an unmergeable one keeps the host version in
  place, preserves the guest version whole as `config.yaml.conflict-guest`,
  and reports the conflict.
- **Arm C — the CRDT prototype**, driven exactly as its own test suite
  drives it (two replica processes, Automerge sync, projected files read
  back). Its command channel exposes insert/create only, so scenario edits
  are insertions; S2's guest edit is the deliberate exception (see below).

## Results

Host/guest = that side's edit survives in the final `config.yaml` (or, for
S4's guest item, the new file survives). "Surfaced" = the user is told
there is a conflict before/without losing work.

| Scenario | A: pull `--force` | A: push (no force) | B: hash + 3-way merge | C: CRDT prototype |
|---|---|---|---|---|
| **S1a** different regions | host ✗, guest ✓, surfaced ✗ | **blocked** by dirty check; nothing merged | host ✓, guest ✓, clean merge | host ✓, guest ✓, surfaced n/a (silent merge) |
| **S1b** same line | host ✗, guest ✓, surfaced ✗ | **blocked** by dirty check | host ✓ in place, guest ✓ **in conflict copy**, **surfaced ✓** | both inserts ✓ but value is `level: info_HOST_GUEST` — see note |
| **S2** guest whole-file replace | host ✗, guest ✓, surfaced ✗ | **blocked** by dirty check | host ✓, guest ✓, clean merge | host ✓, **guest ✗ — rewrite silently lost** |
| **S3** guest-only edit (control) | host —, guest ✓ | **blocked** by dirty check (guard fires even though only the guest changed anything) | host —, guest ✓ | host —, guest ✓ |
| **S4** host edits config; guest creates new file | host ✗, guest ✓ (new file arrives), surfaced ✗ | **proceeded — guard did not fire; mirror push deleted the guest's new file** | host ✓, guest ✓ | host ✓, guest ✓ |

Arm A addendum — `push --force` (the override the guard's own error
message offers): in every scenario where the guard blocked (S1a, S1b, S2,
and the S3 control), forcing the push destroyed the guest's edits
(guest ✗ across the board). The guard is load-bearing.

## Per-scenario notes

**S1a — different regions.** The happy case for both challengers: diff3
merges cleanly, the CRDT merges cleanly. coop pull `--force` installs the
guest's file wholesale and the host's `timeout: 30` line is gone; nothing
reports it. coop push does exactly what it was designed to do — refuses,
shows the guest's git status, and points at the review-directory flow.
That is a surfaced conflict, but the resolution is entirely manual.

**S1b — same line.** The most informative scenario. Host appended `_HOST`
to `level: info`, guest appended `_GUEST`. Arm B's diff3 cannot merge two
changes to one line, so it does the honest thing: conflict reported, host
version in place, guest version preserved whole in
`config.yaml.conflict-guest`. Arm C "preserves both edits" textually —
and produces `level: info_HOST_GUEST`, a value neither side wrote and no
validator would accept, with no signal that anything is wrong. Character-
level merge guarantees no *characters* are lost; it cannot guarantee the
merged value means anything. For config files and code, a surfaced
conflict can be worth more than a silent merge.

**S2 — editor whole-file replace.** The guest editor rewrote the whole
file via temp-file + rename (changing `level` and `cache`), while the host
edited in place. Arm B doesn't care how the guest file was produced — it
compares content against the base, diff3 merges the disjoint changes
cleanly, both sides survive. **Arm C loses the guest's rewrite entirely:**
the replace happened outside the guest replica, the prototype has no
filesystem watcher/import, and the next sync's projection quietly restored
the document's version over the editor's file. Replicas converged,
projections identical, no error anywhere — the loss is invisible in every
signal the prototype emits. This is the prototype's own documented
amendment #2 ("changes made outside the CRDT must be detected and
reconciled — never silently treated as current") failing exactly as
predicted, now with a reproduction. As designed today, the CRDT arm is
*worse than the simpler arm* on the very case hbrodin asked about — an
integration would need an import/reconciliation layer before it is safe
near real editors, at which point it has grown a component very much like
Arm B's change detection.

**S3 — control.** All arms pass the retrieval direction. Note coop push is
blocked even here: the guard cannot distinguish "guest changed, host
didn't" (safe to pull, and push is simply the wrong command) from a true
conflict — it just refuses, which is safe but blunt.

**S4 — the untracked gap, end to end.** Host edited `config.yaml`; the
guest created a brand-new `agent_notes.txt`. Pull `--force` brings the new
file across but reverts the host's config edit (the guest's stale copy
wins — the guest never opened that file). Push is the mirror-image
failure: the new file is untracked, `check_guest_dirty` is run with
`--untracked-files=no`, the guard **does not fire**, and the mirror push
(`--delete`) deletes `agent_notes.txt` from the guest. No warning in
either direction. This is coop-semantics.md gap 1 demonstrated, not just
read. Arms B and C both keep both sides' work (B by hash comparison, C by
divergent creation — its test (b) case).

## Complexity

| Arm | Code | External deps |
|---|---|---|
| A (coop as-is) | Already shipped: `src/workspace.rs` is 2,944 lines (push/pull/guard ≈ 460 of them) | rsync, git (for the guard), SSH |
| B (model in this harness) | ~100 lines of Python (hash compare + merge driver) | git (`merge-file`); stdlib hashing |
| C (prototype) | 551-line Rust replica + 93-line Python driver (+61 lines of scenario driver here) | `automerge` 0.7 crate (+ `sha2`, `hex`), a sync transport, a projection layer — and, per S2, an out-of-band change import/reconciliation layer that does not exist yet |

Arm B's number is a model, not an integration — productizing it inside
coop (state storage, deletion semantics, nested trees, the untrusted-guest
policy questions hbrodin raised) is real work. But the gap between "~100
lines of model" and "a CRDT plus its import layer" is the honest shape of
the comparison he asked for.

## Limitations — read before quoting any of this

- **No live VM was booted.** Arm A is a file-level model: real rsync,
  coop's exact flags, coop's guard logic re-run against a real git repo,
  but local paths instead of SSH and no Lima/Firecracker runtime. The
  file-level semantics are where every loss above occurs; VM plumbing
  could add failures, not remove these.
- **Arm A is a model, not coop's binary.** coop itself was not built or
  run (its push/pull require a live instance). Every modeled behavior
  cites the source lines it mirrors in `coop-semantics.md`; the rsync
  argument sets and the guard's git command are copied verbatim.
- **Fixture scale and shape.** One text file, line-oriented edits, no
  nested directories, no symlinks, no permission modes, no binary files,
  no deletions by either side (deletions *by rsync `--delete`* do occur
  and are reported). The CRDT prototype only handles regular files at all
  at this stage.
- **Arm C driver patch (disclosed).** The replica's stdin channel is
  line-based, so to feed it multi-line file content the comparison added a
  `\n`-unescape to the `insert`/`create-text` *input parsing only*
  (documented in the source at `src/bin/replica.rs`, `unescape()`). The
  Automerge operations, sync protocol, and projection are untouched, and
  the prototype's own suite still passes 14/14 after the change.
- **Arm C edits are insertions only**, because that is all the prototype's
  command channel exposes (no delete/replace operation). S1b's "same
  line" case is therefore two insertions at the same offset rather than
  two rewrites of the line's value; Arm B faced the same two insertions
  at file level, so the comparison is symmetric, but a value-rewrite
  variant of S1b was not run against the CRDT.
- **Arm A's fixture is a git workspace.** That is coop's guard at its
  best. In a non-git workspace the push guard never fires and S1a/S1b/S2
  pushes would behave like the `--force` override row: guest work
  destroyed without warning.
- mtimes in Arm A were set explicitly (base < host edit < guest edit) to
  make rsync's size+mtime quick check deterministic. In the wild, an edit
  that preserves both size and mtime would be skipped by rsync entirely —
  another silent-loss path this fixture does not exercise.

## Bottom line

hbrodin's skepticism is borne out on the current evidence. The simpler
hash + three-way-merge arm matched or beat the CRDT prototype in every
scenario — decisively in S2, where the prototype silently lost the
editor's whole-file rewrite, and on honesty in S1b, where it surfaced a
conflict instead of emitting an invalid merged value. The CRDT's genuine
edge remains the one from the original issue: authority-free convergence
with provenance, if an integration ever needs it. But as a *starting
point* for closing the Lima/Firecracker divergence, the comparison says
what he predicted: start with change detection and conflict handling;
reach for CRDTs only if a concrete case defeats them. None of the five
cases here did.
