# Multi-service compose on the cluster — design + plan

Status: **design pass** (the "P1.7.C.2 multi-service compose — TBD" deferred by
`notes/p1-7-c-1-harbor-cluster-plan.md`). This is the *plan*, not the code. It
names concrete touch-points and locks the decisions the implementation follows.

Specs touched: `specs/01` (sandbox backend — new node compose primitive),
`specs/03` (scheduler — footprint-sized placement, subnet exclusion), `specs/04`
(node agent — compose-project lifecycle + GC), `specs/07` (networking — project
network + egress), `specs/09` (GC — reap the whole project), `specs/21` (node
control protocol — the compose acquire command).

Branch: `feat/multi-container`.

---

## 0. The gap in one paragraph

A harbor task may ship `environment/docker-compose.yaml` declaring **sidecar
services** (a DB, a mock cloud endpoint, peer "hosts"), a **private network**
with a pinned subnet + **static IPs**, `extra_hosts`, and `depends_on` +
healthcheck **ordering**. Upstream harbor's local mode brings the whole thing up
with one `docker compose -f build.yaml -f <task>/docker-compose.yaml up --detach
--wait` and execs solve.sh into the `main` service — networks, DNS, static IPs,
and ordering all come for free from docker compose. xrlenv's **cluster** path
(`XrlenvHarborEnvironmentCluster`) deliberately advertises `docker_compose=False`
and does a single `client.acquire_container(...)` → one `main` container, no
network, no sidecars. So any task whose solve.sh reaches a sidecar fails:
`psql: could not translate host name "postgres"`, `ssh: connect to 10.188.74.102
timed out`, the iptables 5-service nets. The node substrate itself is
single-container (`raw_container.py:acquire()` → one docker-py `containers.run`,
`network_mode` is a *string*; there is **no** `docker network create`, static-IP,
or "compose project" primitive anywhere node-side). Closing the gap is a genuine
new substrate, not a config tweak.

## 1. Corpus census (why this is worth doing, and how big)

Scan of the 200 `verified` task compose files:

| Class | Count | Handled today? |
|---|---:|---|
| No compose file | 153 | ✅ single acquire |
| Single-service compose (`main` only) | 40 | ✅ single acquire (compose ignored) |
| **Multi-service compose** | **7** | ❌ this feature |

The 7 multi-service tasks:

| task | services | network shape | status today |
|---|---|---|---|
| tw_522753 | postgres, app | default bridge, service-DNS (`postgres:5432`) | ❌ excluded |
| tw_299387 | fake-token, fake-gcs, main | `kopianet` bridge, service-DNS, healthchecks + `depends_on` | ⚠️ **passes only via a hand-written workaround** (patched solve.sh boots both emulators *inside* the one container and points hostnames at loopback — its own comment: *"the harness runs a SINGLE unprivileged container and does NOT honor docker-compose"*) |
| tw_188260 | main, solr-node, ambari-server | `10.188.74.0/24`, **static IPs**, `extra_hosts`, **per-service build contexts** (`solr-node/`, `ambari-server/`) | ❌ excluded |
| tw_304270 | main, stapp02, stapp03, stlb01 | `172.16.70.0/24`, **static IPs**, NET_ADMIN/privileged | ❌ excluded |
| tw_304271 | main, stapp02, stapp03, stlb01 | `172.16.70.0/24`, **static IPs** | ❌ excluded |
| tw_305044 | main, stapp01, stapp02, stapp03 | static IPs (two-"host" iptables) | ❌ excluded |
| tw_488034 | harbor, app | ~heavy Harbor registry stack | ❌ excluded (also has other blockers — macOS-hardcoded paths; not unblocked by this feature alone) |

So the feature makes **all 7 faithful**: unblocks 5 cleanly (tw_522753, tw_188260,
tw_304270, tw_304271, tw_305044), removes the tw_299387 workaround, and removes
*one of two* blockers on tw_488034. It also future-proofs any new benchmark whose
tasks are compose-shaped (this is a harbor-wide contract, not a TW quirk).

Two axes span the 7: **service-DNS-only** (postgres/fake-gcs — no pinned subnet,
docker auto-assigns) vs **static-IP** (188260/304270/304271/305044 — pinned
subnet the solve.sh hardcodes). The static-IP axis is where the concurrency
subtlety lives (§4.2).

