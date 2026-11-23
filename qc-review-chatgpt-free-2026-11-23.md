# MERGEBRIDGE — External QC Review (ChatGPT, free tier)

- **Date:** 2026-11-23 (~12:43 PM ET)
- **Reviewer seat:** ChatGPT via Darell's signed-in browser session (account "Mr Mac", Free plan — free-tier model, NOT a named frontier model). First test of the Research & Innovation Engine's external-model seat (Darell's direction, 2026-11-23).
- **Chat:** https://chatgpt.com/c/6b047b44-b9a4-83e9-837e-f58f27cc92d8 ("Engineering Design Review")
- **Brief given:** adversarial QC — weakest assumption, most likely integration failure, evidence demanded before the next build stage. Input was the design summary + prototype evidence + JEV scores + stated limitations.
- **Status of this document:** advisory input only. Claims unverified beyond Astro's read against the design record; JEV and Darell remain the authority.

## Verdict (reviewer's)

"Conditional rejection — do not advance to production integration yet." Overall 6.5/10 (coherence 8, novelty 7, prototype evidence 6, security assurance 4, integration readiness 5, production readiness 3). Decision: advance to a controlled integration prototype, not production.

## 1. Weakest assumption — "conflict-free does not mean trust-free"

The claim that authority-free CRDT merging preserves coop's trust model. CRDTs give deterministic convergence, not authorization: a hostile guest can submit structurally valid but destructive changes, flood operations to exhaust host CPU/memory, churn update/delete cycles, and exploit conflicts with legitimate host changes. Requires an explicit host-side authority policy independent of Automerge (workspace files allowed; protected paths denied; oversized batches rejected; binary refs hash-verified AND policy-checked). Host must remain final authority over filesystem projection.

## 2. Most likely integration failure — projection races

Not CRDT correctness — the translation between CRDT state and filesystem state. Guest updates arriving mid-projection can leave the host writing stale or partial state; plus atomic replacement/partial writes, rename/delete races, full-projection cost on large workspaces, build-artifact sync storms, binary refs pointing at content not yet arrived, and host processes observing half-updated workspaces. Wants: projection ordering, locking, transaction boundaries, recovery from interrupted sync.

## 3. Evidence demanded before the next stage

- **A. Filesystem consistency testing (critical):** concurrent modify/modify, modify/delete, rename-vs-modify, directory create/delete, interrupted writes, process kill mid-sync, restart/recovery — ≥1,000 randomized concurrent-operation scenarios; zero silent data loss or document/filesystem divergence.
- **B. Adversarial guest testing (critical):** op flooding, huge insertions, update/delete churn, malformed binary refs, hash mismatches, pathological nesting, protected-path attempts; resource limits enforced, host stays operational.
- **C. Projection architecture:** versioned snapshots, incremental projection, atomic replacement, explicit conflict policy, interrupted-projection recovery, and detection of filesystem changes that bypass the CRDT.
- **D. Real coop integration:** inside the actual Firecracker backend over vsock/SSH, incl. lifecycle, concurrent agents, host edits during runs, large workspaces, cleanup; compared against the rsync implementation.
- **E. Performance benchmarks vs rsync:** initial/incremental sync time+bytes, large-file overhead, high-edit-frequency CPU/latency, projection duration, recovery time.

## Extra challenge — binary-reference lifecycle

A {sha256, size} ref establishes content *identity*, not *availability*. Needs a lifecycle: pending transfers, missing blobs, garbage collection, interrupted uploads, superseded references. Otherwise the document converges while the workspace stays incomplete.
