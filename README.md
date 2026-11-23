# MERGEBRIDGE — Stage 0/1 prototype

A two-process, no-VM proof of the MERGEBRIDGE concept (Research & Innovation
Engine candidate `mergebridge-coop-automerge`, JEV gate PASS 0.706,
prototyping authorized by Darell 2026-11-23).

## The claim under test

trailofbits/coop syncs the workspace between host and guest by one-time copy
on Firecracker (`mounts_are_live = false`). Copying has no representation
for concurrent edits: if the human edits on the host while the agent edits
in the guest, one side's work is destroyed by definition (last copy wins),
and the system cannot even say what was lost.

MERGEBRIDGE transplants Automerge's CRDT document + sync protocol into that
seam: host and untrusted guest hold **replicas of one workspace document** —
text files as Automerge text, binaries as SHA-256 hash references only —
syncing over a stream. CRDT merge is authority-free, so the trust model
survives: the guest replica can interleave operations but can never mint
host authority.

## What this prototype proves (and what it doesn't)

Two copies of the same Rust binary run as separate processes — a "host"
replica and a "guest" replica — sharing one Automerge document over a Unix
domain socket (standing in for coop's vsock relay; the socket exists only
during a sync session, no network listener is created). A Python harness
drives them through a scripted scenario. The automated tests assert:

- **(a) Convergence under concurrency** — both sides edit the *same* text
  file simultaneously, while disconnected; after one sync session both
  edits are present in the merged document, on both replicas, and in the
  projected files. **This is the falsifier: if (a) failed, the concept
  would fail.**
- **(b) Divergent creation** — files created on opposite sides while
  disconnected appear on both replicas after sync, byte-identical; a
  redundant sync changes nothing (heads stable).
- **(c) Binary handling** — a binary file exists in the document only as
  `{sha256, size}`. The test scans the saved document bytes and asserts the
  binary's marker bytes are absent; the content itself crosses the same
  channel out-of-band and is verified against the hash on receipt.
- **(d) The falsifier contrast** — the identical concurrent-edit scenario
  replayed under the copy model (last-copy-wins) loses the host edit;
  MERGEBRIDGE keeps both. The contrast is the point of the prototype.

Result on 2026-11-23: **14/14 checks pass** (suite run repeatedly).

This prototype does **not** touch coop's code, does not run a VM, uses full
(not incremental) filesystem projection, and starts a fresh Automerge sync
state per session (correct, but chattier than a persistent sync state).
Those are Stage 1–3 concerns in the synthesis blueprint, in
`~/workspace/research-innovation-engine/candidates/mergebridge-coop-automerge.json`.

## Binding constraints honored (from the synthesis)

- No new network listener — sync rides a local Unix socket only for the
  duration of a session (the prototype's stand-in for coop's existing
  vsock/SSH channels).
- No secret-bearing path exists in the document schema, by construction.
- No binary content inside the CRDT — hash references only.
- The copy/rsync path is untouched; coop is not modified or vendored here.

## Layout

- `src/bin/replica.rs` — the replica process (both roles). Commands on
  stdin (`init-fixture`, `insert`, `create-text`, `create-binary`, `sync`,
  `dump`, `heads`, `quit`); state in `<dir>/doc.automerge`, blobs in
  `<dir>/blobs/<sha256>`, projection in `<dir>/ws/`.
- `tools/harness.py` — spawns/choreographs the two processes.
- `tests/run_tests.py` — the (a)–(d) proof suite.
- `demo.py` — narrated two-act demonstration (copy model loses, merge wins).

## Run it

```sh
cargo build --release        # needs a Rust toolchain; automerge 0.7
./run_tests.sh               # builds if needed, runs the proof suite
python3 demo.py              # narrated demonstration
```

## Strains observed (honest list)

- **Bind/connect race in the harness**: the guest could connect before the
  host finished binding (or hit a stale socket file from a prior session)
  and the host then waited in `accept()` forever. Fixed with a connect-retry
  loop in the guest role. A real coop integration rides an already-open
  channel, so this is a harness artifact, not a concept strain.
- **Document/filesystem impedance is visible even at fixture scale**: the
  prototype's schema handles only regular text/binary files — no modes,
  symlinks, or nested directories yet. The synthesis flags this as the
  design's main battleground (plus churn policy for build artifacts); the
  prototype confirms the merge core and defers the mapping fight to Stage 1.
- **Fresh sync state per session** re-advertises history each session
  (still only 4 protocol rounds at this scale). A persistent per-peer sync
  state is the obvious optimization for the real integration.

## Design amendments — external QC review, 2026-11-23

An independent QC review of this design (external model seat, Research &
Innovation Engine; full review in `qc-review-chatgpt-free-2026-11-23.md`)
produced three amendments, adopted into the design record:

1. **Convergence is not acceptance — an explicit authority policy is
   required.** Automerge guarantees that replicas converge; it says
   nothing about which guest operations the host should *accept*.
   "Authority-free merge" therefore does not by itself preserve coop's
   trust model. Stage 1 must add a host-side policy layer, independent of
   the CRDT: guest operations are admitted only within policy (workspace
   scope only; protected paths denied; operation-batch and size limits
   enforced against flooding/churn; binary references hash-verified *and*
   policy-checked). The host remains the final authority over what is
   projected to its filesystem.
2. **Projection must be transactional, incremental, and bypass-aware.**
   The document/filesystem translation is the design's main risk surface:
   updates arriving mid-projection must never leave stale or partial
   state on disk (versioned snapshots, atomic file replacement, recovery
   from interrupted projection), and changes made to the workspace by a
   process *outside* the CRDT (an editor, a build tool) must be detected
   and reconciled — never silently treated as current.
3. **Binary references need a lifecycle — identity is not availability.**
   A `{sha256, size}` reference proves what a blob *is*, not that it has
   *arrived*. The design must specify states and handling for pending,
   missing, interrupted, and superseded blob transfers, plus garbage
   collection — otherwise the document can converge perfectly while the
   workspace remains incomplete.

The review's evidence demands (≥1,000 randomized concurrent-operation
scenarios with zero silent loss; an adversarial-guest harness; a real
Firecracker integration compared against the rsync path; performance
baselines) are adopted as the Stage 1 acceptance bar.
