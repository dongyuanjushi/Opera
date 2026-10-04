# Phase-0 Acceptance Run — Baseline Result

**Status: PASS** ✦ 8 / 8 rollouts sealed `finished` across 2 GCP nodes, 2026-04-30.

This is the canonical phase-0-exit signal per
`notes/phase-0-acceptance.md`. The platform completed an end-to-end
multi-VM smoke against the upstream terminal-bench-2 catalog — every
phase-0 task ran on a real cloud-provisioned VM, the per-task images
were locally built per node, the in-sandbox stub drove the agent
loop, harbor's verifier shape produced parseable rewards, and the
control plane tracked the full lifecycle including authenticated
node attachment.

## Setup

| Component | Version / value |
|---|---|
| Repo HEAD at run | `4789898` (`dev/phase0`) |
| Acceptance commit | `4789898` — preceded by `2634399` (timeouts), `5a38e78` (digest fix), `c8ec4fa` (probe distribution check), `2e038c5` (task-list alignment), `c795e1c` (heartbeat mirror) |
| Plug-in | `xrlenv_plugins/benchmarks/terminal_bench_2/` |
| Run-config | `xrlenv_plugins/benchmarks/terminal_bench_2/examples/default.run-config.yaml` |
| Smoke driver | `xrlenv_plugins/benchmarks/terminal_bench_2/examples/tb2_acceptance_smoke.py --connect-host 127.0.0.1 --connect-port 50051 --min-nodes 2` |

### Topology

| Role | Host | Notes |
|---|---|---|
| Control plane | Laptop (Mac) | `xrlenv up --grpc-host 127.0.0.1 --grpc-port 50051`; SSH reverse tunnels carry the gRPC port to each VM |
| Node A | `gcp-osworld-agent-junnan-li-3` (GCP) | Full 8-task image set built locally via `build-task-images.sh --smoke` |
| Node B | `gcp-osworld-exp-1` (GCP) | Same |

### Task set (the 8 phase-0 tasks)

`fix-git`, `build-pov-ray`, `overfull-hbox`, `cobol-modernization`,
`prove-plus-comm`, `constraints-scheduling`, `nginx-request-logging`,
`dna-insert`. Source-of-truth lists in
`xrlenv_plugins/.../scripts/build-task-images.sh::SMOKE_TASKS` and
`xrlenv_plugins/benchmarks/terminal_bench_2/examples/tb2_acceptance_smoke.py::PHASE_0_TASKS`; the lists must
stay aligned (cross-reference comments in both files).

## Per-rollout results

| Task | Node | Final reward | Duration | Steps |
|---|---|---|---|---|
| fix-git | `gcp-osworld-agent-junnan-li-3` | 1.0 | 5.9 s | 2 |
| build-pov-ray | `gcp-osworld-exp-1` | 1.0 | 120.2 s | 2 |
| overfull-hbox | `gcp-osworld-agent-junnan-li-3` | 1.0 | 86.9 s | 2 |
| cobol-modernization | `gcp-osworld-exp-1` | 1.0 | 8.3 s | 2 |
| prove-plus-comm | `gcp-osworld-agent-junnan-li-3` | 1.0 | 46.0 s | 2 |
| constraints-scheduling | `gcp-osworld-exp-1` | 1.0 | 26.4 s | 2 |
| nginx-request-logging | `gcp-osworld-agent-junnan-li-3` | 1.0 | 11.5 s | 2 |
| dna-insert | `gcp-osworld-exp-1` | 1.0 | 137.6 s | 2 |

Distribution: 4 / 4 across the two nodes (the scheduler's
capacity-aware placement spread concurrent rollouts naturally — no
image-affinity scheduling needed yet because every node had every
image, see D18 in `deferred_audit_todos.md` for the future
optimization).

Driver final line:

```
8 / 8 rollouts sealed as finished across 2 node(s):
  ['gcp-osworld-agent-junnan-li-3', 'gcp-osworld-exp-1']
```

## Pass-criteria check (per `notes/phase-0-acceptance.md`)

| Check | Status | Evidence |
|---|---|---|
| All 8 rollouts seal `finished` | ✓ | Driver "8 / 8 rollouts sealed as finished" |
| Distribution across both VMs | ✓ | 4 / 4 split, both node IDs in driver output |
| Per-task image overlay worked | ✓ | Each rollout's `init.task_id` matches its task_key; per-task image returned by the resolver |
| Node-token auth gated the bidi stream | ✓ | `audit.token_used` count = 142 (≥ 2 nodes ×N stream events); `auth.denied` count = 0 |
| Trajectory viewer renders | ✓ | Admin `/rollouts/<id>` renders step list end-to-end via spec-21 `FetchTrajectoryCommand` (verified during the smoke recovery) |
| Audit log clean | ✓ | `auth.denied` = 0 over the entire run |
| `/metrics` covers the rollout | not re-checked | Coverage validated in earlier slice work; not re-run on this baseline |
| `xrlenv nodes` shows live state | ✓ | Both VMs `STATUS=connected` with seconds-fresh `LAST_SEEN` after the heartbeat-mirror fix in `c795e1c` |

## Caveat — what this signal does and doesn't say

**Platform integrity ✓.** The smoke proves XRLEnv can carry a real
benchmark end-to-end across multi-host scheduling, authenticated
node attachment, image distribution per-node, in-sandbox-stub
drive, harbor's verifier-output contract, and trajectory sealing.

