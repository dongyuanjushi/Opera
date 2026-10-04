# harbor 0.8.0 → 0.20.0 migration

Branch: `migration/harbor-0.20.0`. Goal: adopt harbor's **native, per-phase,
fail-closed network policy** (which replaces xrlenv's bespoke "acquire-open →
post-install `apply_egress`" offline hack — see the xevolve triage
`notes/triage/xrlenv-harbor-offline-egress.md`) **and** align on the harbor
version `abundant-ai/swe-marathon` uses (`harbor[modal]==0.20.0`), unblocking that
onboarding's version wall.

We do **not** take the `[modal]` extra — xrlenv *is* the container manager.

## Scope

harbor is a pip dependency imported only by these in-repo plug-ins:

| Plug-in | Driver package | In this migration? |
|---|---|---|
| `xrlenv_plugins/harbor/` (shared env) | `harbor` | **YES** |
| `benchmarks/lhtb` | `harbor` | **YES** |
| `benchmarks/seta` | `harbor` | **YES** |
| `benchmarks/terminal_bench_2_1` | `harbor` | **YES** |
| `benchmarks/terminalworld` | `harbor` | **YES** |
| `benchmarks/deep_swe` (`xrlenv_plugins/pier/`) | **`datacurve-pier==0.3.0`** (separate `pier` namespace) | **NO — decoupled** |
| `benchmarks/swebench_verified` | `swebench` | no (not harbor) |
| `benchmarks/evoclaw` | subprocess interceptor | no |
| `benchmarks/webarena_infinity` | raw container | no |

**deep-swe/pier is a different package** (`datacurve-pier`, installs under `pier`,
does not shadow `harbor`; the two coexist). It has its own offline-egress mechanism
(the Squid `filtered_egress` proxy — fail-closed by construction) and is **unaffected**
by the harbor bump. If we want deep-swe on harbor-0.20 semantics that's a *separate*
`datacurve-pier` bump, not this ticket.

**External coupling flag:** the old `terminal-bench-2` pin comment said `harbor==0.8.0`
was held "to be compatible with the coding-bench." coding-bench lives outside this repo
and also depends on harbor 0.8.0; bumping here forces coding-bench to migrate in lockstep
or decouple its pin. Coordinate before this branch merges.

## Audit result — breakage surface (empirical, harbor 0.20.0 in an isolated venv)

Remarkably small, because the plug-in subclasses harbor's `DockerEnvironment` and uses
its *stable* accessors. **Compatible, no change:** every import path (11/11), `ExecResult`
fields, `EnvironmentPaths.{agent,verifier,artifacts}_dir`, `_effective_cpus/_memory_mb`,
`_environment_docker_compose_path`, `with_default_user`, `run_healthcheck`, the
`EnvironmentConfig` fields we read (`docker_image/network_mode/allow_internet/env/workdir/
cpus/memory_mb`), `NetworkMode.NO_NETWORK`, and the sweep-runner deps
(`_download_dataset`, `OracleAgent.run(instruction, environment, context)`, `JobConfig`,
`RetryConfig(max_retries=…)`).

**Two actual breakages (both in `xrlenv_plugins/harbor/environment.py`):**

1. **`__init__` positional forwarding.** harbor 0.20 `DockerEnvironment.__init__` inserted
   `network_policy` / `phase_network_policies` into what was the `mounts_json` positional
   slot, and renamed `mounts_json`→`mounts` (now on `BaseEnvironment`). Our two `__init__`s
   forwarded `mounts_json` **positionally** → it would bind to `network_policy`. Fix: drop
   the `mounts_json` param and forward `*args, **kwargs` (harbor's factory constructs the
   env **all-by-keyword**, so a passthrough is correct and version-robust; `mounts=` then
   flows through `**kwargs`). Applied.
2. **Dead TYPE_CHECKING import.** `from harbor.config.models.environment.config import
   ServiceVolumeConfig` — `harbor.config` no longer exists (moved to
   `harbor.models.trial.config`). Only used to type the removed `mounts_json`; dropped.

## Feature layer — native per-phase network policy (DONE, code + tests)

harbor 0.20 drives egress natively: the Trial calls `environment.set_network_policy(policy)`
at each phase boundary (`trial/trial.py`), gated by `capabilities.dynamic_network_policy`
and **validated fail-closed at trial creation**. The `[agent]` policy applies during
`agent.run()` but not `agent.setup()` = open-install → restricted-rollout, native.

Implemented on `XrlenvHarborEnvironmentCluster` (`xrlenv_plugins/harbor/environment.py`):

- `_apply_network_policy(policy)` maps a harbor `NetworkPolicy` onto the existing spec-07
  `apply_egress` primitive — **no infra/proto changes**, because `compile_egress_rules`
  DROPs cloud-metadata before any ACCEPT and ends in a catch-all REJECT:
  - `PUBLIC`     → `apply_egress(["0.0.0.0/0"])` — allow all (metadata still blocked). This
    is how a post-agent-phase **restore-to-baseline re-opens** with a CIDR-only primitive.
  - `NO_NETWORK` → `apply_egress([])` — block all external.
  - `ALLOWLIST`  → `apply_egress(allowed_hosts)` — v4 IP/CIDR only (e.g. the LLM proxy).
- `_can_enforce_egress()` gates the capabilities: single-container + `runc` + unprivileged
  (no `NET_ADMIN`) only. A compose / sysbox / privileged task → capability `False` → harbor
  **rejects** an offline such task at validation (fail-closed) instead of under-enforcing.
- `capabilities` advertises `dynamic_network_policy` / `network_allowlist` /
  `network_allowlist_ipv4_{addresses,cidrs}` (gated); hostname / wildcard / IPv6 stay
  `False` → harbor rejects those (no DNS-name allowlist primitive yet — honest).
