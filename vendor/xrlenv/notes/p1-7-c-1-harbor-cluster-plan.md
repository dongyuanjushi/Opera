# P1.7.C.1 — `XrlenvHarborEnvironmentCluster` (single-service) plan

**Status**: SHIPPED + GATE-GREEN 2026-05-07. Plumbing landed in
commit `d94a471`; cluster gate (8 phase-0 terminal-bench-2 tasks
through the VM topology with `--max-workers 8`) closed by
`84887a0`. Two post-gate fix commits — `7b2bb44` (mkdir before
`put_archive`) + `45925e2` (mkdir + chmod the `/logs/*` dirs in
`start()`) — close the runtime gaps the gate exposed.

**Parent**: slice `P1.7.C` in `notes/phase-1-to-do.md` line 679–687.

## Shipped vs plan

What's in the tree as of this commit:

- **`xrlenv_plugins/harbor/environment.py`** — adds
  `XrlenvHarborEnvironmentCluster(XrlenvHarborEnvironment)` plus
  pure helpers (`_sanitize_image_tag`,
  `_sanitize_container_name`, `_client_from_env`,
  `_tar_one_file`, `_tar_dir_contents`, `_untar_one_file`,
  `_untar_dir_contents`). Method overrides:
  `is_mounted=False`, `can_disable_internet=False`,
  `_resolve_image_ref`, `_ensure_dir` (`mkdir -p` as root before
  `put_archive`; closes Docker's "target dir must exist"
  contract gap that local-mode `docker compose cp` papers over),
  `start` (lazy `Client` from env + `mkdir -p && chmod 777
  /logs/{agent,verifier,artifacts}` + `acquire_container` with
  default xrlenv rollout labels populated from harbor's
  `trial_paths.trial_dir` + `session_id`, override-able via the
  `xrlenv.rollout_metadata(...)` contextvar), `stop`, `exec`,
  `upload_file`, `upload_dir`, `download_file`, `download_dir`,
  `_chown_to_host_user` (no-op — bind-mount UID alignment is
  moot in the cluster topology).

- **`xrlenv_plugins/harbor/__init__.py`** — re-exports both
  `XrlenvHarborEnvironment` and
  `XrlenvHarborEnvironmentCluster` so harbor users can pick either
  via `import_path` in `job.yaml`.

- **`xrlenv_plugins/harbor/README.md`** — adds a "Cluster mode"
  section (env-var table, image-distribution staging note,
  single-service-only callout, `is_mounted=False` rationale,
  validation pointer).

- **`tests/unit/test_harbor_cluster.py`** (NEW, 35 tests) — covers
  subclass relationship, property contract, all pure helpers,
  lazy client construction (env-driven + error path), lifecycle
  (start, force_build no-op, acquire-failure cleanup, stop
  idempotence, keep_containers warning, default xrlenv rollout
  labels, contextvar override precedence), exec (chunk
  aggregation, cwd/env/user/timeout pass-through, default
  timeout, workdir fallback, missing-session error),
  upload/download (single-entry + recursive round-trips, mkdir
  before put_archive, mkdir-fails-hard regression pin, non-dir
  error, missing-session error, no-op chown).

- **`examples/benchmarks-onboarding/terminal-bench-2/smoke.py`**
  (REWRITE) — drives `harbor.Job.run()` directly via the harbor
  `JobConfig` API (the API end-users would write `job.yaml` for).
  Picks LOCAL/CLUSTER `import_path` based on `--local`. 8-task
  default (PHASE_0_TASKS), `--all`, `--tasks`. Concurrency via
  harbor's native `JobConfig.n_concurrent_trials` (no external
  ThreadPoolExecutor wrapper — harbor uses asyncio internally
  per-trial). Pass criterion: `verifier_result.rewards` fully
  populated with positive values per trial.

- **`examples/benchmarks-onboarding/terminal-bench-2/README.md`**
  (REWRITE) — matches `swebench-verified/README.md` shape.