**Model-eval signal ✗.** This run uses harbor's upstream
`solve.sh` as the agent's policy (the "oracle" smoke). Every reward
of 1.0 reflects what the *upstream solution* accomplishes when run
through XRLEnv's plumbing — a sanity floor and a platform-validity
check, not a model-eval result. To get an evaluation-valid score,
swap `_make_oracle_policy` in `tb2_acceptance_smoke.py` for any
async callable that returns shell-command actions (or
`{"__exit__": True}` to seal). The platform's grader-isolation
guarantees (D12 stage 1 — timing-isolated verifier injection) hold
regardless of which policy drives the agent.

## Bugs surfaced + fixed during the acceptance recovery

The smoke didn't pass first try; closing the gate exposed a chain of
issues, each fixed structurally rather than worked-around. Captured
here so future runs of this acceptance gate carry institutional
memory:

| Symptom | Root cause | Fix commit |
|---|---|---|
| `crack-7z-hash` aborts at `step_count=0` after 60 s | `NodeAgentConfig.stub_request_timeout_s=60` overrode the 3600 s `_DEFAULT_REQUEST_TIMEOUT_S` in stub_client | `e0f2989` |
| Aborted rollouts had no diagnostic info on the admin page | `RolloutSession.__aexit__` discarded the exception type/message; admin page didn't surface categorical reason | `7e6a561` |
| `coordinator.log` invisible from the admin | No route + no template surface | `9c92d38` |
| `xrlenv nodes` showed stale "X min ago" while `STATUS=connected` | `update_node_seen` existed in the API but nothing called it; admin `/nodes` didn't query `list_nodes` | `c795e1c` |
| Single source of truth for task list drifted | `SMOKE_TASKS` in build script ≠ `PHASE_0_TASKS` in smoke driver | `2e038c5` |
| Operators had to manually run 4-step bring-up + paste token | No one-shot script | `3d3bdf5` (`deploy/bring-up-node.sh`) |
| Build-script not idempotent — re-runs cost ~5 min for no work | No skip-if-tagged check | `62b8078` |
| Operator user couldn't run docker without sudo | Bootstrap added only the `xrlenv` system user to docker group | `3f4b5a4` |
| Build-script aborted with `xrlenv: command not found` | Operator's interactive shell didn't have `/opt/xrlenv/.venv/bin` on PATH | `fbb7933` |
| Build-script + verify-setup hardcoded the 8-task smoke list | Should default to "all in cache" with `--smoke` opt-in | `310b00a` |
| Multi-VM rollouts failed with `pull access denied` for locally-built images | Catalog opportunistically pinned tags to laptop's buildx local-only `RepoDigests` | `5a38e78` |
| Connect-mode smoke fired before nodes reattached | No `--min-nodes` gating in connect mode | `c8ec4fa` |
| Cancelling rollouts pinned for 15+ minutes with no recovery | `_terminate` had no per-RPC timeouts; no startup-sweep for transient rows | `2634399` |
| Cancelled rollouts rendered as "X min live" in dashboard | `_rollout_duration_snapshot` trusted `coordinator.log` over state-store for liveness | `6f295bf` |
| Catalog spammed WARN per Pattern-A rollout for the legitimate-None case | `_maybe_pin_image` conflated "no registry digest available" with "resolver bug" | `4789898` |

## Outstanding follow-ups (not blocking)

Tracked in `notes/deferred_audit_todos.md`:

- **D18** — image-affinity scheduling (the scheduler should know
  which nodes have which images; today's even-split worked because
  all 8 task images are on both nodes).
- **D19** — pre-flight image check before placement (so a
  misconfigured node fails fast with `image_missing` instead of
  through the docker pull error path).
- **D20** — image distribution + digest-pinning architecture
  (umbrella for D18/D19 and the broader per-node-local-vs-registry
  story).
- **D21** — SDK `Client.list_nodes()` / cluster-status RPC
  (replaces the connect-mode probe-and-retry workaround in
  `_wait_for_nodes_via_probe`).
- **D12 stage 2** — UID separation between agent and verifier
  (only matters for tasks declaring `[agent].user`/`[verifier].user`
  in `task.toml`; the 8 phase-0 tasks all declare `None`).

None of these blocks phase-0 ship; they're enhancements that come
in as part of phase-1 work touching the relevant slices.

## Reproducing the result

```bash
# On each VM (idempotent if already done):
git clone <fork>/XRLEnv.git ~/xrlenv && cd ~/xrlenv
export XRLENV_CONTROL_PLANE=<control-plane-host>:50051
export XRLENV_NODE_TOKEN=$(ssh control-plane "xrlenv tokens issue node")
sudo -E bash deploy/bring-up-node.sh
# Then re-login (or `exec newgrp docker`) so the docker group takes effect:
exec newgrp docker
bash xrlenv_plugins/benchmarks/terminal_bench_2/scripts/populate-harbor-cache.sh
bash xrlenv_plugins/benchmarks/terminal_bench_2/scripts/build-task-images.sh --smoke

# On the control plane laptop:
xrlenv up --grpc-host 127.0.0.1 --grpc-port 50051 &
xrlenv nodes  # verify both VMs STATUS=connected with seconds-fresh LAST_SEEN
export XRLENV_CONSUMER_TOKEN=$(cat ~/.xrlenv/secrets/consumer.token)
.venv/bin/python xrlenv_plugins/benchmarks/terminal_bench_2/examples/tb2_acceptance_smoke.py \
    --connect-host 127.0.0.1 --connect-port 50051 --min-nodes 2
```

Expected output's last line:

```
N / 8 rollouts sealed as finished across 2 node(s):
  ['<gcp-vm-A>', '<gcp-vm-B>']
```

Where `N == 8` for the platform-integrity signal. A model-eval-valid
result requires swapping the policy in
`_make_oracle_policy(...)`.
