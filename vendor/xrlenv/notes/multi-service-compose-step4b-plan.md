# Step 4b — harbor plugin compose routing (`XrlenvHarborEnvironmentCluster`)

Consumes the 4a SDK surface (`Client.acquire_compose_project` → `ClusterComposeSession`).
This is the last slice that makes multi-service TerminalWorld tasks actually run on
the cluster. It is **plugin-side, benchmark-faithful** code — the bar is "byte-for-byte
the same task the local harbor path would run, minus the host-local docker."

## What 4b does (plan §5)

For a task whose sandbox is a multi-service `docker-compose.yaml`, `start()` assembles
the effective **image-ref-only** compose, computes the whole-stack footprint, and calls
`acquire_compose_project` instead of `acquire_container`. `stop()` / `exec` / file
transfer are **unchanged** — the compose session targets `main`, so they already work.
Single-service tasks keep the exact current single-acquire path (byte-for-byte — the
default is sacred).

## The load-bearing constraint (why this isn't `docker compose config`)

Harbor's *local* path never materializes one compose file: it layers many `-f` files at
`docker compose` runtime (`resources` → base `docker-compose-build.yaml`/`-prebuilt.yaml`
→ task `docker-compose.yaml` → mounts json → no-network) and lets docker-compose resolve
`${CONTEXT_DIR}` / `${PREBUILT_IMAGE_NAME}` from the subprocess env at `up` time
(harbor `docker.py:215-390`).

The cluster sends **one** rewritten image-ref compose to the node (the node has no build
contexts, no host env, and must not build — images are pre-built + pushed). So the plugin
must produce the effective document itself. Two ways:

- **(A) `docker compose config` on the consumer** — reuse harbor's `-f` list + env to
  render the merged doc, then rewrite `build:`→`image:`. Maximally faithful, but
  reintroduces a docker-compose-CLI dependency on the consumer host — the very thing
  cluster mode exists to remove.
