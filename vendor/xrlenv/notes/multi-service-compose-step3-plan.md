# Step 3 — CP compose acquire: design + implementation plan

Expands step 3 of `notes/multi-service-compose-plan.md` (the control-plane half).
This is the *plan*, not code — it names concrete touch-points and the decisions
the implementation follows, in the style of `notes/evoclaw-fleet-reservation-to-do.md`.

Specs: `specs/03` (scheduler — footprint placement, subnet exclusion), `specs/10`
(capacity accounting), `specs/09` (GC), `specs/21` (node protocol — done in 2b),
`specs/02`/`05` (consumer→CP RPC surface).

Prereqs already landed: **2a** CP vet (`xrlenv/control/compose_policy.py`),
**2b** node primitive (`AcquireComposeProjectCommand`/`DestroyComposeProjectCommand`
+ `RawContainerManager.{acquire,destroy}_compose_project`). Step 3 drives them from
the control plane.

> **Rev 2 (2026-07-15).** Corrected after a capacity/GC/transport review. Key fix:
> a compose project is modeled as a **session-like `main` record in `_sessions`**
> carrying the footprint as its resources — not a bare placement — because
> `commit_placement`/`release_placement` only touch `_pending` (in-flight), and
> steady-state capacity + deadline/liveness/ghost coverage both come from
> `_sessions` via `iter_load_entries()` / `list_sessions()`. See §2/§3/§4.

---

## 0. Where compose is SIMPLER than fleet, and where it isn't

The fleet slice solved capacity for **N separate `acquire_container` calls** sharing
a `fleet_id`, needing member-suppression + a ref-count table. A compose project is
**one acquire + one destroy** (the node brings up N containers internally), so:

- **No member sessions, no suppression.** There is exactly one CP-side session (the
  `main` record); the sidecars are never CP sessions. So `iter_load_entries` needs
  **no new loop and no suppression** — the single `main` session simply carries the
  whole-stack **footprint** as its `effective_resources` (§4). Simpler than fleet's
  two-loop transform.
- **No companion/ref-count table.** Reserve + release as one unit on the single
  project acquire / destroy.

