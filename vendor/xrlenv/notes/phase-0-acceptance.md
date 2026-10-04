# Phase-0 Acceptance Smoke

> **Status**: phase-0-exit gate, not a slice deliverable.
>
> The 8-rollout terminal-bench-2 cloud smoke is the release-readiness
> checklist the operator runs by hand once Slice 9b's real harness
> wiring lands. It is **not** part of any CI run because it requires
> manually-provisioned cloud VMs (the user has VM-only access on GCP +
> AWS — see CLAUDE.md "Cloud constraints").
>
> **Dry-run validation (2026-04-28): hello-shell smoke now passes
> end-to-end on Scenario 1 (laptop control plane + 1 GCP VM + 1 AWS
> EC2 VM)** — a long list of platform bugs surfaced and were fixed
> during the dry-run; see "Dry-run findings" below. terminal-bench-2
> remains the canonical phase-0-exit gate; this dry-run validates the
> *platform* mechanics the gate depends on.

## What "phase 0 ships" looks like

The phase-0 acceptance signal is **8 terminal-bench-2 rollouts across
one GCP + one AWS VM**. terminal-bench-2 is the first benchmark to
onboard because:

1. It exercises Pattern A end-to-end (one outer template,
   per-task pre-built Docker image, instance resolver) — the same
   shape SWE-bench-Lite, the harbor-framework task suite, and any
   future benchmark with per-task images uses.
2. Per-task images are small (≤2 GB typical) compared to SWE-bench-Lite
   instances (1–4 GB each, hundreds of instances), so a phase-0
   acceptance run downloads under ~20 GB across both VMs even with
   no warm cache.
3. The harness is shell-driven (terminal pane in/out + a tests/
   directory the grader runs at done), so the in-sandbox stub +
   `in_sandbox_final reward.cmd` paths get exercised without the
   adapter needing to wrap a complex VM (OSWorld) or pytest harness
   (SWE-bench).

The smoke demonstrates the whole stack end-to-end:

- The control plane boots (gRPC + /metrics + admin panel +
  trajectory cache + run-dir janitor) on the operator's laptop.
- Two `xrlenv-node serve` processes connect outbound from
  freshly-provisioned GCP and AWS VMs (one each), authenticated with
  the operator-issued node tokens (`xrlenv tokens issue node`).
- The trainer runs `client.batch_rollout(template="terminal-bench-2",
  inits=[{"task_id": ...}, ...])` with 8 distinct terminal-bench-2
  task ids; the Pattern-A resolver overlays the per-task image +
  resources from each task.toml's `[environment]` block.
