# Current Status

This page summarises what XRLEnv ships today and how the implementation
progressed. For the planned future work, see {doc}`roadmaps`.

## What is available today

The following capabilities are implemented, tested, and ready to use:

**Core SDK (Consumer-facing):**
- Single rollout: `Client.rollout` / `RolloutSession`
- Batch rollout: `Client.batch_rollout`
- Cancellation: `Client.cancel_rollout`, `Client.cancel_group`
- Replay: `Client.replay` (local and remote)
- Reward modes: `env_step`, `in_sandbox_final`, `consumer_final`
- Per-rollout `Deadline` (hard, soft, idle-TTL, per-step timeout)
- Idempotency keys (`request_id`) and group primitives (`group_id`, `task_key`)
- Session keepalive: `session.heartbeat()`

**Control plane:**
- `LocalRuntime` (in-process) and `DistributedRuntime` (gRPC, multi-node)
- Capacity-aware scheduler with fits-and-largest-remaining placement
- Static capacity estimator: CPU / memory / disk budgets, per-task fairness cap
- Admission queue with backpressure and `queue_timeout_s`
- Deadline watcher (cooperative asyncio per-rollout)
- Node registry with heartbeat watchdog and disconnect-seal
- SQLite WAL state store (`~/.xrlenv/state.db`)
- Per-rollout trajectory sink: `meta.json` + `trajectory.jsonl` + `coordinator.log`
- GC layers 1, 2, and 4 (run-dir cleanup, state-db pruning, retention-days sweep)

**Observability:**
- Prometheus `/metrics` on port 9090
- Structured JSON logs to stdout (all `xrlenv up` output)
- Per-rollout `coordinator.log` under the run directory

**Operator CLI** (`xrlenv` entrypoint):
`up`, `nodes`, `rollouts`, `replay`, `events`, `tail`, `attach`, `images`,
`warmup`, `tokens issue`

**Admin panel** (read-only, `http://127.0.0.1:8080` by default):
Overview, `/nodes`, `/sandboxes`, `/rollouts`, `/rollouts/<id>` (trajectory
viewer), `/capacity`, `/health`

**Security:**
- Shared bearer tokens with three scopes (`node.report`, `consumer.rollout`,
  `operator.admin`)
- Fingerprint binding for node identity
- Mount allowlist with default-deny system paths
- Cloud metadata IP block with boot self-test
- Image digest pinning at template registration
- Asset SHA-256 integrity verification
- Default seccomp profile + `--cap-drop=ALL`
- Audit events table (90-day retention)

**Backends:** Docker (phase 0, all platforms)

**Templates and onboarding paths:**

Under the slim pivot (P1.7), case-2/3 evaluation harnesses plug
in via the docker-py drop-in or a per-framework adapter — not via
in-tree EnvAdapter plug-ins.

- **`hello-shell`** (ready) — in-tree case-1 platform spine
  smoke at `xrlenv/templates/hello_shell/`; `ShellEnvAdapter`.
- **SWE-bench Verified** (ready, case-2) — docker-py drop-in at
  `xrlenv.compat.docker_client`; one-line swap of
  `docker.from_env()` → `xrlenv.from_env()`. Worked example:
  `examples/benchmarks-onboarding/swebench-verified/`.
- **terminal-bench-2 / harbor-format** (ready, case-3) —
  per-framework adapter at
  `xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster`. Worked
  example: `examples/benchmarks-onboarding/terminal-bench-2/`.

OSWorld and other future benchmarks land via whichever shape
their upstream harness fits (drop-in if docker-py, per-framework
adapter if their own Protocol). Case-1 RL training plug-ins
(future Slime / verl integrations under P1.3 / P1.4) continue to
use the EnvAdapter Protocol.

## Implementation history

The phase-0 implementation shipped in numbered slices. This table is for
contributors who want to understand where code came from; the user-facing
features are documented in the main guides.

| Slice | What landed |
|-------|-------------|
| 1 | Phase-0 spine: single rollout end-to-end on Docker. `LocalRuntime`, `ShellEnvAdapter`, `hello-shell` template, HTTP/UDS stub, `Client.in_process`. |
| 2 | Durability + capacity + lifecycle: `SqliteStateStore`, `PlatformJsonlSink`, run-dir layout, `StaticCapacityEstimator`, capacity-aware scheduler, `AdmissionQueue`, `DeadlineWatcher`. |
| Pydantic refactor | All dataclasses → pydantic v2 `BaseModel`. `extra="forbid"` on every model. |
| 3 + 3.5 | Spec-21 bidi gRPC: `DistributedRuntime`, `NodeRegistry`, multi-node bootstrap, disconnect-seal. |
| 4 | Consumer SDK completion: `batch_rollout`, `cancel_rollout`, `cancel_group`, idle-TTL watcher, `session.heartbeat()`. |
| 4.5 | Reward modes: `in_sandbox_final` multi-grader + `consumer_final`. |
| 5a | Observability: `/metrics` Prometheus, JSON structured logs, per-rollout `coordinator.log`. |
| 5b | Operator CLI: all `xrlenv` subcommands + GC layers 1, 2, 4. |
| 6 | Image cache manager: per-node LRU, operator pin list, `xrlenv images` / `xrlenv warmup`. |
| 7a + 7b | Admin panel: FastAPI 7-view dashboard + `FetchTrajectoryCommand` over the node bidi stream + trajectory cache. |
| 8 | Security: bearer-token auth, three scopes, audit table, image digest pin, mount allowlist. |
| 9 | Pattern A instance resolver + Pattern B asset block + four scaffold templates (later refactored out of the platform package into `xrlenv_plugins/`). |
| 9 (plug-in refactor) | Benchmark scaffolds externalised to `xrlenv_plugins/`. Platform now ships only `hello-shell`; `terminal-bench-2` is the reference plug-in. |
| 9 (manifest / run-config split) | `template.yaml` → `manifest.yaml` (contract only). Per-experiment policy moves to a user-supplied run-config (`Client(run_config=...)`); each plug-in ships `examples/default.run-config.yaml` as the on-ramp. Plug-in layout flattened — discovery glob is `xrlenv_plugins/*/*/manifest.yaml`. |
| 9b | terminal-bench-2 real plug-in: harbor-cache resolver, shell-driver EnvAdapter, reward wrapper, per-task image build script, verifier-asset upload, and plug-in tests. |