## 2. Direction — compose-on-the-node (delegate to upstream)

**Chosen: run the task's real compose stack on a scheduler-chosen node.** Ship a
(rewritten — see §4.1) compose file to the node, run `docker compose up
--detach --wait` there, exec/upload/download against the `main` service over the
**existing** container-scoped wire, teardown = `docker compose down`. Networks,
static IPs, `depends_on`, healthchecks all work because docker compose itself
does them. Squarely aligned with CLAUDE.md's *"don't reinvent benchmark-side
wheels — honor upstream's filesystem contract verbatim."*

**Rejected: xrlenv re-implements compose** (a node network primitive + parse the
YAML + replay as N acquires + our own healthcheck/`depends_on` polling +
`extra_hosts`/ulimits/sysctls). This re-implements a large, moving surface inside
core and drifts the moment a task uses a compose feature we didn't model — the
exact failure mode CLAUDE.md's swebench-parser lesson warns against. Only looks
lighter.

### 2.1 Relationship to the fleet-reservation feature

Fleet reservation (landed: `RawContainerCoordinator._fleets`, `FleetReservation`,
`Scheduler.place(reserve=footprint)`, `iter_load_entries` footprint-once) solved
the **capacity/co-location half** for *N separate `acquire_container` calls*
sharing a `fleet_id`. Multi-service compose does **not** need the multi-acquire
fleet table: a compose project is **one** acquire and **one** destroy (the node
brings up N containers internally). So it reuses exactly one piece of the fleet
work — **`Scheduler.place(reserve=footprint)`** — to size placement by the whole
stack, and skips the `fleet_id` ref-counting/companion machinery entirely. The
compose-project acquire is a single scheduling unit whose footprint = the stack;
GC reaps the project as a unit. (If a future consumer wants late-arriving
companions *and* a shared network, that composes on top — but no TW task needs
it.)

## 3. Wire + substrate shape

### 3.1 New node command — `AcquireComposeProjectCommand` (spec 21)

A new command family alongside `AcquireContainerCommand` (do **not** overload the
single-container command — the shapes diverge: a project has N images, a compose
document, and a project name). Additive/optional; old nodes that don't advertise
the `compose` capability are simply never scheduled a compose task (fail-loud
`BackendCapabilityMissing`, same pattern as sysbox routing).

Fields (CP → node):
- `rollout_id` — owning rollout (scoping/GC), as today.
- `project_name` — sanitized, unique per trial (derive from `session_id`).
- `compose_yaml` — the **rewritten, image-ref-only** compose document (§4.1). No
  build context ever ships to the node.
- `images` — the set of image refs to `ensure_present` before `up` (main +
  sidecars). Node runs the image cache per ref (distribution/affinity/eviction
  already exist).
- `main_service` — which service is the exec target (default `"main"`).
- `resources` — per-project footprint (the scheduler already reserved it; the
  node applies per-service caps from the compose/resources override).
- `network_policy`, `labels`, `container_runtime`, `runtime_limits`,
  `cap_add`/`devices`/`privileged` — same semantics as the single-container
  acquire; applied to the project (compose already carries per-service
  privileged/cap_add for the iptables tasks, so these mostly pass through the
  compose document, not the command).

Reply: `container_id` of `main_service` **plus** `project_name` and the full
member `container_id` list (node tracks all of them for scoping + GC).

**Exec/upload/download need NO new wire** — they are `container_id`-scoped
(`ContainerExecCommand`, `put_archive`, `get_archive`) and target the returned
`main` container_id unchanged. This mirrors p1-7-c-1's "no proto bump for exec"
finding. Only start/stop are new.

### 3.2 Node-side (`xrlenv/node/`)

New `raw_compose.py` (sibling to `raw_container.py`) or a compose branch in the
coordinator. The node runs an **already-CP-vetted** compose (§3.5) — no node-side
policy gate:
1. Write `compose_yaml` to a temp project dir.
2. `ensure_present` each image (reuse `ImageCacheManager`).
3. `docker compose -p <project> up -d --wait` (honors `depends_on`/healthchecks
   for free; `--wait` blocks until healthy).
4. Resolve `main_service`'s container_id (`docker compose -p … ps -q main`);
   record **all** member container_ids ↔ rollout_id for scoping/GC.