- All 8 rollouts seal as `finished` with non-null `final_reward`
  (the harness's per-task pass score). The exact score depends on
  the policy under test — the smoke is about the *platform*
  completing end-to-end, not a model's score.
- The admin panel renders all 8 rollouts under `/rollouts`; the
  trajectory viewer (`/rollouts/<id>`) renders each rollout's step
  list end-to-end via the spec-21 `FetchTrajectoryCommand` from the
  remote node-side jsonl.
- `xrlenv events` shows clean lifecycle events; `xrlenv audit
  --kind auth.denied` returns 0 rows.

SWE-bench-Lite + OSWorld stay first-class **after** terminal-bench-2
has demonstrated the platform can carry a real benchmark end-to-end.
Their templates already ship in `xrlenv/templates/{swebench-base,
osworld-base}/` with scaffold adapters; real upstream wiring + their
own per-benchmark acceptance smokes follow terminal-bench-2.

## Dry-run findings (hello-shell on real cloud VMs)

`examples/scenario1_acceptance_smoke.py` was driven through the full
laptop-control-plane / SSH-reverse-tunnel / GCP+AWS-data-plane flow
with the `hello-shell` template. Final result:

```
4 / 4 rollouts sealed as finished across 2 node(s):
['aws-i-...', 'gcp-osworld-exp-1']
```

The dry-run surfaced a long list of latent platform bugs that would
have blocked the terminal-bench-2 acceptance even with Slice 9b done.
All are fixed on `dev/phase0`:

| # | Issue | Fix |
|---|------|------|
| 1 | Bootstrap aborted with the env-validation error *after* installing 17 packages, wasting bandwidth and time | `deploy/_preflight.sh` runs `require_env XRLENV_CONTROL_PLANE` *before* package install; ANSI-colored `die`/`warn`/`ok` helpers; bootstrap aborts in <2s on missing env |
| 2 | `python3.12` not on AL2023/Debian/Ubuntu 22.04 by default | `bootstrap-common.sh:ensure_python_312` tries distro repos (dnf for AL2023, apt for Ubuntu 24.04+) then falls back to `uv python install 3.12` (portable python-build-standalone) — **deadsnakes PPA dropped** because `add-apt-repository` is fragile and Ubuntu-only |
| 3 | `pip install -e $XRLENV_REPO` left a `.pth` pointing into `/home/<user>/xrlenv` (mode 700) — systemd `User=xrlenv` couldn't traverse → `ModuleNotFoundError: xrlenv.node` | Switched to non-editable install (`pip install <repo>`); added post-install import sanity check that runs from `cwd=/` to avoid CWD-on-sys.path shadowing |
| 4 | Real circular import: `xrlenv.node.trajectory_reader` → `xrlenv.control.trajectory_sink` → `xrlenv.control.__init__` → coordinator → admission → scheduler → `xrlenv.control.node_transport` → back into trajectory_reader | Deferred the `PlatformJsonlSink` import inside `JsonlTrajectoryReader.__init__` and put `FetchRangeKind` behind `TYPE_CHECKING` |
| 5 | Pydantic v2 `PydanticUserError: DistributedRuntime is not fully defined` when `multinode_smoke.py` ran in a fresh interpreter — `AdminServer` was guarded under `TYPE_CHECKING` but used as a model field type | Field type is now `SkipValidation[Any]` (validation is already skipped, type was decorative) |
| 6 | AL2023 ships `curl-minimal`; `dnf install curl` conflicts | Drop `curl` from the dnf line; `curl-minimal` provides everything we need |
| 7 | In-container stub crashed on `bind()`: container's `USER sandbox` (uid 1000) couldn't write to host bind-mount owned by host's xrlenv system user (uid ~990) | `host_run_dir.chmod(0o777)` after mkdir (per-sandbox uuid4 path under `runs_root` — no security regression) |
| 8 | Even after #7, host node-agent couldn't `connect()` to the bound socket — default umask left `stub.sock` at 0o644 | Stub `os.chmod(uds_path, 0o666)` immediately after `site.start()` |
| 9 | Stub couldn't `import xrlenv.observability.logging` — package `__init__` eagerly imported metrics → `prometheus_client` (not in the slim image) | Replaced eager re-exports with PEP 562 `__getattr__` so the stub stays lightweight |
| 10 | `multinode_smoke.py` boots its own runtime on port 50052 and ignores the operator's existing tunnels/control-plane on 50051 — no path to drive the live cluster | New `examples/scenario1_acceptance_smoke.py` replaces `xrlenv up` for the smoke run; binds on the same port the SSH tunnels expect; VMs reattach via systemd restart loop |
| 11 | `xrlenv nodes` showed empty — `NodeRegistry` was in-memory only (Slice-4 follow-up never landed) | New `nodes` table in `state.db`; registry mirrors `register/deregister` so the CLI can read live state |
| 12 | `Trajectory.metadata` dropped `node_id` (lives top-level on the rollout record) — smoke output showed `'node': None` for every rollout | `_metadata_with_node_id` helper folds the field in at both seal paths and at sink-read |
| 13 | Default `xrlenv up` log output was JSON — unfriendly for an operator watching the boot | `PrettyFormatter` (red ERROR / yellow WARN / green INFO / dim DEBUG); auto-detect TTY vs pipe; `NO_COLOR` env var honored; JSON unchanged for systemd capture |
| 14 | When the in-sandbox stub timed out at startup, the Docker backend force-removed the container before the operator could read its logs — debugging required guesswork | `_collect_failure_diagnostics` captures container `state.exit_code`, `state.error`, and last 30 lines of stdout/stderr into the raised `TimeoutError` message |
| 15 | gRPC reconnect backoff caps at 30s — a node that's been failing for a while can miss the smoke's window by milliseconds when reconnecting after the operator restarts the control plane | `--restart-grace 90` default in the smoke + an in-doc workaround (`sudo systemctl restart xrlenv-node` resets backoff to 1s) |
| 16 | Scheduler race: N concurrent `place()` calls all read `state.list_sandboxes()` empty before any sandbox lands → all decisions cluster on the highest-capacity node, defeating `max_runs_per_task` anti-affinity | Scheduler tracks an in-flight `_pending` registry under `threading.Lock`; `commit_placement` / `release_placement` lifecycle wired into coordinator + admission queue |
| 17 | Iteration cycle after a `git pull` on a VM was "re-run the full bootstrap" — slow and re-installs system packages | `deploy/refresh.sh`: stops xrlenv-node, `pip install --no-deps --force-reinstall` from the checkout, verifies import, restarts. ~5s |
| 18 | `xrlenv events` only surfaced rollout-lifecycle events; spec-19 audit (`auth.token_used`, `auth.denied`) lived in a separate table with no CLI surface | New `xrlenv audit` subcommand mirrors `xrlenv events` with `--kind` / `--role` / `--since` filters |

Each fix has a regression test in `tests/unit/`. Total suite is 603
passing as of this update; `mypy --strict` and `ruff` clean.

## What's currently scaffold (Slice 9 closes the platform side; 9b lands the real adapter)

Slice 9 ships the *platform* mechanics this smoke depends on:

- `instances:` resolver block on the manifest + coordinator overlay
  (Pattern A) — done.
- `assets:` block + `AssetFetcher` registry + per-node
  `AssetCacheManager` (Pattern B) — done.
- Template manifests live in two places:
  - `xrlenv/templates/hello-shell/` — phase-0 spine smoke template
    (used by the Scenario-1 dry-run above), co-located with
    `ShellEnvAdapter`.
  - `xrlenv_plugins/benchmarks/terminal_bench_2/` — Pattern A
    plug-in for the harbor-framework task suite. SWE-bench-Lite and
    OSWorld land as their own plug-in directories later (separate
    onboarding).

Slice 9b (post-22c61f8) ships the *real* terminal-bench-2 wiring:

- `TerminalBench2EnvAdapter` is a real shell-driver adapter:
  `setup()` returns the task's `instruction.md` (resolver-supplied
  via `init_params`) as the initial observation; `step()` runs the
  action as a subprocess shell command in its own process group
  (timeout kills the whole descendant tree); `teardown()` is a
  no-op. Reward arrives via the manifest's `in_sandbox_final`
  `reward.cmd` running `run-task-tests.sh`, which parses harbor's
  verifier output contract (`$ENV_VERIFIER_DIR/reward.txt` or
  `reward.json`).
