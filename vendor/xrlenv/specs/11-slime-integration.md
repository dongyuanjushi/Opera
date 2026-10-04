# 11 — Slime Integration

## Purpose

First-class adapter for [THUDM/slime](https://github.com/THUDM/slime),
our **primary** target trainer. Slime's `RolloutManager` has clean
staleness and longtail handling (dynamic-batch whatever finishes in
the time window, drop the rest), and our `batch_rollout` cleanly
truncates stragglers — the two designs reinforce each other.

Slime is an **optional install**. XRLEnv core does not import it;
`xrlenv/adapters/slime.py` does, and only when the user asks for it.

**Phase**: ships in **phase 1** alongside the verl adapter. The
phase-0 SDK (`client.batch_rollout(...)`) is already designed to
support Slime's longtail story; this spec describes the additive
phase-1 work to wire it up.

## What Slime expects

A *rollout function* with signature:

```python
def rollout_fn(args, rollout_id: int, data_source, evaluation: bool) -> Output:
    ...
```

Where:
- `args` carries Slime's runtime config and a handle to the SGLang
  inference engine the trainer is currently driving.
- `data_source` exposes `load(rollout_id) → list[prompt]` and `len()`.
- `evaluation` toggles eval-mode behavior (no on-policy data emitted).
- `Output.data` is `list[Sample]`.
- `Output.metrics` is a free-form dict.

`Sample` carries:
```
tokens, response_length, reward, status, loss_mask, metadata
```

`status` is one of `"finished" | "truncated" | "failed"`. Slime's
data buffer drops non-`finished` samples or weighs them down via
`loss_mask` — exactly the pattern our truncation needs.

## Adapter shape

```python
# xrlenv/adapters/slime.py

def make_rollout_fn(
    template: str,
    sdk_client: xrlenv.Client,
    default_deadline: Deadline,
    policy_factory: Callable[[Any], Policy] = ...,
) -> Callable:
    """Return a function suitable for Slime's RolloutManager."""

    async def rollout_fn(args, rollout_id, data_source, evaluation):
        prompts = data_source.load(rollout_id)
        policy  = policy_factory(args.engine)        # wraps SGLang call

        # All sandboxes destroyed by the time this returns.
        result = await sdk_client.batch_rollout(
            template=template,
            inits=[{"prompt": p} for p in prompts],
            policy=policy,
            hard_deadline=default_deadline.hard_s,
            soft_deadline=default_deadline.soft_s,
            concurrency=args.rollout_concurrency,
        )

        samples = (
            [_to_slime_sample(t, status="finished")  for t in result.finished]
            + [_to_slime_sample(t, status="truncated") for t in result.truncated]
            + [_to_slime_sample(f, status="failed")    for f in result.failed]
        )
        return Output(data=samples, metrics=_aggregate_metrics(result))

    return rollout_fn
```

Concrete responsibilities:

1. Pull a batch of prompts from `data_source` per `rollout_id`.
2. Run them through `batch_rollout` with the configured
   `hard_deadline`. The SGLang engine that Slime hands us is wrapped
   into a `policy` callable.
3. Convert every `Trajectory` into a `Sample`:
   - `tokens` — concatenation of prompt + generated tokens (uses the
     policy's tokenizer).
   - `response_length` — generated portion length.
   - `reward` — from the trajectory's `final_reward`.
   - `status` — directly from our trajectory status.
   - `loss_mask` — full mask for `finished`, partial mask up to the
     truncation point for `truncated`, all-zeros for `failed`.
   - `metadata` — template name, deadlines, sandbox/node IDs for
     debug.
4. Return `Output(data=samples, metrics={...})`.

## Why this fits Slime's longtail and over-request story

Slime drives its own over-request / filter / cancel loop in
[`generate_rollout_async`](https://github.com/THUDM/slime/blob/3cdec0cc82dd82f2b1a3e80164430c662d9354ea/slime/rollout/sglang_rollout.py):
submit `over_sampling_batch_size` groups, await `FIRST_COMPLETED`,
apply `dynamic_sampling_filter`, continue until `rollout_batch_size`
is satisfied, then `abort()` the rest. XRLEnv core deliberately does
**not** bake this policy in (engines differ); the Slime adapter
implements exactly that loop on top of XRLEnv's primitives:

- `client.batch_rollout(...)` with `task_keys` and `group_ids` set
  per group → starts the over-requested rollouts; XRLEnv's group
  anti-affinity spreads them across nodes; `max_runs_per_task`
  protects fairness.
- `asyncio.wait(..., FIRST_COMPLETED)` over the returned futures →
  drives Slime's filter loop unchanged.
- `client.cancel_group(group_id)` when Slime decides a group is
  done or has failed its filter → frees sandboxes immediately
  rather than waiting on hard deadline, so the next iteration
  doesn't starve resources.
- Aborted-but-partial trajectories surface through XRLEnv's replay
  (spec 02) so Slime's `partial_rollout` salvage path keeps working.

Two failure modes Slime authors are explicit about:

- **Stragglers blocking the batch** — Slime drops them via filter
  + abort; XRLEnv frees the sandbox within the standard 5 s
  teardown.
- **Failed engines** — Slime watches engine health and recovers via
  `recover_updatable_engines`. We mirror this on the env side: the
  node-agent `/healthz` lets Slime detect a dead env worker by
  node.

## Image prewarm: data-source wrapper (zero Slime change)

The Slime adapter installs spec 15's **Layer 2** prewarm strategy
by wrapping `args.data_source` with a `WarmingDataSource` at adapter
init. Slime's `generate_rollout_async` calls
`data_source.get_samples(n)` exactly as before; the wrapper
transparently peeks ahead K iterations and fires fire-and-forget
`client.warmup(...)` for the upcoming `task_id`s. Slime sees no
behavioral difference.

Combined with **Layer 1** — the SDK's implicit warmup at every
`batch_rollout` call — this gives effectively-perfect prefetch on
Slime workloads against an XRLEnv cluster, with zero modification
to Slime source. See spec 15's "Prewarm without modifying trainer
source" for the full mechanism.

## Sessions and preemption-safe resume (phase 3)

When the cluster runs preemption-resilient sessions (spec 18,
phase 3), the Slime adapter auto-derives session tokens from
fields Slime already checkpoints — no Slime-side change. The
adapter calls `client.start_session(..., derive_from={...})` with:

```python
derive_from = {
    "experiment_id": args.experiment_id,        # pinned at run start (e.g. wandb run id)
    "rollout_id":    args.rollout_id,           # already in Slime's checkpoint
    "sample_index":  sample.index,              # rebuilt from data_source.load(rollout_id)
    "group_id":      group_id,                  # adapter-derived from rollout shape
}
```

On Slime restart-from-checkpoint, the adapter recomputes the same
`derive_from` dict for each in-flight rollout → same hash → same
token → `client.attach(...)` reaches the still-warm or restored
session. Slime never sees session machinery.

This whole story is **phase 3** — phase 1 ships only the
trajectory adapter + image prewarm wrappers above; the session
machinery lands later without code changes to Slime.

## Trajectory recording: Slime's data buffer (with platform-jsonl shadow recommended)

The Slime adapter installs a `slime-sample` `TrajectorySink`
(spec 08) at adapter init. This sink:

- Converts each per-step record from XRLEnv's shape (action / obs /
  reward / optional tokens / loss_mask) into Slime's `Sample` shape.
- Streams converted samples directly into Slime's data buffer for
  the running iteration.
- Honors Slime's `partial_rollout` salvage: aborted-but-partial
  rollouts surface to the data buffer with `status="aborted"` and
  the partial token sequence, exactly as `generate_rollout_async`
  expects.

> **Durability warning.** Slime's data buffer is per-iteration RAM
> in the trainer process. Recording to `slime-sample` *alone* means
> a trainer crash, OOM, or restart loses every in-flight rollout's
> trajectory — they are not durable on the XRLEnv side. See spec
> 08's durability matrix.
>
> **Recommended default**: `observability.trajectory_sink:
> "multi:[platform-jsonl, slime-sample]"`. The native sink keeps
> Slime's RAM-backed iteration data path intact; the jsonl shadow
> persists to `~/.xrlenv/runs/<date>/<rollout_id>/trajectory.jsonl`
> so post-mortem replay, the trajectory viewer, and offline
> debugging still work after the trainer exits.
>
> **Adapter default**: when no template-level
> `trajectory_sink` is set, the Slime adapter installs
> `multi:[platform-jsonl, slime-sample]` — never `slime-sample`
> alone. This is the platform-jsonl shadow guarantee.
>
> **Native-only opt-out**: a template that wants `slime-sample`
> alone must set both `trajectory_sink: slime-sample` **and**
> `observability.allow_native_only_trajectory_sink: true` (spec
> 06) in its manifest. Without the flag, the adapter rejects the
> override at register and falls back to the multi-sink default.
> Cite the durability matrix in the manifest comment so the
> trade-off is visible at review.

## OTel tracing: Slime owns the token layer

The Slime adapter does *not* emit env-layer spans (start_rollout,
backend.create, env_adapter.step) — those are XRLEnv's. It does emit
the token-layer spans Slime already produces (generation, reward,
filter, weight update). Trace context propagates from Slime →
XRLEnv SDK → control plane → node, so all spans nest into one
trace per rollout. See spec 08's "OTel layer split."

## Health probes

Slime polls the **control plane** for node health, not the node
agent directly (node agents are outbound-only, spec 04). The
control plane's gRPC API exposes:

- `Healthz(node_id)` — 200 if the named node is heartbeating to
  the control plane within the threshold, 503 otherwise.
- `Readyz(node_id)` — 200 if the node's backends are responsive
  (last successful Docker stats / Cube ping inside the threshold).

Underneath, the control plane consults the node-registry state
populated by heartbeats on the reverse command stream. The
node-local `/healthz` on `127.0.0.1` (spec 04) is for systemd /
k8s readiness only and is never reached from off-node.

Slime polls these to mark env workers dead, the same way it polls
SGLang engines.

## Adapter contract table

The Slime adapter is an integration boundary; pin the parts that
have to match upstream so future Slime changes show up at
adapter-build time, not at training-run-failure time.

| Concern | Contract |
|---|---|
| Required upstream version | `slime >= 0.4.0` (the version that introduced the `RolloutManager` shape this adapter mirrors). The adapter's `__init__` checks `slime.__version__` and raises `AdapterIncompatible` if older. |
| Imported symbols (only these) | `slime.RolloutManager`, `slime.Sample`, `slime.Output`, `slime.utils.data.Dataset`, `slime.engine.SGLangEngine` (typed handle only). Anything else is a layering violation. |
| Input from Slime | `args` (`rollout_concurrency`, `engine`, `experiment_id`, `rollout_id`), `data_source` (must implement `load(rollout_id) -> list[prompt]`, `len()`, optionally `peek(n, offset)`), `evaluation: bool`. |
| Output to Slime | `Output(data: list[Sample], metrics: dict)`. `Sample` populated per the table below. |
| `Sample.tokens` source | tokenizer drawn from `args.engine.tokenizer`; concatenation of prompt + generated tokens. |
| `Sample.response_length` | length of the *generated* token segment only. |
| `Sample.reward` | `Trajectory.final_reward`. |
| `Sample.status` mapping | XRLEnv `finished → "finished"`, `truncated → "truncated"`, `cancelled → "aborted"`, `failed → "failed"`. |
| `Sample.loss_mask` | full ones for `finished`; partial up to truncation point for `truncated`; partial up to cancel point for `aborted`; all zeros for `failed`. |
| `Sample.metadata` | `{template, deadline, sandbox_id, node_id, rollout_id, status_reason}`. |
| Cancellation mapping | XRLEnv `cancel_group(group_id, reason="have enough")` ← Slime's filter / abort decision; never the other way around. |
| Trajectory sink | **Default**: `multi:[platform-jsonl, slime-sample]` (durability shadow on by default). The adapter installs this composite at init unless the manifest overrides. **Native-only opt-out**: requires both `trajectory_sink: slime-sample` *and* `observability.allow_native_only_trajectory_sink: true` (spec 06); without the flag the adapter rejects the override and falls back to the multi-sink default. **Hard-pin**: `observability.trajectory_sink_pin: true` refuses adapter overrides entirely (template wins). |
| Warmup key extraction | `_extract_task_id(sample)` — adapter-private function; default reads `sample.task_id` then `sample.metadata["instance_id"]`. Customizable via `make_rollout_fn(task_id_extractor=...)`. |
| Smoke test | `examples/slime/swebench_slime_smoke.py` runs 8 SWE-bench rollouts end-to-end and asserts at least 1 finishes; CI runs on every adapter change. |

Adapter compatibility errors are loud — the adapter refuses to
load a Slime version it hasn't been pinned against, rather than
silently mapping new fields incorrectly.

## Phase 1 deliverables (slipped from phase 0)

- `xrlenv/adapters/slime.py` implementing `make_rollout_fn` and
  helpers.
- `examples/slime/swebench_slime_smoke.py` — a runnable script that
  wires up Slime's `RolloutManager` against an XRLEnv control plane,
  runs N SWE-bench rollouts, prints the resulting `Sample` rewards.
- This script is the **phase 1 gate** for the trainer-integration
  side of the platform.

Phase 0 does *not* ship Slime integration. The phase-0 SDK
(`client.batch_rollout(...)`) is already shaped to support Slime's
longtail story (hard deadline + clean partial-trajectory return), so
the eventual adapter is small and additive — the deferral is about
sequencing, not redesign.

## Open question

Whether the adapter should drive an SGLang engine that Slime owns
(passed through `args`) or assume the policy is supplied by the
caller. Plan-of-record: **Slime owns the engine; we receive it via
`args` and call it.** Confirm during implementation.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: nothing Slime-specific ships. Phase-0 SDK is designed
  to make the phase-1 adapter trivial.
- **Phase 1**: SWE-bench smoke test working end-to-end. Extend to
  web-search template if it lands in the same phase.
- **Phase 2**: snapshot-aware rollouts (Slime configures a fork point
  for off-policy correction); multi-agent rollouts; per-template
  longtail metrics surfaced in `Output.metrics`.
