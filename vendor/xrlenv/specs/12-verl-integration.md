# 12 — verl Integration

## Purpose

Secondary adapter for [verl-project/verl](https://github.com/verl-project/verl),
the community-maintained RL framework. Lower priority than Slime —
Slime is the primary target trainer — but ships in the same phase as
Slime to reach feature parity.

**Phase**: ships in **phase 1** alongside the Slime adapter. Phase 0
delivers the underlying SDK; phase 1 wires both adapters on top.

verl is an **optional install**. `xrlenv/adapters/verl.py` only
imports it when the user asks for it.

## What verl expects

verl is Ray-actor-native. Trainers (`RayPPOTrainer` and successors)
spawn rollout workers as Ray actors and call them with `DataProto`
batches. The adapter therefore *is itself a Ray actor*, not a plain
function (unlike the Slime adapter).

verl's per-rollout output is finer-grained than Slime's `Sample`:
responses, attention masks, position ids, rewards, ref-logprobs
placeholders, plus optional per-token reward signals. The adapter
takes our `Trajectory` and converts in one place.

**API churn caveat**: verl's classes (`RolloutWorker`, `ActorWorker`,
`DataProto` shape) have changed across versions. The adapter tracks
the **`main` branch of `verl-project/verl`** and re-pins after each
verl release. `examples/verl/requirements.txt` references a specific
commit SHA so an older check-out of XRLEnv is reproducible. Concrete
class signatures are deferred to implementation time — speculating on
past or future shapes adds nothing here.

## Adapter shape

```python
# xrlenv/adapters/verl.py
import ray
from xrlenv import Client

@ray.remote
class XRLEnvRolloutWorker:
    def __init__(self, control_plane: str, template: str,
                 hard_deadline: float, soft_deadline: float | None = None):
        self._client    = Client(control_plane)
        self._template  = template
        self._hard      = hard_deadline
        self._soft      = soft_deadline

    def generate(self, batch: "DataProto", actor_engine) -> "DataProto":
        """verl's rollout entrypoint. Sync facade over async batch_rollout."""
        prompts = _extract_prompts(batch)
        policy  = _wrap_engine_as_policy(actor_engine)

        result  = self._client.sync.batch_rollout(
            template=self._template,
            inits=[{"prompt": p} for p in prompts],
            policy=policy,
            hard_deadline=self._hard,
            soft_deadline=self._soft,
            concurrency=batch.meta_info.get("rollout_concurrency", 32),
        )

        return _trajectories_to_dataproto(
            finished=result.finished,
            truncated=result.truncated,
            failed=result.failed,
        )
```

Concrete responsibilities:

1. Receive a batch of prompts as a `DataProto`.
2. Wrap the actor's inference engine (vllm or sglang) as our `policy`
   callable.
3. Call `batch_rollout(...)`. Honor verl's per-batch concurrency hint.
4. Convert each `Trajectory` into the `DataProto` fields verl expects:
   responses, attention_mask, position_ids, rewards (per-step or
   final), `is_truncated`, `is_failed`. ref-logprobs are left for
   verl's reference-model worker to fill (we don't compute them).
5. Mark `truncated`/`failed` rollouts so verl's filter / dynamic-batch
   step drops them or weights them down.

## Differences from the Slime path