- `TerminalBench2InstanceResolver` resolves a `task_id` against two
  sources: an optional override layer under
  `templates/terminal-bench-2/tasks/<task_id>/` (empty by default —
  lets operators pin a specific image without forking) and the
  upstream harbor-cache at `~/.cache/harbor/tasks/<task_id>/`.
  `enumerate_instances()` walks both layers.
- `scripts/build-task-images.sh` wraps each upstream task image
  with `tests/`, `instruction.md`, and `run-task-tests.sh` so the
  in-sandbox stub can drive it without modifying harbor's images.
- D12 stage 1 (timing-isolated grader injection) — closed.
  `tests/` and `run-task-tests.sh` are uploaded into the sandbox at
  reward time via the new `PutArchiveCommand` bidi primitive, never
  baked into the per-task image. The agent's `step()` loop runs
  against an image that does not contain the grader; the wipe-then-
  upload sequence (`rm -rf <target>` as root, then `put_archive`)
  ensures any agent-created residue is discarded before the real
  assets land. Phase-0 acceptance is now benchmark-valid for the
  fix-git task set (all 8 declare `[agent].user = None` and
  `[verifier].user = None`).

What **isn't** done in phase-0 (intentional):

- D12 stage 2 (UID separation between agent and verifier). Stage 1
  closes timing isolation; identity isolation requires `--user`
  support in the backend exec primitive + reading
  `[agent].user` / `[verifier].user` from `task.toml`. Tracked in
  `notes/deferred_audit_todos.md`.
