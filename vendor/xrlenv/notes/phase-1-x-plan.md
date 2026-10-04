# Phase 1.x — Hardening slice plan

> **Status:** PLANNED 2026-05-10. Five-item checklist per the
> revised phase-1 exit criteria in `notes/phase-1-to-do.md`.
> Trainer integration deferred to phase 2; this slice brings
> xrlenv to a shape suitable for sustained agentic-benchmark
> evaluation outside loopback-only dev.

## Decisions locked

| # | Decision | Rationale |
|---|---|---|
| **B5.4 userns-remap** | **Per-acquire opt-in** on the raw-container path (default `"host"`). **Shipped 2026-05-10.** | A lot of benchmark tasks (swebench's harness, agent dev shells, build steps) need the in-container "agent" to run as root + write to host-bind-mounted paths. Default-on would silently break them. SDK kwarg `Client.acquire_container(userns_mode="remap")` lets operators opt in per-acquire when the image doesn't need in-container root. Daemon-level `userns-remap` in `/etc/docker/daemon.json` is a prerequisite operators configure separately; without it `"remap"` is a silent no-op. Note: shipped only on the raw-container path (case 2/3); case-1 stub-bound sandboxes still use host UIDs — extending to case-1 is a future polish if surfaced. |
| **B7.3 admin auth** | **Reuse existing token store** (basic auth where username=role, password=token) + **two-tier roles** (viewer for reads, operator for writes) | Symmetric with existing operator-token check on apply/cancel; no new artifacts. The two tiers let teammates watch builds without share privileges that could flip cluster state. |
| **B7.3 token prefix** | New tokens get a `read_` / `write_` prefix so the share signals the privilege | Operators copy/paste tokens; the prefix makes "this is a viewer-only token" or "this is a writer token" visible at a glance, no separate doc lookup. |
| **B7.1 OTel** | **Opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT` env var**; `OTEL_TRACES_EXPORTER=console` supported for developer local debugging | Zero dependency added unless operator wants tracing. Standard OTel env-var conventions; no xrlenv-specific knobs to learn. |
| **B8.1 bootstrap** | New `xrlenv bootstrap` Python subcommand; **shell scripts stay as thin wrappers** | Operators have muscle memory for `bash deploy/bootstrap-gcp.sh`; preserving the entry point + delegating to Python is cheap. Plus existing docs keep working. |
| **B5.2 token rotate** | **Immediate cutover by default**; `--grace 24h` flag for operators who need overlap | The safe default for security ops is "old token invalid the moment new one issued." Grace is for the rare deployment-rollover case where the new token isn't propagated yet. |

## Slice order

5.4 → 5.2 → 7.3 → 7.1 → 8.1. Easiest → hardest, picking up cheap
wins first. Each slice is independent: no hard dependencies. Each
can ship as its own commit + smoke validation.

---

## Slice 1: B5.4 — userns-remap (per-acquire opt-in) — **SHIPPED 2026-05-10**

**Shipped shape** (commit `620f282`). Operators can opt in to
the docker daemon's `userns-remap` config on a per-acquire basis
via the raw-container SDK:

```python
async with await client.acquire_container(
    image="my-task:1",
    userns_mode="remap",   # default is "host"
) as session:
    ...
```

**What landed**:

- Wire fields: `string userns_mode` on `AcquireContainerCommand`
  (spec-21) + `AcquireContainerRequest` (rollout-control).
- SDK: `Client.acquire_container(userns_mode: Literal["host",
  "remap"] = "host")`. Threaded through transport → service →
  raw-container coordinator → node agent → `RawContainerManager.
  acquire` → docker-py's `userns_mode` kwarg.
- Translation at the docker boundary: `"host"` →
  `docker run --userns=host` (explicit opt-out of daemon remap);
  `"remap"` → `docker run --userns=""` (use daemon default).
  Setting `userns_mode="host"` **explicitly** (rather than
  omitting the kwarg) is what gives "default off" semantics on
  operator daemons that already have userns-remap configured.

**What was NOT shipped** (lower priority; revisit if asked):

- **Case-1 path** (stub-bound sandboxes via
  `NodeAgent.create_sandbox`). The userns_mode field doesn't
  exist on `ResourceSpec` or the case-1 wire. If a future
  consumer needs userns-remap for an xrlenv-native template, add
  it there.
- **Per-template manifest field**. Today the operator/SDK caller
  decides per-acquire. A template-level "this image needs
  in-container root, don't ever try to remap" annotation could
  prevent operator mistakes; not load-bearing now.

**Tests** (+2 in `tests/unit/node/test_raw_container.py`):
- `test_acquire_default_userns_mode_is_host` — default acquire
  passes `userns_mode="host"` to docker (regression guard).
- `test_acquire_userns_remap_opts_in` — explicit `"remap"`
  passes empty string to docker.

**Docs**: `docs/build_with_xrlenv/work_with_xrlenv_managed_containers/
direct_api.md` § "Security: opting in to ``userns-remap``" —
SDK usage + daemon-config prerequisite + rationale for
opt-in default.

---

## Slice 2: B5.2 — token rotate / revoke — **SHIPPED 2026-05-10**

**Status.** Landed on `dev/phase1-slim`. Both methods present on
`TokenStore`; three CLI subcommands wired; 23 new unit tests
cover the rotate/revoke/list + interceptor-level rotate-then-401
contract. mypy + ruff + sphinx clean; full pytest 1346 passed.

**Shipped surface.**

```bash
xrlenv tokens rotate <role>            # immediate cutover (default)
xrlenv tokens rotate <role> --grace 24h    # overlap window
xrlenv tokens revoke <token-id>            # by id or unique ≥6-char prefix
xrlenv tokens list                     # show active + grace + revoked
```

**On-disk additions** (under `~/.xrlenv/secrets/` or the
`--secrets-root` override):

- `<role>.token` — current active bearer (unchanged, mode 0600).
- `<role>.token.previous.json` — `{"token", "grace_until"}` sidecar
  written by `rotate --grace`; mode 0600; entries past their
  `grace_until` are ignored. Cleaned up automatically on the next
  immediate-cutover rotate.
- `revoked.json` — append-only list of `{"token_id", "revoked_at"}`
  records. Operators can grep this file from audit logs. The
  control plane's `TokenStore.maybe_reload()` watches it alongside
  the role files and picks new revocations up on the next RPC.

**Identifiers.** Adds `TokenIdentity.token_id` (12 hex chars). The
first 6 chars equal the existing `digest_hint`, so an operator
quoting a 6-char log fingerprint still resolves uniquely via
`tokens revoke <prefix>`. Prefixes shorter than 6 chars are
rejected outright; ambiguous prefixes name the candidates.

**Decisions taken**: 12-char `token_id`; per-role grace sidecar
files (not a global state.db row) so the model stays stateless
and hot-reloads via the same mtime-watch path as `tokens issue`.

---

## Slice 3: B7.3 — admin HTTP basic auth + two-tier roles — **SHIPPED 2026-05-11**

**Status.** Landed on `dev/phase1-slim`. Admin server now gates every
non-`/healthz`-non-`/static` request through a single middleware
classifier; viewer + operator roles wired; token prefix convention
in place; bind guard relaxed to allow public binds when a TokenStore
is wired. Full pytest 1361 passed; mypy clean; ruff clean; sphinx -W
clean.

**Shipped surface.**

- New role `viewer` in `Role` union; new scope `admin.read`.
- `xrlenv tokens issue viewer` emits a `read_<urlsafe32>` token;
  `xrlenv tokens issue operator` emits `write_<urlsafe32>`. `node` /
  `consumer` stay unprefixed (systemd / env-installed, rarely shared).
  Existing unprefixed tokens still verify — the prefix is generator-
  side only.
- `Authorization: Basic <base64(username:password)>` accepted on every
  admin HTTP route. Browser flow: navigate to `http://host:8080/`,
  basic-auth prompt fires (`WWW-Authenticate: Basic
  realm="xrlenv-admin"`), enter `viewer` / `<read_...>` → panel loads.
- `Authorization: Bearer <token>` still accepted (existing CLI flow
  unchanged). Both transports go through the same middleware.
- Read routes (every GET except `/healthz` + `/static/*`) require
  `viewer | operator`. Write routes (every other method, today all
  POST) require `operator`. `/healthz` and `/static/*` stay open for
  load balancers + the pre-auth challenge page.
- Username must match the bearer's role: a viewer-username + operator-
  token combo fails 401. Prevents prefix-confused escalation.
- Bind guard relaxed: `--admin-allow-public` is now accepted when the
  TokenStore is non-empty. Without auth, public binds still raise
  `AdminBindError`; the SSH-tunnel workaround remains valid for
  no-token setups.
- Loopback dev escape hatch preserved: empty TokenStore on
  `127.0.0.1` no-ops the middleware (quickstart still works without
  tokens).

**Decision on the open question** (was: "should consumer tokens map
to viewer for admin?"): **no** — consumer tokens are gRPC-side
identities and now 401 the admin HTTP path. Operators wanting
read-only admin access issue a `viewer` token; the two surfaces stay
separate.

**Tests.** 9 new admin-server tests cover: open-when-store-empty,
401-on-missing-creds (with `WWW-Authenticate`), viewer/operator
basic-auth accept, viewer-cannot-write 403, username-role-mismatch
401, unknown-token 401, malformed-header 400, `/healthz` stays open.
5 new security-module tests cover `generate_token` (admin prefix +
RPC raw + round-trip + role-table sync + legacy-unprefixed compat).

**Files touched.**
- `xrlenv/control/security.py` — `Role` += `viewer`; `Scope` +=
  `admin.read`; new `ROLE_TOKEN_PREFIX` + `generate_token(role)`;
  `_load_from` now walks 4 roles.
- `xrlenv/admin/server.py` — split `_require_operator` →
  `_require_role(allowed_roles)`; new middleware classifier; bind
  guard rewritten.
- `xrlenv/cli/{__main__,commands}.py` — viewer added to the role
  choices; both `tokens issue` + `tokens rotate` use the new
  `generate_token` helper.
- Tests + Sphinx docs (`observability/admin_auth.md` is new;
  `developer_guide/tokens.md`, `cli_reference.md`,
  `developer_guide/security.md`, `observability/admin_panel.md`,
  `deploy/multi_node_deployment/runbook.md` all updated).

---

## Slice 4: B7.1 — OTel spans — **SHIPPED 2026-05-11**

**Status.** Landed on `dev/phase1-slim`. New `xrlenv/observability/
tracing.py` lazily initializes the tracer from `OTEL_*` env vars on
first use; eight hot-path call sites wrap their bodies with
`get_tracer().start_as_current_span(...)`. Default (no env var) is a
noop tracer that costs roughly one dict lookup + one `with` block —
verified the existing 1363-test baseline didn't regress. With
`OTEL_TRACES_EXPORTER=console` or `OTEL_EXPORTER_OTLP_ENDPOINT=...`
configured, spans land in the chosen exporter without further code
change.

**Shipped surface.**

Three modes selected at first `get_tracer()` call:

- **Off (default)** — no env var → noop tracer. The whole module is
  safe to import without `opentelemetry-*` installed; it falls back
  to a private `_NoopTracer` whose `start_as_current_span` is a
  zero-cost context manager. The default install path stays free of
  the dependency.
- **Console (dev)** — `OTEL_TRACES_EXPORTER=console` → spans
  pretty-printed to stderr via `ConsoleSpanExporter`. Useful for
  local debugging; never enable in prod.
- **OTLP (prod)** — `OTEL_EXPORTER_OTLP_ENDPOINT=http://host:4317` →
  spans export to that OTLP gRPC endpoint via
  `BatchSpanProcessor` (async, never blocks a hot path).

The two env vars are not mutually exclusive; setting both wires
both processors.

**Instrumentation points** (8 spans, all on the control-plane / node
hot path):

| Span | Where | Attributes |
|---|---|---|
| `xrlenv.coordinator.dispatch_rollout` | `RolloutCoordinator.start_rollout` | `template`, `task_key`, `group_id`, `deadline_s` |
| `xrlenv.coordinator.build_apply` | `BuildCoordinator.apply` | `dry_run`, `force`, `eager`, `skip_if_present`, `applied_by` |
| `xrlenv.scheduler.place` | `Scheduler.place` | `template`, `image`, `backend`, `node_count` |
| `xrlenv.node.create_sandbox` | `NodeAgent.create_sandbox` | `rollout_id`, `backend`, `image`, `node_id` |
| `xrlenv.node.env_step` | `NodeAgent.env_step` | `sandbox_id`, `node_id` |
| `xrlenv.node.ensure_present` | `ImageCacheManager.ensure_present` | `image`, `deadline_s`, `cache_hit` |
| `xrlenv.node.source_build` | `SourceBuilder.build` | `image_ref`, `source_type`, `skip_if_present`, `timeout_s` |
| `xrlenv.transport.rpc` | `RemoteNodeTransport._send_and_wait` | `command_kind`, `node_id`, `timeout_s` |

**Decision taken** (was: batch vs sync exporter): **batch**
(`BatchSpanProcessor`, OTel default) — async export keeps the hot
path free of network I/O. The noop fallback handles the "no SDK
installed" case so the slice doesn't drag a hard dep into the core
package.

**Tests.** New `tests/unit/observability/test_tracing.py` (7 tests):
no-env returns noop, console-env returns real tracer, OTLP-env
returns real tracer, lazy-init is cached, noop span accepts all the
methods call sites use, noop `__exit__` doesn't swallow exceptions,
end-to-end span emission via an isolated `InMemorySpanExporter`.

**Files touched.**
- New `xrlenv/observability/tracing.py` (~210 lines) with
  `get_tracer()` + noop fallback classes.
- 8 instrumentation sites wrapping the existing method with a `with
  get_tracer().start_as_current_span(...)` block; long methods
  refactored into a small public wrapper + `_<name>_impl` body to
  keep the indentation diff minimal.
- `pyproject.toml` — new `[observability]` extra
  (`opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-grpc`).
- Docs: new `docs/observability/tracing.md` (env-var + Jaeger
  walkthrough + 8-span catalog + custom-span guidance), wired into
  `docs/observability/index.md` + `docs/index.rst`;
  `docs/observability/admin_panel.md` cross-link in "See also".

---

## Slice 5: B8.1 — `xrlenv bootstrap` subcommand — **SHIPPED 2026-05-11**

**Status.** Landed on `dev/phase1-slim` (code + tests + Sphinx docs
+ planning closure). **Operator-driven smoke test pending** — the
user is running the new entry point on a real GCP/AWS VM tomorrow
to confirm parity with the bash scripts before we declare this slice
operationally validated.

**Shipped surface.**

```bash
xrlenv bootstrap --target {gcp,aws,linux-generic}
                 [--control-plane host:port]
                 [--node-id <id>]
                 [--target-os {amzn,rhel,fedora,ubuntu,debian}]
                 [--xrlenv-wheel /path | --xrlenv-repo /path | --xrlenv-version VER]
                 [--runtime-user USER]
                 [--install-root /opt/xrlenv]
                 [--skip-operator-docker-group]
                 [--dry-run]
```

**Files touched.**

- New `xrlenv/cli/bootstrap.py` (~1000 lines, stdlib-only): OS probe
  via `/etc/os-release`, cloud metadata auto-detect (GCP metadata
  service + AWS IMDSv2), Docker install branches (apt for
  Debian/Ubuntu, dnf for AL2023/RHEL/Fedora, upstream Docker apt
  repo on GCP), full `bootstrap_xrlenv` sequence (ensure_user,
  ensure_directories, python venv install with native-distro →
  uv-managed fallback, pip install from wheel/repo/PyPI, systemd
  unit + node-token drop-in, operator docker-group add). Each step
  is a `Step` dataclass with `description`, `commands`,
  `python_fn`, and idempotent `skip_if` predicates. The runner has
  a `--dry-run` mode that prints the plan without touching the host.
- `xrlenv/cli/__main__.py` — `bootstrap` subparser + dispatch.
- `deploy/bootstrap-gcp.sh`, `deploy/bootstrap-aws.sh` — rewritten
  as 3-line wrappers that `exec python3
  $REPO_ROOT/xrlenv/cli/bootstrap.py --target {gcp,aws}
  --xrlenv-repo $REPO_ROOT "$@"`. The wrappers invoke the module as
  a **flat script** (not via `python3 -m xrlenv.cli.bootstrap`) so
  they don't trigger `xrlenv/__init__.py`'s pydantic import — the
  bootstrap is supposed to install pydantic, so it can't depend on
  it. `bootstrap.py` is stdlib-only by design.
- `deploy/bootstrap-common.sh`, `deploy/_preflight.sh`, and
  `deploy/refresh.sh` left **intact**: `refresh.sh` still uses
  `bootstrap-common.sh` as a helper-function library (its
  fast-path "I just `git pull`-ed, refresh the venv" flow shares
  the same `ensure_directories` + `install_systemd_unit` logic).
- New tests: `tests/unit/cli/test_bootstrap.py` (23 cases) cover
  OS probe (override + family bucketing + unsupported-distro
  reject), `build_config` (env-flag precedence, missing-knob
  errors, wheel/repo path validation, SUDO_USER pickup), `build_plan`
  (target-specific step ordering, optional token-drop-in step,
  optional operator-docker-group step, install-source priority),
  and `run_plan` dry-run behaviour (commands previewed, skip_if
  honored, python_fn not invoked).
- Docs: `docs/deploy/multi_node_deployment/runbook.md` step 4
  rewritten with `--dry-run` as the recommended first step;
  `docs/developer_guide/cli_reference.md` gains an
  `xrlenv bootstrap` flag table.

**Decisions taken** (were "decisions left" in the original plan):

- **OS detection**: probe `/etc/os-release` by default, with
  `--target-os` as the operator-override knob (custom AMIs whose
  os-release is missing or wrong).
- **Pre-flight failures**: validation errors raise `ValueError`
  with operator-friendly messages and exit code 2; environment
  errors raise `RuntimeError` and exit code 2. The previous bash
  `die "..."` shape carries over verbatim where possible.
- **`--idempotent` flag**: dropped. Every step in `build_plan` is
  inherently idempotent via `skip_if`, so an explicit flag was
  redundant. Re-running the bootstrap on an already-bootstrapped
  node is fast and safe by default.
- **`refresh.sh` conversion**: deferred. Slice 5's scope was the
  `bootstrap-{gcp,aws}.sh` rewrites; `refresh.sh` has its own
  semantics (skip heavyweight steps, reinstall venv only) and
  works fine today as bash. A future B8.x slice can fold it into
  `xrlenv bootstrap --refresh-only` if the value emerges.

**Pending operator validation.** Tomorrow's smoke flow:

1. SSH into a freshly provisioned GCP VM (Debian 12 or Ubuntu 22.04).
2. `git clone` the repo + `sudo -E bash deploy/bootstrap-gcp.sh
   <control-plane:port> --dry-run` — sanity-read the plan output.
3. Re-run without `--dry-run`. Confirm the node attaches to the
   control plane (check `journalctl -u xrlenv-node -n 30` + the
   admin panel's `/nodes` page).
4. Repeat on an AWS EC2 AL2023 VM. Confirm dnf-install path works.
5. Re-run the bootstrap on the same VM (idempotency check). All
   steps should report "skipped — already done".

**Cost (estimated → actual)**: 2-3 days estimated; code + tests +
docs landed in one session (~½ day) thanks to careful bash
mirroring; operator-validation half-day still ahead.

---

## Cross-cutting

### Test strategy

- Unit tests for every slice; existing 1320 baseline expected to
  stay green throughout.
- Each slice gets ≥1 smoke test that exercises the operator-
  facing surface.
- Doc changes accompany each slice (build_plan.md tech doc
  pattern: a new subsection per feature).

### Wall-clock estimate

| Slice | Cost |
|---|---|
| B5.4 | ½ day |
| B5.2 | ½ day |
| B7.3 | 1 day |
| B7.1 | 2-3 days |
| B8.1 | 2-3 days |
| **Total** | **~6-8 days** |

### Phase-1.x acceptance write-up

After all 5 slices land, update `notes/phase-1-acceptance.md`
to reflect the 5-item exit checklist + the revised scale gate
(200 concurrent raw containers via drop-in, no trainer half).
That's a follow-up slice (~1 day) and the final phase-1 closure.

### Out of scope (deliberately)

These are NOT part of this slice — they live in phase 2:

- Trainer integration (B1.*)
- Redis StateStore (B6.1-2)
- CubeSandbox (B4.1)
- Warm pools (B4.3)
- Function-Call mode (B4.2)
- OSWorld / web_search plug-ins (B2.2-3)
- `xrlenv analyze` CLI (B9.*)

See `notes/phase-2-todo.md` for the full phase-2 backlog.