| Aspect | Slime adapter | verl adapter |
|---|---|---|
| Form | Plain rollout function | Ray actor |
| Engine | SGLang | vllm (today), sglang (where supported) |
| Output | `Sample` (token + scalar reward) | `DataProto` (token + masks + positions + reward) |
| Sync vs async | Adapter is `async def` | Adapter exposes a sync facade (Ray's actor model) |
| Longtail handling | Status field per Sample | `is_truncated`/`is_failed` markers in DataProto |
| Over-request / filter / cancel | Slime's `generate_rollout_async` loop | verl's `DataProto` filter pass |

Both adapters compose XRLEnv's policy-free primitives — `task_key` /
`group_id` annotations, `cancel_rollout` / `cancel_group` RPCs, group
anti-affinity, per-task fairness cap — into their respective engine's
loop shape. XRLEnv core does not assume one shape (see spec 02).

The shared core is the same `client.batch_rollout` call. Both
adapters are thin shims over it.

## Image prewarm: dataset wrapper (zero verl change)

The verl adapter installs spec 15's **Layer 2** prewarm strategy by
wrapping the incoming `DataProto` prompt iterator (or its underlying
`Dataset` object) at adapter init. verl's `RolloutWorker.generate`
consumes prompts as before; the wrapper peeks K iterations ahead
and fires fire-and-forget `client.warmup(...)` for the upcoming
`task_id`s.

Combined with **Layer 1** (SDK's implicit warmup at every
`batch_rollout`), this gives near-perfect prefetch on verl
workloads with zero verl source modification. See spec 15's
"Prewarm without modifying trainer source."

## Sessions and preemption-safe resume (phase 3)

When the cluster runs preemption-resilient sessions (spec 18,
phase 3), the verl adapter auto-derives session tokens from fields
verl already checkpoints — no verl-side change. The adapter calls
`client.start_session(..., derive_from={...})` with:

```python
derive_from = {
    "experiment_id": args.experiment_id,        # pinned at run start
    "training_step": current_step,              # already in verl's checkpoint
    "batch_index":   row.index,                 # already on the DataProto row
}
```

On verl restart-from-checkpoint, the adapter recomputes the same
`derive_from` for each in-flight rollout → same hash → same token
→ reattach succeeds. verl never sees session machinery.

This is **phase 3**.

## Trajectory recording: verl's `DataProto` (with platform-jsonl shadow recommended)

The verl adapter installs a `verl-dataproto` `TrajectorySink`
(spec 08) at adapter init. This sink:

- Buffers per-step records during a rollout.
- At seal, converts the trajectory into the `DataProto` row layout
  verl expects (responses, attention masks, position ids, rewards,
  token-level loss_mask, status markers for `truncated` / `failed`
  / `cancelled_by_group_completion`).
- Returns the `DataProto` chunk back to the caller (the verl
  trainer) through the same Ray actor return path verl already uses
  for `RolloutWorker.generate(...)`.

> **Durability warning.** `DataProto` is Ray actor state in the
> verl trainer; without an explicit verl-side checkpoint it is RAM
> only. Recording to `verl-dataproto` *alone* means a trainer or
> Ray-actor crash loses every in-flight rollout's trajectory on the
> XRLEnv side. See spec 08's durability matrix.
>
> **Recommended default**: `observability.trajectory_sink:
> "multi:[platform-jsonl, verl-dataproto]"`. verl's native path is
> unchanged; the jsonl shadow persists to
> `~/.xrlenv/runs/<date>/<rollout_id>/trajectory.jsonl` for replay,
> the viewer, and offline debugging after the trainer exits.
>
> **Adapter default**: when no template-level
> `trajectory_sink` is set, the verl adapter installs
> `multi:[platform-jsonl, verl-dataproto]` — never
> `verl-dataproto` alone. This is the platform-jsonl shadow
> guarantee.
>
> **Native-only opt-out**: a template that wants `verl-dataproto`
> alone must set both `trajectory_sink: verl-dataproto` **and**
> `observability.allow_native_only_trajectory_sink: true` (spec
> 06). Without the flag, the adapter rejects the override at
> register and falls back to the multi-sink default. Cite the
> durability matrix in the manifest comment so the trade-off is
> visible at review.

## OTel tracing: verl owns the token layer

verl emits its own spans for generation, reward-model scoring, and
the trainer's per-step bookkeeping. The verl adapter does not
duplicate XRLEnv's env-layer spans (start_rollout, backend.create,
env_adapter.step); those stay on the XRLEnv side. Trace context
propagates verl → XRLEnv SDK → control plane → node so spans nest
into one trace. See spec 08's "OTel layer split."