- Coordinator-side step-timeout handling for `StepResult.truncated`
  was added in an earlier audit cycle (M1) — coordinator now seals
  `RolloutStatus.TRUNCATED` with `reason=step_timeout` and skips
  `in_sandbox_final` reward, raising `RolloutTruncated` to the
  consumer with the partial trajectory.

## Operator runbook (Scenario 1 — verified)

The full deployment runbook lives in
[`docs/deployment/runbook.md`](../docs/deployment/runbook.md) — Parts
1–7 of the "Scenario 1" section walk the laptop-control-plane +
GCP+AWS topology end-to-end, including SSH reverse tunnels for the
laptop-behind-NAT case. Short version:

```bash
# On the laptop (control plane host):
xrlenv tokens issue node       # → save XRLENV_NODE_TOKEN
xrlenv tokens issue consumer   # → save XRLENV_CONSUMER_TOKEN
# Open SSH reverse tunnels (one per VM, kept alive with autossh):
autossh -M 0 -N -R 50051:127.0.0.1:50051 -R 9090:127.0.0.1:9090 \
        -R 8080:127.0.0.1:8080 ec2-user@<aws-vm-ip>
# (separate window) gcloud compute ssh ... --tunnel-through-iap ...

# On each VM (after `git clone` + `cd xrlenv`):
export XRLENV_CONTROL_PLANE="127.0.0.1:50051"
export XRLENV_REPO="$PWD"
sudo -E bash deploy/bootstrap-aws.sh   # or bootstrap-gcp.sh
sudo systemctl edit xrlenv-node        # paste XRLENV_NODE_TOKEN
sudo systemctl restart xrlenv-node
# One-time, until image distribution lands (see "Next steps"):
sudo docker build -t xrlenv/hello-shell:0.1 ~/xrlenv/xrlenv/templates/hello-shell

# On the laptop — embedded mode (replaces xrlenv up):
python examples/scenario1_acceptance_smoke.py \
    --grpc-port 50051 \
    --min-nodes 2 \
    --rollouts 4 \
    --spread

# OR connect mode (leaves a long-running xrlenv up + Client.grpc):
xrlenv tokens issue consumer
export XRLENV_CONSUMER_TOKEN=$(cat ~/.xrlenv/secrets/consumer.token)
python examples/scenario1_acceptance_smoke.py \
    --connect-host 127.0.0.1 --connect-port 50051 \
    --consumer-token "$XRLENV_CONSUMER_TOKEN" \
    --min-nodes 2 --rollouts 4 --spread

# Iteration after a source change on a VM:
ssh <vm> "cd ~/xrlenv && git pull && sudo -E bash deploy/refresh.sh"
```

The terminal-bench-2 acceptance run (Slice 9b deliverable) uses the
same flow with `--rollouts 8 --template terminal-bench-2 --tasks
<task_id_1> ... <task_id_8>`. The script template lives in this doc
until the real adapter lands.

## Pass criteria