- `start()` applies a **non-public startup baseline** after acquire (no-op for the common
  `[environment] public` baseline; needed so a task whose agent-phase policy equals a
  non-public baseline is still sealed — harbor no-ops `set_network_policy` when phase ==
  baseline).

Tests: `tests/unit/plugins/harbor/test_harbor_network_policy.py` (13) — mapping, capability
gating, fail-closed hostname rejection, and the full harbor `set_network_policy` round-trip
(PUBLIC→NO_NETWORK→PUBLIC). Two `__new__` stubs in `test_harbor_cluster.py` gained the
harbor-0.20 `__init__` attrs `_exec_env_overlays` (ContextVar) + `_is_windows_container`.
**346/346** harbor + benchmark unit tests pass on harbor 0.20; ruff clean.

### No task-config migration needed — harbor honors 0.8-era `allow_internet` (corrected)

**Earlier draft claimed a mandatory task-decl migration; that was wrong.** harbor 0.20
carries a **`TaskConfig` deprecation shim** (`models/task/config.py`
`handle_deprecated_environment_allow_internet` → `_apply_legacy_allow_internet`): at task
load it maps `[environment] allow_internet = false` → `network_mode = NO_NETWORK` (and
`= true` → `PUBLIC`), then clears the legacy field. Verified empirically. (The earlier
red herring: a *bare* `EnvironmentConfig(allow_internet=False).resolve_baseline()` returns
`PUBLIC` because the shim lives on the parent `TaskConfig`, not the leaf config.)

So **existing 0.8-era offline tasks are honored as-is** — no `task.toml` edits, no
`build_cache` patch. harbor constructs the cluster env with a `NO_NETWORK` baseline, and our
`start()` → `_apply_baseline_network_policy` enforces it (single-container runc). Guarded by
`test_harbor_honors_legacy_allow_internet_false_as_no_network` so a future harbor that drops
the shim fails our suite loudly.

### `SealingOracleAgent` DELETED — native seal cluster-confirmed

`benchmarks/lhtb/sealing_oracle.py` existed because the bare oracle sweep had no consumer
Trial to drive `apply_egress`. Under 0.20 harbor's own Trial resolves the shimmed
`NO_NETWORK` baseline and our baseline hook seals the container before the oracle's
`solve.sh` — making `SealingOracleAgent` redundant.

**Confirmed on the dev cluster (2026-08-03)**: `commit0-multilib-tdd` run with the native
path only (a temporary `--no-seal-agent` bypass, since removed) → the task's own verifier
reported `network_detail: "no egress (sandbox sealed)"`, reward **1.0**. So the seal comes
entirely from harbor's shim + `_apply_baseline_network_policy`.

`sealing_oracle.py`, its test, the `_register_sealing_oracle()` swap in
`run_oracle_sweep.py`, and the transient `--no-seal-agent` flag are all **removed**. The
sweep now uses harbor's default `OracleAgent` as-is.

## Re-gate (the real remaining cost — needs the dev cluster + images) — ✅ DONE

Pure-compat + feature are cheap; **behavioral confidence is the cost.** Before flipping the
pin on `main`, per harbor benchmark (lhtb, seta, tb2.1, terminalworld; deep-swe unaffected):
1. Move the fleet/dev `.venv` to 0.20 (`uv sync` — lock already updated on this branch). ✅ done
2. Native offline seal spot-checked on the cluster (`commit0-multilib-tdd`, reward 1.0,
   `no egress (sandbox sealed)`). ✅ done
3. `SealingOracleAgent` + swap + `--no-seal-agent` removed. ✅ done
4. Run each benchmark's full oracle sweep on 0.20 and match the 0.8 gate. ✅ done

### Re-gate results (full sweeps on 0.20 — `tmp/harbor-migration-dev-2026-08-03…/benchmarks-harbor-migration-dev-summary.json`, + seta triage 2026-08-04/05)

| benchmark | on 0.20 | verdict |
|---|---|---|
| **terminal_bench_2_1** | **88/88** | fully green — no regression |
| **terminalworld** | **191/191** | fully green — no regression |
| **lhtb** | 38/43 | 5 non-passing are **documented content/build gaps** in `lhtb/STATUS.md` (`climate`/`materials`/`robotics-slam` = `bake_patch_binary` rebuild; `chess-mate` = `game` sidecar build+push; `unknown-config-semantics` = private-reference content). NOT a 0.20 regression. |
| **seta** | 1272/1371 → triaged | the 99 were **proven NOT a 0.20 regression** (seta STATUS.md); now 35 recovered (13 pure-cache + 10 base-image + 8 dropped-command + 4 verifier-as-root) + 64 blacklisted with verified reasons. |
| **deep-swe** | n/a | decoupled (`datacurve-pier`) — unaffected. |

**Conclusion: the migration introduced ZERO regressions.** tb2.1 + TW are 100% green on 0.20;
every residual non-pass in lhtb/seta is a benchmark content/build issue that pre-dates the
bump and is tracked in that benchmark's `STATUS.md`. The 0.20 code path (compat + native
per-phase network policy + fail-closed offline seal) is behaviorally confirmed.

## Open forks for the operator
- deep-swe **excluded** (decoupled `pier`/`datacurve-pier` package) — confirmed.
- coding-bench lockstep coupling (external) — must migrate or decouple its own `harbor==0.8.0`
  pin; it lives outside this repo, so it does not gate this branch's merge.
- lhtb gate hygiene (non-blocking): lhtb has no exclusion list, so its sweep returns `rc:1`
  on the 5 documented gaps. If CI enforces per-benchmark `rc:0`, either add an lhtb blacklist
  for the 5 or accept 38/43 as the lhtb baseline. tb2.1 / TW / seta gates are green as-is.