5. Return them.
6. destroy → `docker compose -p <project> down -v --remove-orphans` (+ prune the
   project network). Must be idempotent and appear in the raw-GC reconciler's
   orphan sweep (spec 09) keyed on the project label, so a crashed consumer's
   whole project is reaped, not just `main`.

Serialization/health: the node's existing create/destroy gates
(`_create_gate`/`_destroy_gate`, and the sysbox-specific concurrency caps) must
extend to compose `up`/`down` — a compose `up` is N container creates; under load
it needs the same pacing that stopped the sysbox-fs wedge. (See the sysbox
concurrency memory: creates *and* destroys are serialized per node.)

### 3.3 Control-plane (`xrlenv/control/`)

- Parse the consumer→CP footprint the same way fleet does — Docker labels
  (`xrlenv.fleet_cpu_request`/`_mem_request` already exist; reuse them as the
  project footprint, no new label). The plugin sets them (§5).
- `coordinator.acquire()` gains a compose branch: `place(reserve=footprint)` for
  the whole stack, then issue `AcquireComposeProjectCommand` to the pinned node.
  `iter_load_entries` charges the footprint once for the project (a compose
  project is naturally one load entry — even simpler than the fleet member
  suppression).
- Subnet exclusion (§4.2): a static-IP task carries a `xrlenv.net_subnet_claim`
  label; the scheduler treats the CIDR as a node-exclusive resource (anti-affinity
  keyed on subnet), so two projects claiming overlapping subnets never co-locate.
- Release: node-confirmed project destroy frees the footprint + the subnet claim
  (invariant 2 — released only on confirmed destroy).

### 3.4 Client + plugin (`xrlenv/client/`, `xrlenv_plugins/harbor/`)

- `Client.acquire_compose_project(...)` → a `ClusterComposeSession` exposing the
  same `exec`/`exec_stream`/`put_archive`/`get_archive`/`apply_egress`/`destroy`
  surface as `ClusterContainerSession` but bound to the project's `main`
  container_id. The four file/exec methods in the plugin are **unchanged** — they
  already operate on a session, and the session targets `main`.
- `XrlenvHarborEnvironmentCluster` grows a compose path: when
  `self._uses_compose` (harbor already computes this) and the task is
  multi-service, `start()` calls `acquire_compose_project` instead of
  `acquire_container`; `capabilities.docker_compose` flips to `True` for those
  tasks; `stop()` destroys the project. Single-service tasks keep the exact
  single-acquire path (byte-for-byte — the default is sacred).

### 3.5 CP-side compose policy gate (security — do not skip)