- **`notes/p1-7-c-1-harbor-cluster-plan.md`** — this plan doc.

What's deferred (per locked plan):

- Multi-service compose tasks → P1.7.C.2 with its own design pass.
- Real build-on-acquire (`HarborImageBuilder` registered against
  `BuildImagesCommand` + acquire→build→re-acquire fallback) →
  P1.7.C.2.
- In-tree `xrlenv_plugins/benchmarks/tb2/` deletion → P1.7.D
  (~3 days, ready to land — slice is gate-green).

Post-gate fix commits (the cluster gate exposed two latent gaps
local mode hides; both shipped as their own commits with new
regression tests):

- `7b2bb44` — `_ensure_dir(target)` runs `mkdir -p` as root
  before `put_archive` in both `upload_file` (parent dir) and
  `upload_dir`. Docker's raw `put_archive` returns 404 if the
  target dir doesn't exist; harbor's local-mode
  `docker compose cp src/. main:dst` auto-creates it, hiding the
  difference. A non-zero exit on the mkdir step now raises
  `RuntimeError` (vs. silently no-op'ing) — pinned by
  `test_upload_dir_raises_when_mkdir_fails`.
- `45925e2` — `start()` runs `mkdir -p
  /logs/{agent,verifier,artifacts} && chmod 777 ...` chained
  as one shell command, raising on non-zero exit. Local mode
  gets these dirs from docker-compose bind mounts; cluster mode
  doesn't, and the prior pure-`chmod` step silently no-op'd on
  the missing dirs → solve.sh exited 1 on the bash redirect →
  trial cascaded to a verifier 404. The `start_acquires_and_chmods`
  test was extended to assert the new composite shape +
  `user="root"`. Integrity callout in the docstring: the three
  dirs are write-targets (empty during agent run); the
  verifier's actual scripts go to `/tests`, uploaded by harbor
  AFTER the agent finishes — no new visibility for the agent.
- `84887a0` — `start()` default-populates the two
  xrlenv rollout labels (`xrlenv.rollout.artifact_path =
  trial_paths.trial_dir`, `xrlenv.rollout.displayed_name =
  session_id`) so the admin `/rollouts/raw/<id>` view shows
  recognisable trial-level metadata without the operator
  wrapping each trial in `with xrlenv.rollout_metadata(...):`.
  The contextvar still wins by `labels.update(...)` order —
  pinned by `test_start_rollout_metadata_overrides_default_labels`.

Plan deviations from what was written above:

- **One file vs separate file**: kept the implementation in
  `environment.py` per the plan as written. The file grew from 175
  to ~470 lines; still small enough that side-by-side comparison
  of local vs cluster behaviour is the right trade.
- **No `_xrlenv_route_command` cluster override**: the seam was
  designed as a subprocess-argv rewriter when we still believed
  cluster mode would intercept `docker compose` argv. The locked
  decision (per-API translation, not byte-forwarding) made the
  seam moot — overrides hit harbor's typed methods directly. Kept
  in the local class as dead-but-harmless (next slice can retire
  it cleanly).
- **`force_build`**: documented as a no-op with a log line per
  plan. `keep_containers=True` is also a no-op with a warning
  (the session model destroys on stop; a "stop but keep" branch
  needs a control-plane-side change). Both are listed as
  short-term gaps in the plug-in README.

## What ships

A new `XrlenvHarborEnvironmentCluster` class in
`xrlenv_plugins/harbor/environment.py` that subclasses the existing
`XrlenvHarborEnvironment` (today a no-op metadata recorder) and
overrides harbor's container-touching methods to route through the
xrlenv cluster primitives instead of local `docker compose` /
`docker cp`. After this slice, harbor users add
`import_path: xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster`
to their `job.yaml` exactly the way they'd pick `e2b` or `modal`,
and harbor's existing trial driver runs unchanged against the
cluster topology.

The phase-1 acceptance gate for this slice: 8 terminal-bench-2
tasks (`fix-git`, `build-pov-ray`, `overfull-hbox`,
`cobol-modernization`, `prove-plus-comm`,
`constraints-scheduling`, `nginx-request-logging`, `dna-insert`
— matching `PHASE_0_TASKS` in
`xrlenv_plugins/benchmarks/terminal_bench_2/examples/tb2_acceptance_smoke.py`)
through 2 nodes, sealed with all rewards positive and harbor's
own report.json showing the expected per-task pass/fail shape.

## Decisions locked (2026-05-07 design conversation)

- **Single-service first.** Multi-service compose tasks (a few
  harbor tasks attach a `db` / `redis` helper) defer to a follow-on
  slice with its own design pass. The 8 gate tasks are all
  single-service `main`-only.

- **No constructor flag, no auto-detect — distinct subclass.**
  `XrlenvHarborEnvironment` (existing) stays as the local-mode
  metadata recorder. `XrlenvHarborEnvironmentCluster` (new) is the
  cluster-routed shape. harbor users pick the right `import_path`
  in `job.yaml` exactly the way they pick E2B vs Modal vs Daytona —
  no surprise behavior change for anyone already using the local
  class.

- **Land cluster mode green first, P1.7.D second.** This slice
  does not delete the in-tree `xrlenv_plugins/benchmarks/tb2/`
  plug-in. P1.7.D follows once the cluster path is gate-green.

- **No proto bump needed.** `ContainerExecCommand` (proto field 33)
  and `StreamContainerExecCommand` (field 41) both already carry
  `cwd`, `env` (map), and `user`. harbor's
  `docker compose exec -w cwd -e KEY=VAL -u user main bash -c X`
  flags map 1:1.

- **Build-on-acquire is staged.** This slice assumes images are
  pre-built on each node via the existing
  `examples/benchmarks-onboarding/terminal-bench-2/scripts/build-task-images.sh`
  (same prerequisite the README already documents). If a node
  doesn't have the image, `acquire_container` fails fast with a
  clear `ImageNotFound` pointing at the build script. Real
  build-on-acquire (`HarborImageBuilder` registered against
  `BuildImagesCommand`, plus an `acquire → build → re-acquire`
  fallback) is **P1.7.C.2**, ~1 week on its own. The user-facing UX
  gap is one log line; the production "build if missing" comes one
  slice later but doesn't block this gate.

- **Lazy `Client` from env, no new harbor-side kwargs.** The
  cluster Environment constructs an `xrlenv.Client` in `start()`
  using the same env-var protocol as the docker-py drop-in
  (`XRLENV_GRPC_HOST`, `XRLENV_GRPC_PORT`, `XRLENV_CONSUMER_TOKEN`,
  `XRLENV_GRPC_SECURE`). harbor users set the same env they already
  set for `xrlenv.from_env()` and pick the right `import_path` in
  `job.yaml`.

## Method overrides (all in
`xrlenv_plugins/harbor/environment.py::XrlenvHarborEnvironmentCluster`)