- **(B) programmatic main-synthesis (chosen, per plan §5.2)** — the merge the corpus
  actually needs is shallow: harbor's base only contributes **`main`** (`command:
  ["sh","-c","sleep infinity"]`, image = the canonical `<id>` ref). Once we rewrite
  `build:`→`image:`, `${CONTEXT_DIR}` and the whole base build-compose become **moot** (no
  build context ships). The mounts/resources/no-network overlays are handled out-of-band
  (cluster doesn't bind-mount; footprint carries main's cpu/mem; egress is separate). So
  the plugin: loads the task compose, **ensures a `main` service** exists (add it if the
  task defines only sidecars; set its image + keepalive command if it defines a bare
  `main`), rewrites every `build:`/local-tag service to its pushed ref, injects per-sidecar
  caps.

  **Helper coverage — what exists vs what 4b-1 adds (audit correction).** The naming/rewrite
  *primitives* are already in `xrlenv_plugins/harbor/compose.py`: `rewrite_to_image_refs`,
  `default_image_refs`, `subdir_build_services`, `sidecar_footprint`, `subnet_claims`,
  `is_multi_service`. What does **not** exist yet — and is the explicit, unit-tested
  deliverable of **4b-1** — is the *assembly* layer: `ensure_main_service(doc, *, main_ref,
  keepalive)`, the local-tag repoint set (`pull_policy: never` services → `main_ref`), an
  `image_refs(doc)` images-list collector, and an `assemble_project(...)` orchestrator that
  ties ensure-main → ref-map → `rewrite_to_image_refs` → images-list together. (An earlier
  draft's "already in the helper" wording overstated this — these are new.)

**Decision: (B).** It keeps cluster mode docker-free on the consumer and reuses the
shared naming primitives (no build-vs-run drift). (A) is the fallback only if a future task
needs a merge too rich for main-synthesis (none in the current corpus).

## Detection & routing

- `capabilities.docker_compose` flips **True** iff the task is multi-service — computed by
  loading `<environment_dir>/docker-compose.yaml` and applying `compose.is_multi_service`
  (a task compose declaring ≥1 non-`main` service, i.e. `>1` effective service). A
  single-`main` or compose-less task stays `False` → the current single-acquire path.
- **`disable_internet` stays `True`** for compose tasks (unchanged) — but that capability is
  only truthful for the *single-container* path, where post-install egress tightening runs
  via `apply_egress` on the one container. For a **multi-service** task, `apply_egress` is
  not yet supported (4a P2 — it would restrict only `main` and leave sidecars open, so it
  raises). **Net effect, called out explicitly here because harbor may accept an offline
  task partly on `disable_internet=True`:** an *offline* multi-service task will **fail loud
  at post-install `apply_egress`** until project-network egress lands. This is intentional
  (fail-loud beats silently leaving sidecars on the internet), not a regression — no task in
  the 6-task unblock set is offline. If the corpus grows an offline compose task, that's the
  signal to build project-network egress (a follow-on slice), not to relax the capability.
- `start()` branches on the same predicate. One helper `_multi_service_compose()` returns
  the parsed task compose dict when multi-service, else `None`; both `capabilities` and
  `start()` call it (cheap file read, no caching subtleties).
- `_uses_compose` (harbor's own property — `docker.py:188`) is **True** for any task
  shipping a compose file, including single-service. So the routing predicate is
  `is_multi_service`, not `_uses_compose` — the plan's "`_uses_compose` + `is_multi_service`"
  reduces to `is_multi_service` on the task compose (which implies the file exists).

## Effective-compose assembly (the `main`-synthesis)

Corpus shapes the helper must cover (all real, plan §5.2):
- **no `main` at all** (tw_522753: `app`+`postgres`) → **inject** `main` (image = canonical
  ref, `command: ["sh","-c","sleep infinity"]`), so harbor can `exec` into it.
- **`main` with no image** (tw_188260, tw_304270) → set `main.image` = canonical ref, keep
  keepalive command.
- **local build-tag `image:` + `pull_policy: never`** (tw_299387: `terminalworld-env-299387`)
  → repoint those service names at their pushed refs (the helper's `rewrite_to_image_refs`
  already repoints any *mapped* service + strips `pull_policy`).
- **sub-dir `build:` contexts** (tw_188260: `solr-node/`, `ambari-server/`) → distinct
  pushed refs.

Sequence in `start()` for a multi-service task:
1. `doc = compose.load_compose(<env>/docker-compose.yaml)`.
2. Ensure `main` (see the explicit contract below).
3. Build the `{service: ref}` map for every `build:`/local-tag service (see refs below).
4. `rewritten = compose.rewrite_to_image_refs(doc, ref_map, main_service="main")` —
   build→image, `pull_policy` dropped, per-sidecar caps injected.
5. `compose_yaml = yaml.safe_dump(rewritten)`.
6. `images = ` every `image:` in `rewritten` (main + sidecar refs + public sidecars like
   `postgres:14`) — what the node ensure-presents before `up`.

### `ensure_main_service` contract (fill-missing-only, never overwrite)

Compose merge is "later `-f` wins", and harbor layers the task compose **on top of** the
base — so an explicit `main.command` in the task compose overrides the base's keepalive.
`ensure_main_service(doc, *, main_ref, keepalive=["sh","-c","sleep infinity"])` must
reproduce that, i.e. **only fill fields the task left absent; never overwrite an explicit
one**:
- `main` **absent** → inject `{image: main_ref, command: keepalive}`.
- `main` present, **no `image` and no `build`** → set `image = main_ref` (the map/rewrite
  sets it when `main` builds from `.` or carries a local tag; this covers a bare `main` that
  declares neither).
- `main` present, **no `command`** → set `command = keepalive` (so it stays alive for
  `exec`); **`main.command` already set → leave it untouched.**
- every other explicit `main` field (`environment`, `working_dir`, `depends_on`, …) is
  preserved verbatim.

So a task that defines `main.command` keeps it byte-for-byte; the keepalive is a *default*,
not an override. (The current corpus doesn't define `main.command`, but the contract is
correct regardless, and 4b-1 tests the "explicit command preserved" case.)

## Image refs — build-vs-run consistency (LOCKED)

Grounded in what the build actually pushes (`build_plan_gen.py:157,218` +
`_compose_service_entries`):
- **task's own image** (`build: .` / `main`): `f"{SHARD}/{task_id}:{tag}"` =
  `terminalworld-verified/<id>:main`, registry host prefixed at push →
  `<host>:5011/terminalworld-verified/<id>:main`.
- **sub-dir build services** (tw_188260 `solr-node`/`ambari-server`):
  `hc.default_image_refs(task_id, doc, namespace=SHARD, tag="main")[service]` =
  `terminalworld-verified/<id>-<svc>:main`, prefixed at push →
  `<host>:5011/terminalworld-verified/<id>-<svc>:main`.
- **`image:`-only sidecars** (`postgres:14`): not built, pulled — left untouched.
- **local-tag `pull_policy: never`** (tw_299387): *not* separately built/pushed — it *is* the
  task's own image (harbor's local build of `environment/Dockerfile`), so it maps to the
  task's `<id>` ref.

At runtime `XRLENV_HARBOR_IMAGE_TEMPLATE` is `<host>:5011/terminalworld-verified/{task_id}:main`
(the sweep rebuilds exactly the pushed ref — `build_plan_gen.py:16-17`).

**Decision (satisfies both audit options — reuse the shared function *and* fail loud):**
the plugin builds the build-service ref map by calling the **same** function the build side
uses — `compose.default_image_refs(task_id, doc, namespace=<ns>, tag=<tag>,
main_ref=_resolve_image_ref())` — so the *naming scheme* physically cannot drift. `main_ref`
comes from `_resolve_image_ref()` (any precedence); `<ns>` / `<tag>` are **parsed from
`XRLENV_HARBOR_IMAGE_TEMPLATE`** by splitting on the literal `{task_id}` placeholder
(`prefix/{task_id}:tag` → `ns = prefix.rstrip("/")`, `tag = tag or "main"`) — an unambiguous
split (on the placeholder, never on the host's `:`). Local-tag (`pull_policy: never`, no
`build:`) services are added to the map as `main_ref`.

**Fail loud (locked):** if the task has **sub-dir build services** (`subdir_build_services(doc)`
non-empty) and the template is **absent or lacks `{task_id}`**, raise a clear
`XRLEnvError` ("multi-service task <id> has sub-dir build services {…} but
`XRLENV_HARBOR_IMAGE_TEMPLATE` … cannot produce per-service refs matching what was
built/pushed"). A task with **no** sub-dir builds needs no namespace/tag at all — every
`build:`/local-tag service resolves to `main_ref` (from `_resolve_image_ref`, so the
`docker_image`/`hb__name` precedences still work), so those tasks run without a `{task_id}`
template. 4b-1 unit-tests both the LOCKED derivation and the fail-loud path.

## Footprint

`footprint_cpu = main_cpu + Σ sidecar cpu`, `footprint_mem = main_mem + Σ sidecar mem`,
where `main_*` = the task's effective cpu/mem the single path already computes
(`_effective_cpus` × multiplier, `_effective_memory_mb` × multiplier) and the sidecar sum
comes from `compose.sidecar_footprint(doc)` (declared `deploy.resources`/`cpus`/`mem_limit`
per sidecar, else the flat default). The rewrite injects the matching per-sidecar cgroup
cap so the reservation is enforced. Passed as `footprint_cpu` / `footprint_mem_bytes` to
`acquire_compose_project`.

## Subnet claims — CP-side, nothing for the plugin

The coordinator already derives subnet claims from the `compose_yaml` itself
(`compose_prepare.subnet_claims`, 3b). The plugin sends the compose; the CP extracts +
enforces node-exclusive anti-affinity. No subnet label to set here.

## Unchanged surface (session targets `main`)

- `stop()` → `self._xrlenv_session.destroy()` — for a `ClusterComposeSession` that downs
  the whole project. No change.
- `exec` / `exec_stream` / `upload_file` / `upload_dir` / `download_file` / `download_dir`
  → all route through `self._xrlenv_session`, which targets `main`. No change. (`_xrlenv_session`
  type widens to `ClusterContainerSession | None`, already the base — `ClusterComposeSession`
  is a subclass, so no annotation change.)
- Log-dir setup (`mkdir -p /logs/... && chmod 777`) still runs against `main` — compose
  main needs those dirs exactly like the single path.

## Out of scope for 4b (fail loud, don't under-enforce)

- **Offline egress on a compose task.** `ClusterComposeSession.apply_egress` raises
  (4a P2) — project-network egress is a follow-on. The plugin's `apply_egress` propagates
  it. If a multi-service task needs post-install egress tightening, it fails loud (correct —
  silent main-only restriction would leave sidecars open). None of the 6 unblocked tasks are
  offline; revisit if the corpus grows one.
- **sysbox / systemd / inner-dockerd substrate markers.** Compose tasks run under **runc**
  (the CP policy gate makes multi-service safe without sysbox, plan §3.5). The compose branch
  does **not** honor the `XRLENV_CONTAINER_RUNTIME` (non-runc) / `_SYSTEMD_INIT` /
  `_INNER_DOCKERD` / `_INSTALL_DOCKERD` markers. A task that sets **both** a multi-service
  compose *and* one of these markers is a contradiction the branch must **reject loudly** —
  never silently drop the marker or silently pick the single path. This is a 4b-2 test (see
  below), because `start()` today has substantial marker-driven behavior the compose branch
  bypasses.

## Sub-slicing

> **4b-1 — DONE.** `ensure_main_service` / `local_tag_service_names` / `image_refs` /
> `assemble_project` in `compose.py`, LOCKED sidecar-ref derivation + fail-loud guard, 19
> tests against the four corpus shapes. Harbor-free invariant held.
>
> **4b-2 — DONE.** Plugin wired: `_multi_service_compose` detector, `start()` compose branch
> (assemble → main-cap + footprint → `acquire_compose_project`), `capabilities.docker_compose`
> True for multi-service, `_image_namespace_tag` template parse, `_reject_compose_incompatible_markers`.
> Single path refactored onto shared `_acquire_labels` / `_effective_cpu_mem_limits` /
> `_setup_cluster_log_dirs` helpers (behavior-identical — 80 existing tests green). 11 new
> tests: routing, single-path-untouched, footprint (declared + default), sub-dir-build
> template resolve + fail-loud, marker-contradiction reject (×4) + runc-allowed.

- **4b-1** — the pure assembly layer added to `xrlenv_plugins/harbor/compose.py` (the audit
  flagged these as *not* already present): `ensure_main_service`, the `pull_policy: never`
  local-tag repoint set, `image_refs(doc)` images-list collector, and `assemble_project(...)`
  (ensure-main → `default_image_refs`-based ref map → `rewrite_to_image_refs` → images list).
  Plus the template namespace/tag parse + the sub-dir-build fail-loud guard. All pure,
  unit-tested against fixtures mirroring the four corpus shapes (no-main; bare-main;
  local-tag; sub-dir builds) + the fail-loud path (sub-dir build with a `{task_id}`-less
  template). No `start()` change yet.
- **4b-2** — wire the plugin: `_multi_service_compose()` detector, `start()` branch (assemble
  → footprint → `acquire_compose_project`), `capabilities.docker_compose` True for
  multi-service; `stop`/`exec`/transfer inherited. Unit tests with a fake `Client`: the
  compose path calls `acquire_compose_project` with the assembled args; the single path is
  byte-for-byte untouched; **and the marker-contradiction reject** — a multi-service task
  carrying `XRLENV_CONTAINER_RUNTIME`/`_SYSTEMD_INIT`/`_INNER_DOCKERD` fails loud rather than
  bypassing the marker machinery silently.
- **4b-3** (optional) — live smoke on a dev worker: one multi-service task (tw_522753)
  end-to-end (acquire → exec → verify → down), 0 leftovers.

## Open questions

1. **`capabilities` cost** — it reads the task compose on every access; harbor calls it a
   handful of times per trial. Acceptable, or memoize the parsed doc on the instance?
   Leaning "read each time" (simplest, no staleness) unless it shows up hot.
2. **A task that ships a compose but is effectively single-service** (only `main`, or `main`
   + a pure `depends_on` with no real sidecar) — routed to the single path by
   `is_multi_service` (>1 service). Confirm that's the intended boundary (it matches the
   plan's predicate).

## Resolved (was open)

- **Sidecar ref derivation** — LOCKED: reuse `compose.default_image_refs` (same function the
  build uses) with `namespace`/`tag` parsed from `XRLENV_HARBOR_IMAGE_TEMPLATE` +
  `main_ref=_resolve_image_ref()`; local-tag services → `main_ref`; **fail loud** on sub-dir
  builds without a `{task_id}` template. See "Image refs — build-vs-run consistency (LOCKED)".
