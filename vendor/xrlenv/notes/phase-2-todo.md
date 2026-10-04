# Phase-2 To-do — captured deferrals

> **Status:** placeholder for phase-2 planning. Created
> 2026-05-10 when several items deferred out of phase 1. Use this
> file to capture **decisions already made** so phase-2 planning
> doesn't relitigate them, and to track items moved from
> `notes/phase-1-to-do.md` with their original scope intact.

## Phase-2 thesis (working sketch)

Phase 1 delivered an **operationally-honest cluster substrate for
agentic benchmark evaluation** — real harnesses (swebench,
terminal-bench-2, harbor) drive case-2/3 rollouts via the
docker-py drop-in against a real GCP/AWS cluster, with full
operator surface (build apply / cancel / calibrate /
build-on-acquire / skip-if-present), observability, and security
hardened for non-loopback deployment.

**Phase 2 layers RL training on top of that substrate.** Real
Slime (primary) and verl (secondary) drive rollouts via the SDK
against the same cluster phase 1 hardened. The state store
swaps SQLite → Redis to lift the concurrency ceiling from
100-200 → 500-1k+. CubeSandbox microVM lands as a second
backend alongside Docker. The benchmark roster broadens
(OSWorld, web_search) once egress-allowlist networking ships.

Concretely, phase-2 thesis in one sentence: **"trainer
integration + 5x concurrency + microVM backend + broader
benchmark roster."**

## Slice progress (skeleton — fill in as slices kick off)

| Slice | Status | Highlights |
|---|---|---|
| P2.1 — Trainer integration | queued | Slime + verl adapters (B1.1-B1.5); reward modes (B1.6); `client.warmup` (B3.7); correctness gate via Slime + swebench oracle (B1.2). |
| P2.2 — Redis StateStore | queued | B6.1 (Redis); B6.2 (warm-standby); B8.4 (Redis bootstrap recipe). Lifts concurrency ceiling to 500-1k+. |
| P2.3 — CubeSandbox backend | queued | B4.1 (CubeSandbox backend); B8.2 (CubeSandbox install in bootstrap); A8/D14 (mixed-backend capacity accounting). |
| P2.4 — Broader benchmark roster | queued | B2.2 (OSWorld plug-in); B2.3 (web_search plug-in); B5.5/B5.6 (egress allowlist + network audit). |
| P2.5 — Performance polish | queued | B3.5 (eStargz lazy-load image); B4.3 (warm pools per template); B4.4 (24-hour resource ring buffer); B7.4 (admin 24-hour metric history). |
| P2.6 — Trainer-driven analysis | queued | B9.1-B9.2 (`xrlenv analyze` four-pass); B7.8 (trajectory viewer token-level overlay, lands with B1.6); B6.5 (object-store trajectory archival). |

Order is **not strict** — P2.2 (Redis) and P2.3 (CubeSandbox)
have no dependency on P2.1 (trainer integration); they can ship
in parallel. P2.1 is the highest-value win, so it goes first.

---

## Items deferred from phase 1

Original spec text + cost preserved so phase-2 doesn't redo the
sizing.

### Trainer integration (was B1 — "the core phase-1 win")

Moved out of phase 1 2026-05-10. Rationale: phase 1's slim
pivot reached "operationally-honest evaluation substrate" via
case-2/3 drop-ins; trainer integration adds RL-training-loop
complexity (sink-aware readers, warmup, reward-mode plumbing)
that doesn't share critical dependencies with what phase 1
finishes on. Folding it into phase 2 lets phase 1 exit on
hardening (observability, auth, mTLS, bootstrap, scale-gate)
instead of dragging on.