| Check | How to verify |
|---|---|
| All 8 rollouts seal `finished` | smoke prints "8 / 8 rollouts sealed as finished"; or `xrlenv rollouts --status finished` shows all 8 |
| Distribution across both VMs | smoke prints "across 2 node(s)"; with `--spread` (and post-fix scheduler) the split is deterministic ceil(N/K) per node |
| Per-task image overlay worked | `xrlenv events --rollout <id> --kind rollout.start` payload includes the per-task `docker_image` from the task.toml |
| Node-token auth gated the bidi stream | `xrlenv audit --kind auth.token_used --role node` shows ≥2 entries (one per connected node) |
| Trajectory viewer renders | open `/rollouts/<id>` in the admin panel; step list, actions, observations all visible |
| Audit log clean | `xrlenv audit --kind auth.denied` returns 0 rows |
| `/metrics` covers the rollout | `curl 127.0.0.1:9090/metrics \| grep xrlenv_rollouts_finished_total` shows 8 increments |
| `xrlenv nodes` shows live state | both VMs appear with `STATUS=connected` and recent `LAST_SEEN` |

## Current readiness

| Phase-0 spec area | Status |
|---|---|
| Sandbox backend (Docker) | ✅ |
| Rollout API (`Client.rollout`, `batch_rollout`, `replay`) | ✅ |
| Coordinator + scheduler + admission queue | ✅ (concurrent-place race fixed 2026-04-28) |
| Capacity estimator (multi-pool disk) | ✅ |
| Trajectory sinks (platform-jsonl) + bidi `FetchTrajectoryCommand` | ✅ |
| `Trajectory.metadata.node_id` surfaced | ✅ (2026-04-28) |
| Image cache + pin list + `xrlenv images / warmup` | ✅ |
| Asset cache + `AssetFetcher` + Pattern B | ✅ |
| Pattern A instance resolver | ✅ |
| Five benchmark templates (hello-shell + terminal-base + terminal-bench-2 + swebench-base + osworld-base) | ✅ |
| **Real terminal-bench-2 upstream wiring (Slice 9b)** | ✅ done; resolver + EnvAdapter + reward wrapper live, harbor-cache content-addressable layout supported |
| **D12 stage 1 (timing-isolated grader injection)** | ✅ done; verifier-asset upload primitive + root-backed wipe; agent's step() loop runs without /tests/ in the filesystem |
| **D12 stage 2 (UID separation)** | ⏳ deferred — only matters for tasks declaring `[agent].user`/`[verifier].user` |
| Real SWE-bench-Lite + OSWorld upstream wiring | ⏳ later slices, after the terminal-bench-2 cloud smoke passes |
| Observability + audit + bearer-token auth | ✅ + new `xrlenv audit` CLI |
| Admin panel (7 read-only views) + trajectory viewer | ✅ |
| GC layers 1, 2, 4 (TTL, node startup, run-dir rotation) | ✅ |
| GC layer 3 (control-plane reconcile via bidi) | ⏳ deferred |
| `NodeRegistry` persistence + live `xrlenv nodes` | ✅ (2026-04-28) |
| Logging UX (color in TTY, JSON when piped) | ✅ (2026-04-28) |
| Deploy bootstrap (color, pre-flight validate, uv-managed Python, refresh.sh) | ✅ (2026-04-28) |
| Scenario-1 hello-shell dry-run on real GCP+AWS VMs | ✅ (2026-04-28) |
| **8-rollout terminal-bench-2 cloud smoke** | ⏳ this gate; operator-driven, all platform-side prerequisites are in place |

## Next steps

Ordered by what unblocks the terminal-bench-2 acceptance run, then by
what's a real platform gap surfaced during the dry-run.

### Hard requirements for the terminal-bench-2 acceptance gate

1. **Slice 9b — real `TerminalBench2EnvAdapter`.** ✅ Done. The plug-in
   ships a real resolver (walks both flat and harbor's content-addressable
   `<hash>/<task>/` cache layouts), a real shell-driver EnvAdapter, the
   reward wrapper script, and the verifier-asset injection (D12 stage 1)
   that uploads `tests/` and the wrapper into the sandbox at reward time.
2. **Image distribution for terminal-bench-2 per-task images.** Resolved
   via build-on-VM: each VM runs `bash scripts/build-task-images.sh` which
   does a single-stage build of the upstream `<task>/environment/Dockerfile`
   and tags it `terminal-bench-2/<task>:0.1`. xrlenv's image cache finds
   the local tag and skips registry pulls. No registry push step needed.