| harbor method | Cluster behavior |
|---|---|
| `is_mounted` | Always returns `False` so harbor's `trial.py` takes the post-trial download branch instead of relying on bind mounts. |
| `can_disable_internet` | Returns `False` (network policy isn't yet wired through `acquire_container`). harbor refuses tasks that require `allow_internet=False` rather than running them with internet. |
| `start(force_build)` | Lazy-construct `xrlenv.Client` from env (`XRLENV_GRPC_HOST` / `_PORT` / `_CONSUMER_TOKEN` / `_GRPC_SECURE`). Run `mkdir -p /logs/{agent,verifier,artifacts} && chmod 777 ...` as root (raise on non-zero exit). Resolve image-ref (`task_env_config.docker_image` if set, else `hb__<environment_name>`). Build labels with `harbor.session_id` / `harbor.environment_name` + default `xrlenv.rollout.artifact_path = trial_paths.trial_dir` + `xrlenv.rollout.displayed_name = session_id`; `xrlenv.rollout_metadata(...)` contextvar overrides via `labels.update(...)`. Call `client.acquire_container(image=..., labels=..., name=sanitized(session_id), command=["sleep", "infinity"], task_key=environment_name)` → stash the `ClusterContainerSession`. `force_build` is a log line + no-op (build-on-acquire is P1.7.C.2). |
| `stop(delete)` | `session.destroy()` then `client.close()`; idempotent. `keep_containers=True` logs a warning + still destroys (the session model destroys on stop; "stop but keep" needs a control-plane-side change). |
| `exec(command, cwd=None, env=None, timeout_sec=None, user=None)` | Translate to `session.exec_stream(["bash", "-c", command], cwd=cwd, env=merged_env, user=str(user), timeout_s=timeout_sec or 1800.0)`. Aggregate streamed `RawExecChunk`s into harbor's `ExecResult(stdout, stderr, return_code)`. |
| `upload_file(source, target)` | `_ensure_dir(target.parent)`; tar `source` into a single-entry archive; `session.put_archive(target_dir=parent_of(target), tarball=...)`. |
| `upload_dir(source_dir, target_dir)` | `_ensure_dir(target_dir)`; tar `source_dir` (children only — matches `docker compose cp src/. main:dst`); `session.put_archive(...)`. |
| `download_file(source, target)` | `session.get_archive(source)` → un-tar single entry to local `target`. |
| `download_dir(source_dir, target_dir)` | `session.get_archive(source_dir)` → un-tar recursively to local `target_dir`, stripping the leading docker-emitted source-basename component. |
| `_chown_to_host_user(path, recursive)` | **No-op.** Bind-mount UID alignment is moot in the cluster topology — the container is on a remote node; outputs come back via `get_archive` and land with the consumer's UID at extraction time. |
| `_ensure_dir(path)` | Internal helper. `mkdir -p path` as root via batched `session.exec`; raises `RuntimeError` on non-zero exit. Closes the Docker `put_archive` 404-on-missing-target gap. |

What we **don't** override: anything that doesn't touch the
container (preflight, compose-file generation, env-var resolution,
trial-paths construction). harbor's own logic still runs; we only
swap the four touchpoints (`start`/`stop`/`exec`/`{up,down}load`).

## Smoke rewrite

Path: `examples/benchmarks-onboarding/terminal-bench-2/smoke.py`
(rewrite — current file drives the in-tree EnvAdapter which
P1.7.D will delete; new file drives harbor's runner directly,
matching `swebench-verified/smoke.py` shape).

- **Driver shape**: import + call harbor's runner module (the
  programmatic equivalent of `harbor task run`), pointing at a
  generated `job.yaml` whose `environment.import_path` is
  `xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster`.