What compose adds that fleet didn't: a **new consumer→CP RPC** (§1), **new CP→node
transport methods** (§2.1 — the node has the commands but the transport layer
doesn't send them yet), the **§3.5 vet** (§2), **subnet anti-affinity** (§5), and
a **multi-image placement/digest policy** (§2.2).

**The default single-container path stays sacred** — every change is a new RPC /
method / branch. No single-acquire code is touched.

---

## 1. Consumer → CP wire (`rollout_control.proto`, `rollout_endpoint.py`)

New RPCs on `service RolloutControl` (alongside `AcquireContainer` /
`DestroyContainer`), **not** an `AcquireContainer` overload:

```
rpc AcquireComposeProject (AcquireComposeProjectRequest) returns (AcquireComposeProjectResponse);
rpc DestroyComposeProject (DestroyComposeProjectRequest) returns (DestroyComposeProjectResponse);
```

**`AcquireComposeProjectRequest`** — mirror the **fields the endpoint actually uses**
today, *not* the legacy `AcquireContainerRequest.deadline` (the endpoint ignores it
in favour of the two explicit timeouts below):
- `string compose_yaml` — the rewritten, image-ref-only, plugin-assembled document
  (main synthesized from harbor's base layer). The CP vets it (§2) + derives the
  subnet claim from it (§5).
- `repeated string images` — refs to ensure-present (main + sidecars).
- `string main_service` (default `main`).
- **`ResourceSpec footprint`** — whole-stack `cpu_request` / `mem_request`,
  **plugin-computed** (`main` declared cpu/mem from task.toml + `compose.
  sidecar_footprint`). The CP can't derive `main`'s declared size, so it must
  arrive on the request. Scheduler input for `place(reserve=footprint)`.
- `optional double queue_timeout_s` — admission-queue wait budget (mirrors the
  container path's `queue_timeout_s`, NOT `Deadline`).
- `optional double session_deadline_s` — wall-clock cap; the CP force-reaps the
  project past it (mirrors the container path).
- `double up_timeout_s` — forwarded to the node `up --wait`.
- `optional string project_name` (else CP-derived from the session id),
  `optional string task_key`, `optional string group_id`, `map labels`,
  `optional string request_id` — same semantics as the container request.

**`AcquireComposeProjectResponse`**: `rollout_id`, `node_id`, `main_container_id`,
`main_container_name`, `project_name`, `map<string,string> service_container_ids`,
`double queue_wait_s`.

**`DestroyComposeProjectRequest`**: `rollout_id`, `project_name`. Response empty.

Handler: `rollout_endpoint.py` gains the two methods next to the container ones,
delegating to the coordinator. Regen via `scripts/gen_protos.sh`.

## 2. Coordinator `acquire_compose_project` (`raw_container_service.py`)

A **new method** (the flow diverges too much from `acquire()` to branch it). Flow,
mirroring the single-container acquire's placement/session/commit lifecycle:

The order **matches `acquire_container` exactly** (vet → digest → row → place —
lines ~925-980): validate before writing any row (a policy reject leaves no row),
resolve the digest before recording (so the row/session/affinity/`ensure_present`
all key on the pinned bytes), *then* write the `acquiring` row before wire activity.

1. **Parse + vet** — `compose_policy.vet_compose_project(parsed_yaml, policy=
   self._kwargs_policy)`. A `KwargsPolicyViolation` fails fast (user-error,
   non-retryable) **before any row is written** — no acquiring-ghost left behind
   (mirrors the `validate_kwargs` block at ~925-933, and the digest note at ~944).
2. **Digest-resolve + pin the main image consistently** (§2.2) — resolve the
   **main** image tag→digest via `self._digest_resolver.resolve(image)` (~945, and
   *before any record/placement*), and thread the resolved ref through **all three**
   places: `manifest.image` (placement/affinity), the `main`/`app` `image:` in the
   rewritten `compose_yaml`, and the matching entry in the `images` list sent to
   the node. If they diverge, the node `ensure_present`s one ref (tag) while
   `docker compose` runs another (digest) → a hot-path pull. A resolver failure
   fails the acquire here, still before any row.
3. **Mint `rollout_id` + write the `acquiring` row** — `_record_acquiring(...)`
   (~964, *"write the acquiring record before any wire activity"*) persists the
   `raw_rollouts` row `status="acquiring"`, then add `rollout_id` to `_acquiring_ids`
   (~980). This makes a long **queue wait** or slow **compose up** a proper
   in-flight acquire (the raw-GC reconciler treats an `acquiring` row as a ghost
   **iff its `rollout_id` is not in `list_acquiring_ids()`** — membership, not a
   fixed age; `raw_gc_reconciler.py:675-677`) rather than a **missing row**. (3a
   writes this row; the *compose-project* persisted row is 3c.)
4. **Stamp reserved labels into `compose_yaml`** (§2.3) — now that `rollout_id` is
   minted and `project_name` derived, the **CP** injects `xrlenv.rollout_id`,
   `xrlenv.session_kind`, and `xrlenv.compose_project=<project_name>` into **every
   service** in the compose document. Core owns these labels (the plugin can't —
   it never sees the CP-minted `rollout_id`); this is what lets the reconciler
   correlate node containers → CP row (§6).
5. **Place / admit** — `scheduler.place(manifest, reserve=footprint, image_present=
   <main image affinity §2.2>, task_key=…, exclude_node_ids=<subnet conflicts §5>)`.
   The `reserve=footprint` path exists (fleet slice). Doesn't fit → `AdmissionQueue`
   (footprint-sized wait; one *project* = one queued unit; the `acquiring` row +
   `_acquiring_ids` cover this window). Placement enters `_pending`. Then **issue**
   the node `AcquireComposeProjectCommand` (with the labeled + digest-pinned
   `compose_yaml`) via the **new transport method** (§2.1).
6. **On the reply — hand `_acquiring` → steady-state** exactly like a single acquire:
   - Discard `rollout_id` from `_acquiring_ids` and register a **session-like `main`
     record** in `self._sessions[rollout_id]` (a `RawContainerSession` with
     `container_id=main_container_id`, `effective_resources=footprint`, `node_id`,
     `task_key`, and a new in-memory `compose_project_name` marker). This is what
     `iter_load_entries()` charges (§4) and what `list_sessions()` exposes to the
     deadline/liveness/ghost sweeps (§3). The sidecars are **not** CP sessions.
   - Register `self._compose_projects[rollout_id] = _ComposeProjectRecord(
     project_name, node_id, service_container_ids, subnet_claims, footprint)` —
     the metadata a `RawContainerSession` can't hold (§field-split).
   - `commit_placement(placement)` — drops the `_pending` reservation (steady-state
     is now the session). **Not a load charge** — the load lives in the session.
   - **Update** the `raw_rollouts` row `status="acquiring"` → `"running"` with the
     `main` container id/name (the `update_raw_rollout` path, ~1547/1812).
7. **Failure paths.** A **vet or digest failure** (step 1/2) leaves **no row** —
   nothing to unwind. A failure **after the `acquiring` row** (step 3+: label-stamp,
   place, node error) discards `_acquiring_ids`, `release_placement(placement)` if
   a placement was made (drops `_pending`), marks the `raw_rollouts` row failed,
   and best-effort node `DestroyComposeProjectCommand` (the node also self-tears a
   failed `up`). No session/`_compose_projects` record exists yet.

### 2.1 CP→node transport wiring (part of 3a — do NOT skip)

The node has the commands (2b), but the **control-side transport does not send
them**: `NodeTransport` (Protocol, `node_transport.py:50`) + its local impl +
`RemoteNodeTransport` (`grpc_endpoint.py:107`) expose `acquire_container` /
`destroy_container` but **no compose methods**. 3a must add
`acquire_compose_project` / `destroy_compose_project` to the Protocol, the local
transport, and `RemoteNodeTransport` (build the `AcquireComposeProjectCommand`,
send over the bidi stream, unpack `AcquireComposeProjectReply`). Without this the
coordinator can't reach the node.

### 2.2 Multi-image placement + digest policy (decision)

Single-container image-affinity is single-image (`manifest.image`); compose has
`repeated images`. Decision:
- **Affinity on the `main` image only.** It's the task-specific, largest image;
  sidecars are small public images (`postgres:14`, `fake-gcs`) likely cached
  cluster-wide, and pulled by the node's `ensure_present` regardless. So
  `place(image_present=<main-image affinity>)`; the node command's `images` list
  carries all refs for ensure-present. (An "all images present" aggregate query is
  a future optimization, not v1.)
- **Digest resolution: main image yes, sidecars v1-no.** Resolve the `main`/`app`
  image tag→digest (matches the raw-container freshness model — the task image is
  the one repushed under `:main`), and thread the resolved ref through
  `manifest.image`, `compose_yaml`, and the `images` list **consistently** (§2
  step 2 — a divergence is a hot-path pull). Public sidecar tags stay as tags in
  v1 (they're externally pinned; digest-pinning every sidecar is a stated future
  option).

### 2.3 Reserved-label stamping is CP-owned (not plugin)

**The plugin cannot label services with `xrlenv.rollout_id`** — the CP mints the
`rollout_id` (`uuid.uuid4().hex`, line ~952) *after* the request arrives, so the
consumer never sees it. Label ownership therefore lives in **core**, matching the
single-container path where the node/platform merges `xrlenv.rollout_id` +
`xrlenv.session_kind=raw` onto the container (the `AcquireContainerCommand.labels`
proto note: *"the node always merges … operators should not override those keys"*).

For compose, the **coordinator** (§2 step 4), after minting `rollout_id` + deriving
`project_name`, injects into **every service** in `compose_yaml`:
- `xrlenv.rollout_id=<minted id>` — GC/restart correlation (§6);
- `xrlenv.compose_project=<project_name>` — group node containers into the project;
- `xrlenv.session_kind` — **decided now, split by role, for 3a safety** (see below).

**`session_kind` must be split `main`=`raw` / sidecars=`compose` — not all one
value.** The raw-GC node-truth diff (`raw_gc_reconciler.py:280-354`) computes, per
node, `docker_set` (the node's `list_raw_containers`, **filtered to
`session_kind=raw`** — `raw_container.py:732`) vs `coord_set` (this node's
`_sessions` container ids), and acts on **both** directions:
`docker_set − coord_set` → node-only **orphans → force-destroyed**;
`coord_set − docker_set` → **`coordinator_only` → sealed as lost** (349-354).
- **Sidecars → `xrlenv.session_kind=compose`.** They are **not** CP sessions, so
  labeling them `raw` puts them in `docker_set` but not `coord_set` → node-only
  orphans → the existing sweep **force-destroys them individually**, killing a live
  project. `compose` keeps them out of the raw-filtered node query entirely →
  invisible to the existing sweep, **safe with no 3c change**.
- **Main → `xrlenv.session_kind=raw`.** Main **is** a CP session (its container id
  is in `coord_set`), so labeling it `compose` would drop it from `docker_set` →
  `coordinator_only` → **sealed as lost every sweep**. Main stays `raw` (visible +
  matched → neither orphan nor lost); its deadline/liveness/ghost coverage runs via
  `_sessions` as usual, and reap routes to compose-down via the session marker (§3).

> **Dependency (3a): main's session `container_id` must be the FULL docker id**,
> matching `list_raw_containers` (`container.id`, 64-char). `docker compose ps
> --format json` returns the **short** 12-char id (verified on the node) — if the
> session stores that, `coord_set` (short) never matches `docker_set` (full) and
> main is classified as *both* a node-only orphan AND `coordinator_only`. So the 2b
> runner must resolve main's full id (a `docker inspect -f '{{.Id}}'`, alongside the
> existing name inspect) before returning it. Fold this into 3a (a small 2b runner
> touch-up).

**3c** then expands the node query to also return the `compose` members + their
labels and groups node-only containers by `xrlenv.compose_project`, so an orphaned
*project* (post-crash) is reaped as one compose-down and restart-readopt rebuilds
main as a compose session (not a plain raw one).

The **plugin** (step 4 of the main plan) supplies only the `compose_yaml` + an
optional `project_name` — never the reserved `xrlenv.*` labels. Stamping is a pure
YAML edit the CP does alongside the digest rewrite (both mutate the same document
before it goes on the wire).

**One entry point owns the digest consistency.** `compose_prepare.prepare_compose(
compose, images, …, resolved_main_ref=…) -> PreparedCompose(compose, images)`
stamps the labels **and** pins the resolved main ref into *both* the compose `main`
service **and** the `images` ensure-present list (via `pin_images`), so the node
can never `ensure_present` a tag while `docker compose` runs the digest. The
coordinator calls this single helper (§2 steps 2/4) rather than threading the ref
into three places by hand. (Delivered in 3a-3a.)

> **Transitional (3a-2 → 3a-3b).** The `AcquireComposeProject` /
> `DestroyComposeProject` RPCs are advertised on the wire (3a-2) so the contract is
> stable, but the coordinator that backs them lands in 3a-3b. In between, the
> `rollout_endpoint` servicer has explicit overrides that `abort(UNIMPLEMENTED)`
> with a clear message, and **no SDK `Client` method exists** (guard test), so a
> consumer cannot accidentally reach a half-wired path.

## 3. Coordinator `destroy_compose_project` + lifecycle coverage

**Explicit destroy** mirrors the container `destroy` (line ~1991), per-project:
1. Look up `_compose_projects[rollout_id]`; ownership-check.
2. Issue node `DestroyComposeProjectCommand`. **The node's `down` is strict (2b
   audit fix)** — a failed teardown returns `FAILED`.
3. **Only on node-confirmed OK**: remove the `main` session from `_sessions`
   (this is what frees steady-state capacity — §4), drop the `_compose_projects`
   row, seal the rollout row, and **kick the admission queue** (a freed footprint
   may admit a queued project). A `FAILED` reply leaves everything registered for a
   retry / the GC reaper (§6). **Do not** rely on `release_placement` here — it
   only touches `_pending`, which was already dropped at commit; it's a no-op.

**Deadline / liveness / ghost coverage — by reuse.** Because the `main` record is a
real entry in `_sessions` / `list_sessions()`, the existing RawGCReconciler sweeps
(`list_sessions()` at `raw_gc_reconciler.py:222/575/720`, `liveness_reap_candidates`)
cover the project for free. **One required change:** when the reaper decides to reap
a session, it must route a session carrying `compose_project_name` to
`destroy_compose_project` (down the *whole* project) instead of single-container
`destroy`. A small branch on the session marker in the reap path.

**Cancel.** Existing `Cancel` / `cancel_group` does **not** cover raw sessions at
all — it scans **managed** rollouts, not `raw_rollouts` (a pre-existing raw-path
characteristic, not compose-specific). Step 3 relies on **explicit
`DestroyComposeProject`** for teardown and does **not** add cancel routing. Wiring
`cancel_rollout` / `cancel_group` to compose (or to raw sessions generally) would be
**new cancel semantics** — out of scope here unless explicitly wanted.

## 4. Capacity accounting (`iter_load_entries`) — via the `main` session, no new loop

`iter_load_entries()` (line 2446) emits one `RawSessionLoad` per `_sessions` entry
from its `effective_resources`. The compose project's `main` session carries
`effective_resources = footprint`, so it is charged **once, by the existing session
loop** — no new loop, no suppression (contrast fleet). `commit_placement` /
`release_placement` are `_pending`-only and never create/free steady-state load
(confirmed: `scheduler.py:911/926`); the session's presence/absence in `_sessions`
is the steady-state charge. `_gather_cluster_load` / `capacity.py` / `fits()` are
untouched; the non-compose path (no compose sessions) is byte-for-byte. Test: a
placed project charges its footprint once; destroy removes it; a plain acquire is
unchanged (golden).

## 5. Subnet anti-affinity (step 3b)

Static-IP tasks pin a subnet the solve.sh hard-codes; two projects with the same
CIDR can't co-locate (docker refuses overlapping networks).
- **Derive** claims CP-side from the vetted compose: `compose.subnet_claims(doc)`
  (implemented in step 1). No new label — the CP has the document.
- **Exclude** at placement: `place(..., exclude_node_ids=<nodes in
  `_compose_projects` running a project whose `subnet_claims` overlap this one>)`.
  The `exclude_node_ids` param already exists (`scheduler.py` place signature) —
  reuse it; **no scheduler-core change**.
- **Overlap test**: CIDR overlap (`ipaddress`), not string-equality.
- DNS-only projects (empty claims) → never excluded → unbounded concurrency; only
  the 4 static-IP tasks serialize per-node-per-subnet.

## 6. GC / reconcile / restart (step 3c)

- **Node query change is required.** `ListRawContainersReply` returns only
  `container_ids` (+ `reaped_reasons`) — **no labels or project names** — so the
  reconciler cannot group node containers by `com.docker.compose.project` today.
  3c must either **expand `ListRawContainersReply`** (add per-container
  project/rollout labels) or add a **compose-project listing RPC**. Pick the
  expanded reply (one round-trip, reuses the existing sweep).
- **Persist** a tiny compose-project row (like `FleetReservationRecord`, invariant
  6 — metadata only, no `compose_yaml` blob): `{project_name, rollout_id, node_id,
  footprint, subnet_claims, created_ts}`, deleted on confirmed destroy.
- **Restart rebuild**: rebuild `_sessions` (the `main` record) + `_compose_projects`
  from the persisted rows + the node's live containers, correlated by the
  **CP-stamped** `xrlenv.rollout_id` + `xrlenv.compose_project` labels (§2.3 — a 3a
  CP responsibility, **not** a plugin dependency: the plugin never sees the minted
  `rollout_id`). The main-service container also carries the compose-project label,
  so the reconciler maps each node container back to its project + rollout.
- **Reap**: a project row whose node reports no live containers (past TTL, past the
  re-adoption grace — reuse the fleet TTL gating) → `DestroyComposeProjectCommand`
  + remove session/record.
- **Orphan-by-project sweep**: once the reply carries project labels, group
  node-only containers by project and down the *whole* project as a unit (a single
  compose down), not one `ForceDestroyContainer` per container.

## Field split — `raw_rollouts` row vs `_compose_projects` table

- **In-memory `_sessions` `main` record** (`RawContainerSession`, per-rollout,
  reuses the existing machinery): `rollout_id`, **main** `container_id`, `node_id`,
  `task_key`, `effective_resources=footprint`, `session_deadline`, `created_at`,
  plus a new **in-memory-only** `compose_project_name` marker on
  `RawContainerSession`. **No `state.py` schema / migration** — the marker lives on
  the session object, not the persisted row. Drives capacity + deadline + liveness
  + ghost via the existing sweeps (not cancel — §3).
- **`raw_rollouts` persisted row** — written **unchanged** by the existing
  `acquire_container` machinery: `acquiring` **after vet+digest, before placement**
  (ghost/audit coverage during the queue wait), updated to `running` with the
  `main` container id/name on the node reply (§2 steps 3/5). `rollout_id`, main
  `container_id`, `node_id`, deadline, … **No new column** for compose; the
  project-specific fields live in the table below.
- **`_compose_projects` table (in-memory) + its persisted row (3c)** (per-project,
  new): `project_name`, `rollout_id`, `node_id`, `footprint`, `subnet_claims`,
  `service_container_ids`. Holds what a single-container session/row can't — the
  sidecar member ids (for whole-project destroy), the pinned subnet (for
  anti-affinity), and the project name (for node down + restart correlation).

## 7. Backward compatibility

New RPCs, new coordinator methods, a new `_compose_projects` table, and one new
optional session marker — the single-container acquire / destroy / `place()` (no
`reserve`) / `iter_load_entries` (no compose sessions) paths are untouched. Golden
test: a plain `acquire_container` produces the identical placement + load + session
record. An old CP without the RPC returns UNIMPLEMENTED; the step-4 plugin surfaces
a clear "control plane too old for compose" hint.

## 8. Deliverable order (each an independently reviewable/auditable commit)

> **3a — DONE** (b6ce05c → 025564d). Sub-sliced: 3a-1 full-id runner
> (live-validated); 3a-2 proto RPCs + CP→node transport; 3a-3a `prepare_compose`
> (labels + image pin); 3a-3b-i coordinator state + strict teardown/reap routing;
> 3a-3b-ii `acquire_compose_project`; 3a-3c servicer→coordinator wiring. All
> audited clean.
>
> **3b — DONE** (08f5fa8). `compose_prepare.subnet_claims` / `subnets_overlap`
> (core-owned, `ipaddress`-based); `acquire_compose_project` excludes nodes running
> an overlapping-subnet project via `place(exclude_node_ids=…)`; claims stored on
> the `_ComposeProjectRecord`. DNS-only → no exclusion. GC/persistence is 3c.
>
> **3c — DONE**. Sub-sliced: 3c-1 persist the tiny live `ComposeProjectStateRecord`
> (footprint + subnet claims the node can't re-derive; deleted on confirmed
> teardown / node-loss) (e6b5efb); 3c-2 `ListRawContainersReply` per-container
> correlation labels `(container_id, rollout_id, compose_project)`, filter stays
> `session_kind=raw` (c8373c4); 3c-3 raw-GC reconciler restart-rebuild + orphan
> routing + TTL reap (this commit) — `readopt_compose_project` rebuilds the `main`
> session (compose marker + footprint) + `_compose_projects` from the persisted row
> correlated to the node's live compose-main; a node-only compose-main with no
> re-adoptable row routes to a whole-project `destroy_compose_project` (never a bare
> force-destroy that leaks sidecars); `reap_stale_compose_projects` reclaims rows
> whose project is gone past the TTL, gated on the shared re-adoption grace.
> Mirrors the fleet reconcile tests. All audited clean.

- **3a** — the core: proto RPCs + `rollout_endpoint` handlers + **CP→node transport
  methods (§2.1)** + coordinator `acquire_compose_project` / `destroy_compose_project`
  (digest-resolve main + vet + `place(reserve=)` + node command + `main` session
  with footprint + `_compose_projects` tracking + commit/release of `_pending` +
  reap-path routing on the session marker). Writes the `raw_rollouts` row
  `acquiring` (post-vet/digest, pre-placement) → `running` (on reply), matching
  `acquire_container`'s order. **Stamps the CP-owned reserved labels** (§2.3:
  `xrlenv.rollout_id` / `compose_project` per service; `session_kind=raw` on
  **main**, `=compose` on **sidecars** — so the existing raw-GC neither
  force-destroys sidecars nor seals main). Includes the 2b runner touch-up to
  resolve **main's full container id** (§2.3 dependency). §4 accounting test + §7
  golden. **No subnet, no
  compose-project persistence yet** (the compose-project row is 3c) — a project
  placed + explicitly destroyed works end-to-end, capacity charged/released
  correctly.