`docker compose up` executes the compose document's security-sensitive fields
(`privileged`, `cap_add`, `devices`, host bind-mounts, `network_mode`) on the
node's daemon. The single-`acquire_container` path is gated by the CP's
`KwargsPolicy` (from `nodes.yaml`) — and, decisively, **the node does not
independently enforce `allow_privileged` today** (`raw_container.acquire` applies
`privileged=True` as-is; a comment there notes it's "gated by the control plane …
already approved" by the time it reaches the node). So the compose gate lives
**CP-side too**, for consistency and because the policy config lives there:

- The coordinator's compose-acquire branch **vets the compose against
  `KwargsPolicy` before issuing the node command**. Each service is mapped to the
  same `validate_kwargs(...)` call the single-acquire path uses (privileged,
  cap_add, devices, network_mode, pid/ipc/cgroup/cpuset, host binds, runtime,
  userns, platform) — one adapter, zero new policy semantics. Rejections across
  all services collect into a single `KwargsPolicyViolation` (fail-loud,
  non-retryable), so the node only ever runs an **already-vetted** compose.
- **Reject, don't silently strip.** Consistent with `KwargsPolicy` everywhere
  else: a host bind (`/var/run/docker.sock` or any `source:`-on-host volume not in
  `allowed_host_paths`), `privileged` without `allow_privileged`, a denied cap,
  `network_mode: host`/`container:` without opt-in → the whole acquire is rejected
  with an actionable message. Named volumes, `network_mode: service:*`/`none`, and
  the task's own project-scoped networks are fine.

Verified against the real corpus: the **6 tasks this feature unblocks** vet clean —
the 3 non-privileged (tw_522753/299387/188260) under default policy, the 3
privileged (tw_304270/271/305044) under the operator's `allow_privileged` opt-in;
**none mount a host path**. The 7th, **tw_488034** (already blocked by its
macOS-hardcoded paths + ~10-service weight), mounts `/app:/app` and is correctly
**rejected** by the gate — the right outcome, and evidence the host-bind check
works on real data. So reject-vs-strip is moot for the tasks we actually ship.
This gate is what lets multi-service run under **runc** (no sysbox) safely.
Node-side defense-in-depth (a node that independently refuses privileged/
host-mounts) is deferred — it would need new per-node policy config on *both* the
compose and single-acquire paths, out of scope for this slice.

## 4. The three hard sub-problems — decisions

### 4.1 Image/build story — DECISION: pre-build every service image, push, rewrite compose to image-refs

Two candidates were on the table; **pre-build + push + rewrite** wins.

- **Public sidecars** (`postgres:14`, `fsouza/fake-gcs-server:latest`): no build,
  the node pulls via the image cache. Already solved.
- **The `main`/`app` service** (built from the task Dockerfile): xrlenv already
  builds + pushes it as `terminalworld-verified/<id>:main`. No change.
- **Per-service build contexts** (tw_188260's `solr-node/`, `ambari-server/`):
  **new** — a **shared parse/rewrite helper** (Q1) owns the `<id>-<service>`
  naming. `build_plan_gen.py` calls it to enumerate services and build each
  `build:`-context into a distinct registry ref
  (`terminalworld-verified/<id>-<service>:main`), leaving `image:`-only services
  as pulls; the **plugin** calls the same helper at `start()` to produce the
  rewritten compose (every `build:` → the pushed `image:` ref) and sends that YAML
  over the wire. No rewritten file is persisted (Q1) — one naming function, no
  drift.

Only the small rewritten compose ships to the node (via the command) — **no build
context on the hot path**. Rationale:
- Consistent with today's flow (single-service already pre-builds+pushes; this is
  the N-image generalization).
- Keeps building **off the eval hot path** — deterministic, no per-trial build
  cost, matches "no surprise pulls/builds during evaluation."
- Images arrive through the existing image-cache/registry/affinity/fleet-capacity
  path; the node never needs a build context or a Dockerfile.

Rejected — *ship context + `docker compose build` on the node*: puts building on
the hot path (throughput + determinism hit) and re-opens the build-on-acquire
work P1.7.C.2 scoped as a separate ~1-week effort. The rewritten-compose approach
sidesteps it entirely.

Cost accepted: the plan generator must parse compose + build per-service
contexts. Bounded, one-time per task, and it's already the module that owns
"turn a task into pushed images."

### 4.2 Subnet collision under concurrency — DECISION: keep the compose byte-faithful; make the subnet a node-exclusive scheduling resource

Static-IP tasks pin a subnet (`172.16.70.0/24`) and the solve.sh **hardcodes**
those IPs (`ssh 10.188.74.102`). Two concurrent trials of the same task on one
node → docker refuses overlapping networks. We must **not** rewrite the subnet or
IPs (that tampers with benchmark content the solve reads).

- **Service-DNS-only tasks** (no pinned subnet — tw_522753, tw_299387): docker
  auto-assigns a distinct subnet per compose project → **no collision, unbounded
  concurrency**. Nothing to do.
- **Static-IP tasks**: the plugin extracts the pinned CIDR(s) and sets a
  `xrlenv.net_subnet_claim` label; the scheduler treats each claimed CIDR as a
  **node-exclusive resource** (anti-affinity), so it never co-locates two projects
  with overlapping subnets. Faithful (compose unchanged), correct under
  concurrency (they serialize per-node on the subnet, run free across nodes), and
  small (reuses the existing per-node resource-claim machinery the sysbox cap and
  fairness keys already use). These 4 tasks are privileged/iptables and already
  run at modest concurrency, so the throughput cost is negligible.

Rejected — *per-trial subnet remap* (rewrite subnet + every static IP
consistently): would tamper with the IPs the solve.sh hardcodes → not faithful.

### 4.3 Capacity + isolation — DECISION: footprint = whole stack, egress on the project network, GC reaps the project

- **Footprint** (Q2): the plugin declares the project footprint (`cpu_request`,
  `mem_request`) = `main(cpus,mem) + Σ sidecars × default (1 cpu / 1 GiB)`, or per
  service `deploy.resources` when the task declares it. The scheduler reserves it
  via `place(reserve=footprint)`. **The rewritten compose also injects the matching
  per-sidecar cgroup cap** (`mem_limit`/`cpus`) so the reservation is enforced —
  an uncapped sidecar can't exceed its share and OOM the node. harbor's resources
  override still sizes `main`; the footprint guarantees the node has room for the
  sum.
- **Egress**: `apply_egress` targets the project's network (the offline-task
  open-setup→tighten flow already lives in the session; extend it to iterate the
  project's containers or apply at the network level).
- **GC**: the raw-GC reconciler's orphan sweep gains a project branch — a project
  label with no live owning rollout → `docker compose down` the whole project
  (not just `main`), release the footprint + subnet claim. Node-loss / consumer
  crash reaps the entire stack, never leaks a sidecar.

## 5. Plug-in side (`xrlenv_plugins/harbor/environment.py`) — thin

Mirrors how the single-service cluster path reads task markers today. The plugin:
1. Detects multi-service (`harbor`'s `_uses_compose` + `compose.is_multi_service`).
2. **Rewrites the task compose at runtime** via the shared `harbor.compose`
   helper (Q1) — sends the resulting image-ref-only YAML over the wire; no
   artifact on disk. The rewrite has two responsibilities beyond `build:`→`image:`
   that the corpus surfaced:
   - **`main`-service synthesis / base-compose layering.** Several task composes
     don't define `main`'s image — harbor's base `docker-compose-build.yaml`
     supplies it (`build: ${CONTEXT_DIR}`). tw_522753 defines only `app`+`postgres`
     (no `main` at all); tw_188260/tw_304270 define `main` with no image. The
     plugin must reproduce harbor's base+task layering: ensure a `main` service
     exists pointing at the canonical `<id>` ref with `command: sleep infinity`.
   - **Local build-tag references.** tw_299387's `main`/`fake-token` use
     `image: terminalworld-env-299387` + `pull_policy: never` (a *local* build tag,
     not `build:`), which won't resolve on a node. The plugin passes those service
     names in the rewrite map so they repoint at the canonical registry ref
     (`rewrite_to_image_refs` already rewrites any mapped service + strips
     `pull_policy`).
3. Sets the footprint labels (§4.3, from `compose.sidecar_footprint` + `main`'s
   declared size) and, for static-IP tasks, the `net_subnet_claim` label (§4.2,
   from `compose.subnet_claims`).
4. `start()` → `acquire_compose_project`; `stop()` → project destroy;
   `capabilities.docker_compose=True` for these tasks.
5. exec/upload/download **unchanged** (session targets `main`).

No new job.yaml flag, no auto-behavior-change for single-service tasks — the
compose path only activates when the task actually ships a multi-service compose,
exactly like the sysbox routing is case-by-case.

## 6. Deliverable order (each independently testable)

1. **Shared helper + images_build compose-awareness** — the pure
   `xrlenv_plugins/harbor/compose.py` helper (parse, `<id>-<service>` naming,
   `build:`→`image:` rewrite, sidecar footprint, subnet claims) + `build_plan_gen.py`
   emitting per-service build entries for sub-directory contexts. Harbor-free
   (lazy `harbor/__init__`) so the build generator stays lightweight. Testable
   offline (plan-gen + helper unit tests; no cluster). **DONE.**
2. **Node compose primitive** — **DONE.** *2a* — CP-side compose policy vet
   (`xrlenv/control/compose_policy.py`, reuses `validate_kwargs`; real-corpus scan
   verified). *2b-1* — `xrlenv/node/raw_compose.py` `ComposeProjectRunner`
   (`up --wait` → resolve `main` + members from `ps --format json` → `down`;
   injected runner; **live-validated on a real node** — Compose v5.1.4 jsonl shape
   confirmed, clean teardown). *2b-2* — `AcquireComposeProjectCommand` /
   `DestroyComposeProjectCommand` + `AcquireComposeProjectReply` proto,
   `RawContainerManager.{acquire,destroy}_compose_project` (registers every member
   ↔ rollout_id so the existing exec/archive path addresses `main`), `NodeAgent` +
   `grpc_link` handlers. The vet from 2a is invoked by the CP in step 3, not the
   node (the node runs the already-vetted compose).
3. **CP compose acquire** — `coordinator.acquire()` compose branch: the §3.5 vet
   (from 2a), `place(reserve=footprint)`, `iter_load_entries` project charge,
   subnet-claim anti-affinity, project GC. Unit tests reuse the fleet accounting
   harness.
4. **Client + plugin** — `acquire_compose_project` / `ClusterComposeSession`,
   `XrlenvHarborEnvironmentCluster` compose path, footprint + subnet labels.
5. **Oracle-sweep validation** — drop the 5 clean tasks from `run_full_sweep.sh`
   EXCLUDE, run the oracle sweep, confirm reward 1.0 under concurrency (verifies
   subnet exclusion + footprint pacing). Remove the tw_299387 in-container
   workaround and confirm it now passes faithfully. Green set 187 → ~192.

## 7. Backward compatibility (sacred)

Single-service and no-compose tasks take the **identical** single-acquire path —
no change to `raw_container.acquire`, `place()` (no `reserve` kwarg),
`iter_load_entries`, or the plugin's single-service branch. The compose path is a
new, parallel primitive gated on the task actually being multi-service. tb2.1 /
SWE-bench / the 193 single-or-no-compose TW tasks are byte-identical. Add a golden
test asserting a plain single-service acquire is unchanged.

## 8. Out of scope

- **Multi-HOST across nodes** — every TW "multi-host" task (tw_305044 et al.) is
  actually multi-*container-on-one-node* (the "hosts" are compose services), so
  single-node compose covers them. Genuine cross-node topologies are not needed
  by this corpus and stay out.
- **Real Kubernetes** (tw_513637, tw_661946) — a different substrate (kind/k3s in
  sysbox); a compose project could *host* a single-node k8s later, but that's its
  own design.
- **Host loop devices** (tw_230695, tw_291556), **Splunk-fs** (tw_223822),
  **tw_488034's macOS-hardcoded paths + ~10-service weight** — orthogonal
  blockers this feature doesn't remove.
- **Build-on-node / build-on-acquire** — explicitly avoided by §4.1's
  pre-build+rewrite decision.

## 9. Resolved decisions (2026-07-10)

The three pre-code questions are settled:

- **Q1 — compose rewrite: a shared helper; the plugin rewrites at runtime.** One
  parse/rewrite helper (shared by `images_build` and the plugin, same namespace
  package) owns the `<id>-<service>` naming. Build-time asks it *which services
  need building + their target refs*; the plugin asks it for the *rewritten
  image-ref compose* at `start()` and sends that YAML over the wire. **No derived
  compose file is written into the cache shard** — the plugin already computes the
  registry prefix + task_id, so the single shared naming function eliminates
  build-vs-run drift. (Rejected: a `docker-compose.rewritten.yaml` sibling in the
  shared HF-derived cache — mixes source and build output.)

- **Q2 — footprint = `main` size + per-sidecar default, with a `deploy.resources`
  override and injected caps.** Default footprint = `main`'s declared cpus/mem +
  `N_sidecars × (1 cpu / 1 GiB)`; when a task declares per-service
  `deploy.resources`, read it instead. **Also inject the matching per-sidecar
  cgroup cap into the rewritten compose** so the reservation is *enforced* (a
  runaway uncapped sidecar can't exceed its share and OOM the node), not merely
  accounted. Over-reservation for the ≤3-sidecar corpus is negligible; safe
  admission is the point. (See §4.3.)

- **Q3 — CP-side compose policy gate; runc; sysbox not used.** None of the 7
  need nested dockerd/systemd (tw_304270/271 are `iptables -A … ; iptables-save` +
  `sshpass ssh` into peer containers — NET_ADMIN/privileged + sshd in peers, no
  docker-in-docker). The real risk is that `docker compose up` executes the
  compose's `privileged` / `cap_add` / host mounts **directly, bypassing the
  acquire `KwargsPolicy` gate**. Resolution (see §3.5): the **coordinator vets the
  rewritten compose CP-side, reusing `KwargsPolicy`/`validate_kwargs`**, *before*
  issuing the node command — the node applies `privileged` as-is today (it doesn't
  independently enforce `allow_privileged`), so the gate belongs where the policy
  lives. It **rejects** (fail-loud), not strips: a host bind (`/var/run/
  docker.sock` or any un-allowlisted `source:`-on-host volume), `privileged`
  without `allow_privileged`, a denied cap, or `network_mode: host`/`container:`
  without opt-in fails the whole acquire. runc + operator-opted privileged covers
  all 7; sysbox stays orthogonal (and off the per-node sysbox concurrency cap).