- **Concurrency**: `--max-workers` (default 1) threads directly
  into harbor's native `JobConfig.n_concurrent_trials`. harbor
  uses asyncio internally per-trial, so this is already an
  event-loop-scoped concurrency model — no external
  `ThreadPoolExecutor` / `ProcessPoolExecutor` wrapper is
  required. Concurrency >1 wins when N nodes are available to
  fan out across (the cluster gate ran `--max-workers 8`).
- **Task selection**: default 8-task set (`PHASE_0_TASKS`),
  `--all` for full upstream task list, `--tasks task1,task2`
  for explicit subset.
- **Artifact preservation**: write per-trial outputs to
  `tmp/<run_id>/<task_id>/` matching the
  `feedback_run_artifacts_to_tmp.md` convention.
- **Pre-build hint**: smoke prints a one-line operator hint at
  startup pointing at `scripts/build-task-images.sh`. If
  `acquire_container` fails with `ImageNotFound`, surface the
  hint again in the per-task error.
- **Pass criteria**: parse harbor's own `report.json`; assert all
  8 tasks pass under the oracle policy. Same correctness bar as
  the in-tree smoke today.

## README rewrite

Path: `examples/benchmarks-onboarding/terminal-bench-2/README.md`
(rewrite to match `swebench-verified/README.md` shape).