## Adapter contract table

verl's API churns more than Slime's, so the contract is named
explicitly per pinned commit; re-pin on each release.

| Concern | Contract |
|---|---|
| Required upstream version | tracked against `verl-project/verl @ main`; pinned commit SHA in `examples/verl/requirements.txt`; adapter checks `verl.__version__` and the pinned-commit shim refuses non-matching layouts. |
| Imported symbols | `verl.protocol.DataProto`, `verl.workers.rollout.RolloutWorker` (subclassed via `XRLEnvRolloutWorker`), `verl.utils.tracking.tracking_decorator`. The adapter avoids reaching into private modules; if a public hook isn't available, the adapter's smoke test fails loudly. |
| Input from verl | `DataProto` batch with `meta_info.rollout_concurrency`, `meta_info.experiment_id`, `meta_info.training_step`. Per-row prompt extracted via `_extract_prompts(batch)` (single function the user can override). |
| Output to verl | `DataProto` with `responses`, `attention_mask`, `position_ids`, `rewards`, `is_truncated`, `is_failed`, `cancelled_by_group_completion`, `loss_mask`, `metadata`. ref-logprobs left for verl's reference-model worker. |
| Status mapping | `finished → is_truncated=False, is_failed=False`; `truncated → is_truncated=True`; `cancelled → cancelled_by_group_completion=True`; `failed → is_failed=True, metadata.status_reason=<reason>`. |
| Cancellation mapping | verl filter pass calls adapter's `cancel_group(group_id)` → XRLEnv `cancel_group`. Per-row cancel via `cancel_rollout(rollout_id)`. |
| Trajectory sink | **Default**: `multi:[platform-jsonl, verl-dataproto]` (durability shadow on by default). The adapter installs this composite at init unless the manifest overrides; the verl-dataproto child still buffers in-memory and returns the `DataProto` chunk via the Ray actor return value, while the platform-jsonl child writes to the run dir on the node. **Native-only opt-out**: requires both `trajectory_sink: verl-dataproto` *and* `observability.allow_native_only_trajectory_sink: true` (spec 06). **Hard-pin**: `observability.trajectory_sink_pin: true` refuses adapter overrides entirely. |
| Warmup key extraction | `row.meta_info.task_id` first; `row.index` as fallback when no upstream task id is available; configurable via `XRLEnvRolloutWorker(task_id_extractor=...)`. |
| Concurrency model | one Ray actor per worker; thread-local `Client` connection multiplexed within the actor. |
| Smoke test | `examples/verl/swebench_verl_smoke.py` runs against the pinned commit; "best-effort" given upstream churn — Slime's smoke is the primary phase-1 gate. |

## Phase 1 deliverables

- `xrlenv/adapters/verl.py` implementing the Ray actor.
- `examples/verl/swebench_verl_smoke.py` — wires the adapter into a
  verl trainer config and runs SWE-bench rollouts.
- Slime's phase-1 smoke test is the primary trainer-side gate; verl's
  is tracked but ships best-effort given verl's `main`-branch API
  churn.

## Version target

[verl-project/verl @ main](https://github.com/verl-project/verl) is the
target. Track main; re-pin a SHA in `examples/verl/requirements.txt`
on each XRLEnv release so an older check-out remains buildable.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: nothing verl-specific ships. Phase-0 SDK is designed
  to make the phase-1 adapter trivial.
- **Phase 1**: SWE-bench smoke test working end-to-end (best-effort
  given verl `main` API churn). Web-search template parity if it
  lands in the same phase.
- **Phase 2**: expose XRLEnv's longtail metrics in verl's training
  logger; sglang-backed engine path; snapshot/branch plumbed through;
  multi-agent rollouts; GPU-aware capacity for verl's ref-model
  workloads (out of scope for XRLEnv core but the adapter advertises
  it).