- **3b** — subnet anti-affinity (`subnet_claims` → `exclude_node_ids`, CIDR-overlap
  helper). Tests: two same-subnet projects don't co-locate; DNS-only unbounded.
- **3c** — DONE (see the blockquote above). `ListRawContainersReply` expansion +
  persistence + restart rebuild + TTL reap + orphan-by-project sweep. Mirrors the
  fleet reconcile tests.

## 9. Risks / decisions (status)

- **R1 (capacity handoff)** — RESOLVED: model the `main` record as a session
  carrying `effective_resources=footprint`; steady-state load = the session in
  `iter_load_entries` (no new loop); `commit_placement`/`release_placement` are
  `_pending`-only and are NOT the charge/free (§4). Destroy removes the session +
  kicks admission.
- **R2 (lifecycle coverage)** — RESOLVED: reuse via `_sessions`/`list_sessions`;
  add an **in-memory-only** `compose_project_name` marker on `RawContainerSession`
  (no `state.py` migration) + reap-path routing to compose-down. `Cancel` /
  `cancel_group` does not cover raw sessions and step 3 does **not** add it — teardown
  is via explicit `DestroyComposeProject` (§3).
- **R3 (transport)** — OPEN work item folded into 3a: add compose methods to
  `NodeTransport` + local + `RemoteNodeTransport` (§2.1).