Sections:
- "What this shows" — wiring harbor's existing trial flow through
  xrlenv cluster mode via `import_path`.
- "Pre-requisites" — pre-build images via
  `scripts/build-task-images.sh` on each cluster node.
- "Running the smoke" — `python smoke.py` (8 tasks); `--all`;
  `--max-workers`.
- "Concurrency is the consumer's choice" — same framing as
  swebench-verified's README; harbor uses asyncio internally so
  multi-process is the safe boundary, multi-threading
  is the consumer's call if their tasks are thread-safe.
- "How the integration is wired" — short walk through the
  `import_path` mechanism + the four overridden methods.

## Plug-in README update

Path: `xrlenv_plugins/harbor/README.md`. Add a "Cluster mode"
section pointing harbor users at the new `import_path`, listing
the env-vars (`XRLENV_GRPC_HOST`, `XRLENV_GRPC_PORT`,
`XRLENV_CONSUMER_TOKEN`, `XRLENV_GRPC_SECURE`), and calling out
the staged build-on-acquire status.

## Tests

- `tests/unit/test_harbor_cluster.py` (NEW, 35 tests). Lives
  under `tests/unit/` rather than next to the plug-in to avoid
  the harbor↔`xrlenv_plugins.harbor` sys.path collision —
  pytest's package-discovery walk over `xrlenv_plugins/harbor/`
  would otherwise shadow the upstream `harbor` pip package. Same
  reasoning as the pre-existing `tests/unit/test_harbor_plugin_shape.py`.
  Coverage breakdown in the "What's in the tree" block above.
- Existing harbor plug-in tests (`test_harbor_plugin_shape.py`)
  stay green — the cluster class is additive; the local class
  doesn't change behavior.

## Sequencing — what actually happened

All steps DONE 2026-05-07:

1. ✓ Plan doc + green light (this file).
2. ✓ `XrlenvHarborEnvironmentCluster` skeleton + lazy Client +
   `start`/`stop` + image-ref. (`d94a471`)
3. ✓ `exec` override + streaming aggregation. (`d94a471`)
4. ✓ `upload_*` + `download_*` + tar round-trip. (`d94a471`)
5. ✓ README updates (plug-in + onboarding). (`d94a471`)
6. ✓ Smoke rewrite. (`d94a471`)
7. ✓ Laptop `--local` dry-run on `fix-git`: 1/1 passed.
8. ✓ Cluster gate: 8 phase-0 tasks via `--max-workers 8` on the
   VM topology, all passed under the oracle policy. Two latent
   bugs surfaced + fixed inline: missing `mkdir -p` before
   `put_archive` (`7b2bb44`), missing `mkdir -p /logs/*` before
   chmod in `start()` (`45925e2`). Default xrlenv rollout
   labels added in `84887a0` after the gate exposed an
   admin-UX gap (`artifact_path: (not set)` on the detail
   page).
9. ✓ Landed in `dev/phase1-slim`; audit cycle closing in this
   doc-sweep commit.

## Out of scope for P1.7.C.1

- **Multi-service compose tasks** (P1.7.C.2 design pass — TBD).
- **Build-on-acquire** (`HarborImageBuilder` + acquire fallback)
  (P1.7.C.2, ~1 week on its own).
- **In-tree `xrlenv_plugins/benchmarks/tb2/` deletion** (P1.7.D,
  ~3 days, lands after this slice is gate-green).
- **Trainer integration** (Slime / verl) — P1.3 follows P1.7.
- Anything outside the four overridden methods (preflight,
  compose-file generation, env-var resolution, trial-paths
  construction stay unchanged on harbor's side).