| # | Item | Source | Cost |
|---|---|---|---|
| B1.1 | `xrlenv/adapters/slime.py` — `make_rollout_fn(template, sdk_client, deadline, policy_factory)` | spec 11, 05 | M |
| B1.2 | `examples/slime/swebench_verified_smoke.py` — the correctness gate driver | spec 11 | S |
| B1.3 | `xrlenv/adapters/verl.py` — Ray actor `XRLEnvRolloutWorker.generate(...)` | spec 12, 05 | M |
| B1.4 | `examples/verl/swebench_verified_smoke.py` (best-effort) | spec 12 | S |
| B1.5 | Sink-aware readers — `slime-sample` + `verl-dataproto` | spec 17 | S |
| B1.6 | Reward modes `external_final` and `token_level` | spec 02 | M |
| B3.7 | `client.warmup(templates, instance_ids, ...)` SDK + `WarmupReport` callback (was paired with B1.1) | spec 05, 15 | S |
| B7.8 | Trajectory viewer: token-level overlay tab (paired with B1.6) | spec 17 | M |

**Phase-2 acceptance for trainer integration**: real Slime drives
the swebench-verified oracle smoke at phase-0-scale (8 instances)
end-to-end with cluster-side rollouts, no fabricated rewards.
Mirrors the original phase-1 Gate-1 correctness gate, retargeted
to phase 2.

### Redis StateStore (was B6.1 / B6.2 / B8.4)

Already-decided deferral (Q4 revised 2026-05-02). Phase-1 target
is 100-200 concurrent containers — comfortably within SQLite
WAL's ~1-3k writes/sec. Redis lands in phase 2 alongside the
500-1k+ target.

| # | Item | Source | Cost |
|---|---|---|---|
| B6.1 | Redis StateStore | spec 03, 20 | L |
| B6.2 | Active + warm-standby control plane via shared Redis | spec 03 | M |
| B8.4 | Redis bootstrap recipe (single-node + standby) | spec 09, 20 | S |

**Pre-baked design answers** for the Redis impl live in
`notes/deferred_audit_todos.md` under B6.1; phase 2 shouldn't
relitigate them.

### CubeSandbox microVM backend (was B4.1 / B8.2 / A8/D14)

Already-decided deferral (Q3). Phase 1 ships Docker-only as
first-class citizen; CubeSandbox image story is genuinely
different + warrants its own slice.

| # | Item | Source | Cost |
|---|---|---|---|
| B4.1 | CubeSandbox backend | spec 01, 04, 10 | L |
| B8.2 | CubeSandbox install in bootstrap | spec 09, 01 | S |
| A8/D14 | Mixed-backend capacity accounting | spec 10 | M |

### Benchmark roster expansion (was B2.2 / B2.3)

Moved out of phase 1 2026-05-10. swebench-verified +
terminal-bench-2 + harbor already validate the slim pivot's
drop-in pattern. OSWorld and web_search are useful but not
load-bearing for phase 1's "evaluation substrate" thesis.