- **R4 (footprint source)** — LOCKED: request field, plugin-computed (§1).
- **R5 (multi-image + digest)** — DECIDED: main-image affinity + ensure-present the
  rest; digest-pin the main image, sidecars stay tags in v1 (§2.2).
- **R6 (GC node query)** — DECIDED: expand `ListRawContainersReply` with per-
  container project/rollout labels (§6, in 3c).
- **R7 (restart correlation / label ownership)** — CORRECTED: the **CP** stamps the
  reserved `xrlenv.rollout_id` + `xrlenv.compose_project` (+ `session_kind`) labels
  into every compose service at acquire (§2.3, a **3a** responsibility) — the plugin
  cannot, since it never sees the CP-minted `rollout_id`. Label ownership stays in
  core, matching single-container acquire. The plugin supplies only `compose_yaml`
  + optional `project_name`.
- **R9 (`session_kind` split — 3a safety)** — DECIDED: `main`=`raw` (a CP session:
  must stay visible + matched in the node-truth diff or it's sealed
  `coordinator_only`), sidecars=`compose` (not sessions: `raw` would make them
  node-only orphans the existing sweep force-destroys). Safe with the **existing**
  raw-GC, no 3c change. **Coupled dependency:** main's session `container_id` must
  be the FULL docker id (not the short `compose ps` id) or the diff mis-classifies
  it — a 2b runner touch-up folded into 3a (§2.3).
- **R8 (row timing)** — LOCKED to match `acquire_container`: **vet → digest →
  write `acquiring` row (+ `_acquiring_ids`) → place** (§2 steps 1-4), update to
  `running` on the node reply (step 5). A vet/digest failure leaves **no row**.
  The reconciler treats an `acquiring` row as a ghost **iff its `rollout_id` isn't
  in `list_acquiring_ids()`** (membership, not a fixed age). Never create the row
  "on the reply."