2.5. **Pattern-A per-task resource accounting in the scheduler.**
    Surfaced by the 2026-04-28 audit (M1) on commit `b4bc7e7`.
    `_PendingPlacement` and `SandboxRecord` both key load by outer
    `template_name` only, so a heavy per-task instance (e.g.
    `sqlite-schema` asking for 2 CPU / 4GiB while the outer
    `terminal-bench-2` declares 1 CPU / 2GiB) contributes the outer
    template's resource profile to load accounting, not its
    effective per-task profile. Result: post-instance-resolver
    placements may under-count CPU/memory/disk and over-admit on
    the chosen node. Hello-shell smoke doesn't exercise this (one
    profile per template), but terminal-bench-2 absolutely does.
    Fix shape: snapshot effective resources on `_PendingPlacement`
    and `SandboxRecord`, feed them through `_load_with_pending` /
    `_gather_cluster_load` instead of reconstructing from the outer
    catalog manifest. Land alongside Slice 9b — the harness wiring
    is the trigger that exposes the symptom.

### Real platform gaps that should land before phase-0 ships

3. ~~**`Client.remote` + consumer-facing gRPC service.**~~ **Closed
   2026-04-28** — `Client.grpc(host, port, token=...)` and the
   `RolloutControl` gRPC service shipped (commit `62291cd`). Trainers
   can now drive a live `xrlenv up` from a separate process; the
   smoke driver's `--connect-host` mode exercises this path.
4. **Auto-build hello-shell during bootstrap.** The smoke template
   isn't in any registry (intentionally — it's the spine smoke
   image). Add a `docker build -t xrlenv/hello-shell:0.1 ...` step
   to `bootstrap-common.sh` so the dry-run is one fewer manual
   command per VM.
5. **`grpc_link` reconnect-cleanup task warnings.** Journal shows
   asyncio errors during reconnect cleanup
   (`RuntimeError: aclose(): asynchronous generator is already running`,
   `Task was destroyed but it is pending`). Doesn't break
   functionality (next attempt succeeds) but pollutes the journal
   and could mask real failures. Investigate the lifecycle of the
   bidi reader task vs the request iterator on disconnect.
6. **Lower the gRPC reconnect-backoff cap with jitter.** Capped at
   30s today; means a node that's been failing for a while can miss
   the smoke's grace window by milliseconds. Lower the cap to
   ~5–10s and add jitter so reconnects aren't synchronized across
   nodes.

### Quality-of-life nits (low priority)

7. **Auto-derive CLOUD column in `xrlenv nodes` from node_id prefix.**
   Bootstraps already prefix with `aws-` / `gcp-`; the CLI throws
   that info away. Five-line change.
8. **Bootstrap pre-builds a smoke image, period.** Same as #4 but
   could ship without waiting for image-distribution decisions.
9. **Drop `EXPECTED ADDR` from default `xrlenv nodes` output.** It's
   vestigial — outbound-only nodes don't have a useful "expected
   address" from the control plane's perspective. Move behind
   `--verbose` or to `xrlenv nodes --rostered` only.
10. **Validate the dry-run again with `--rollouts 8 --spread`.** The
    real acceptance gate is 8 rollouts; the 4-rollout dry-run
    exercised the same code paths but won't catch any 8-only edge
    cases (admission queue depth, scheduler choices when both nodes
    fill).

### After phase 0 ships (defer)

11. **Slice 9c — real terminal-bench / SWE-bench / OSWorld wiring.**
    Same Slice-9b shape, repeated for each upstream harness. Each
    gets its own per-benchmark acceptance smoke.
12. **GC layer 3 (control-plane reconcile via bidi).** Deferred from
    Slice 5b. Catches sandboxes the node thinks exist that the
    control plane has forgotten (and vice-versa) on reconnect.
13. **Capacity online refinement (EMA-of-p95 effective request)**.
    Static estimator was adequate for phase-0 dry-run; refinement
    matters more for phase-1 mixed-template workloads.