| # | Item | Source | Cost |
|---|---|---|---|
| B2.2 | `xrlenv_plugins/benchmarks/osworld/` (Pattern B — environment-class plug-in matching OSWorld's existing extension shape) | spec 06 | L |
| B2.3 | `xrlenv_plugins/benchmarks/web_search/` | spec 06, 14 | M |
| B5.5 | Egress-allowlist networking (DNS + iptables per sandbox) — required for web_search | spec 07 | M |
| B5.6 | Per-sandbox network audit (lands with B5.5) | spec 07, 08 | S |

### Performance polish

Moved out of phase 1 2026-05-10. These lift the latency floor +
disk-budget efficiency under high concurrency. Useful, but the
concurrency floor that justifies them is trainer-driven — defers
with trainer integration.

| # | Item | Source | Cost |
|---|---|---|---|
| B3.5 | Lazy-load image support — eStargz (Docker only) | spec 06, 15 | M |
| B4.3 | Warm pools per template — `warm_pool: {min_idle, max_idle}` | spec 04, 06 | M |
| B4.4 | 24-hour disk-based ring buffer for resource samples | spec 04, 08 | S |
| B6.3 | Pool-accounting persistence (warm-pool state survives restart) — lands with B4.3 | spec 04, 20 | S |
| B6.5 | Optional object-store integration for trajectory archival | spec 20 | M |
| B4.2 | Function-Call execution mode + `client.invoke()` (was Q5 — phase 1.5) | spec 01, 05 | M |

### Benchmark analysis (was B9.*)

Moved out of phase 1 2026-05-10. `xrlenv analyze` is most
valuable when the operator has a large benchmark catalog to
introspect; under phase-1's three-benchmark-validation scope the
manual approach is fine. Defers with trainer integration so the
analysis-driven workflow lands when trainer-driven runs justify
it.

| # | Item | Source | Cost |
|---|---|---|---|
| B9.1 | `xrlenv analyze` CLI — four-pass (image introspection → clustering → consolidation → equivalence smoke) | spec 16 | L |
| B9.2 | `xrlenv analyze --remote analyzer-vm` | spec 16 | S |

Note: B9.3 (`EnvAdapter.enumerate_tasks()` contract) is
**N/A under the slim pivot** — the EnvAdapter-shaped enumeration
contract was for pre-slim-pivot in-tree benchmark plug-ins; the
drop-in path puts task enumeration inside each harness. No
phase-2 work needed for this row.

---

## Deferred-by-design (explicitly NOT phase-2 work)

Items where the design decision is "won't do unless an operator
surfaces a concrete need." Distinct from the "deferred to phase 2"
rows above — those are queued; these are off the roadmap until
new evidence appears.

### Entry-level delta dispatch for ``xrlenv build apply``

**Property the operator might want.** When re-applying a changed
``build-plan.yaml`` (size hint edits, an image_ref removed, a new
entry added), only re-dispatch the entries that **actually
changed** relative to the prior plan_id's assignments. Skip the
unchanged entries entirely — not even an ensure_present round-trip.

**Why deferred-by-design.** The operational benefit is small, and
the alternative path (``--skip-if-present``, shipped 2026-05-10
in commit f32080b) achieves the same outcome with simpler
semantics:

- ``--skip-if-present`` does one wire round-trip per entry to
  check local docker tag presence; if present, skip the build.
  Sub-second per entry on a healthy admin.
- The implementation cost of "true delta dispatch" includes:
  defining what "changed" means (size hint? source spec? labels?
  preferred_home_count?); choosing the prior plan_id to compare
  against (latest applied? latest completed?); handling
  concurrent applies racing the comparison; deciding whether
  a partial-failure plan counts as a valid baseline.
- Each of those is a policy call that varies by operator workflow.
  Baking one set of policies into core would push some operators
  toward forks or workarounds.

**Recommended workflow today.** For warm-cluster re-applies:

```bash
xrlenv build apply --plan <yaml> --skip-if-present \
    --connect-host <admin>
```

This gives operators the operationally-equivalent fast path
without the policy ambiguity. Documented in
``docs/technical_details/images/build_plan.md`` § "Warm-cluster
fast path: ``--skip-if-present``".

**When to revisit.** If multiple operators independently surface
a use case where ``--skip-if-present`` is materially worse than
true delta dispatch (e.g. plans with hundreds of entries where
the sub-second-per-entry RTT adds up to material wall-clock), or
if a concrete bug emerges in the workaround. Until then, the
``--skip-if-present`` shape is the answer.

---

## Out-of-phase-2 deferrals (for awareness)

Items already marked for **phase 3** in `specs/00-overview.md` —
listed here only so phase-2 planning doesn't accidentally pull
them in.

- **Sandbox sessions + preemption-safe resume** (spec 18) — phase 3.
- **k8s / MIG / ASG autoscale** — phase 2 might pull some of this
  in if cluster topology justifies it, but the dominant push to
  managed orchestration is phase-3 territory.
- **Multi-tenant isolation hardening** — phase 3.
- **Aspirational extreme density** (KSM, overcommit, 3FS) — phase 3.

Phase 2's existing scope is already substantial without these.

---

## How to use this file

- When a phase-2 slice kicks off: lift its scope from the
  appropriate "deferred" section into the active slice planning.
- When a deferral becomes obsolete (e.g. an item ships
  opportunistically during phase 1.x polish): strike it through
  here with a CLOSED tag pointing at the commit.
- New phase-2-only items (didn't exist as phase-1 deferrals):
  add a new section above the "deferrals" block, not interleaved
  with them.

Phase-2 acceptance criteria will land in
`notes/phase-2-acceptance.md` once the phase-2 thesis converges
into a concrete two-gate exit shape.
