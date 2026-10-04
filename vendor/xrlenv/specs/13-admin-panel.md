# 13 — Admin Panel

## Purpose

A read-only browser dashboard for monitoring the cluster: sandboxes,
hardware health, peak utilization, under-utilized nodes. Lives **inside**
the control-plane process so there's nothing extra to deploy and the user
doesn't need admin access to set up Grafana / Prometheus externally.

## Design choices

- **Bundled with control plane, not separate.** Avoids extra deploy
  unit; the orchestrator already has the data.
- **No SPA build pipeline.** FastAPI + Jinja2 + HTMX + Chart.js. All
  assets vendored under `xrlenv/admin/static/`. `pip install xrlenv`
  is enough.
- **Read-only in phase 0.** Actions (kill sandbox, drain node) wait
  for phase 1 with auth.
- **Default bind is `127.0.0.1:8080`.** Operators tunnel through SSH
  (`ssh -L 8080:localhost:8080 user@control-plane`). Public binds
  go through spec 19's admin-bind guard: `--admin-bind 0.0.0.0:8080`
  is rejected unless `--admin-allow-public` is also passed, and a
  no-auth public bind is refused outright. Phase 1 adds basic auth
  on top.

## Stack

```
xrlenv/admin/
├── server.py            # FastAPI app, mounted by control-plane process
├── templates/
│   ├── base.html        # nav, layout
│   ├── overview.html
│   ├── nodes.html
│   ├── node_detail.html
│   ├── sandboxes.html
│   ├── rollouts.html
│   ├── rollout_detail.html
│   ├── capacity.html
│   └── health.html
└── static/
    ├── htmx.min.js
    ├── chart.min.js
    └── xrlenv.css
```

## Phase 0 views

### Overview (`/`)

At-a-glance cluster summary:
- Nodes: total / healthy / degraded
- Sandboxes: running / starting / destroying
- Rollouts: in flight, completed in last hour, success rate
- Throughput: rollouts/min line chart (last 60 min)
- Control-plane uptime

### Nodes (`/nodes`)

Table:

| id | cloud | instance type | vcpus / mem / disk | backends | heartbeat | sandboxes | CPU% | mem% | disk% | 60-min peak |
|----|-------|---------------|---------------------|----------|-----------|-----------|------|------|-------|-------------|

Color-coded:
- 🟢 green = healthy
- 🟡 yellow = degraded (heartbeat 5–10 s, resource > 80%)
- 🔴 red = lost (heartbeat > 10 s) or saturated

Click a row → Node detail.

### Node detail (`/nodes/{id}`)

- Full hardware probe output.
- Capacity matrix for *this node* across all templates with binding
  constraints.
- Currently-running sandboxes on this node.
- Recent events (heartbeats, GC activity, scheduler placements).
- 60-min line charts: CPU%, mem%, disk%, network throughput.

### Sandboxes (`/sandboxes`)

Filterable table: id, template, node, status, started, age, owning
rollout, resource usage. Click for detail page with stub log tail
(last 200 lines from the in-sandbox stub).

### Rollouts (`/rollouts`)

Filterable table: id, template, status, reason, node, start,
duration, final reward, # steps, owner_id, project_id, run_id.
Filter by every column. Status enum follows spec 02:
`queued / starting / running / cancelling / finishing / finished
/ truncated / cancelled / failed`. Color coding:

- 🟢 `finished` — success
- 🟡 `truncated` / `cancelled` — soft outcomes; the trainer
  decides what they mean
- 🔴 `failed` — infra failure (drives the failure-rate alert
  on `/health`)
- ⏳ transient states render in greyscale with a spinner

Click for the trajectory viewer (spec 17). Owner/project/run
filters are phase-0-cosmetic (everything is `default`); they
become enforcement-grade in phase 2 (spec 03 multi-tenant hooks).

### Rollout detail (`/rollouts/{id}`)

Per-rollout trajectory viewer — the substantive page is described
end-to-end in **spec 17 (Trajectory Viewer)**, including the
locator → fetch → cache → render pipeline that handles trajectories
distributed across nodes and the various sink formats
(platform-jsonl, slime-sample, verl-dataproto). Phase 0 ships the
basic step list + action / observation / info rendering against
the platform-jsonl sink; phase 1 lights up the token-level overlay,
image rendering, search, and side-by-side comparison.

### Capacity matrix (`/capacity`)

Full node × template grid. Each cell shows
`current_concurrent / max_concurrent` and a binding-constraint label
from the spec-10 enum (`cpu`, `mem`,
`disk:sandbox_writable`, `disk:image_cache`, `disk:asset_cache`,
`disk:session_state`, `disk:run_artifacts`, `gpu`,
`backend_missing`). Click a cell to see why the constraint binds and
a "what would loosen this" suggestion (e.g., "`disk:image_cache`-
bound — repartitioning `image_cache` from 35% to 50% on this node
would free 4 more swebench-base sandboxes").

### Images (`/images`) — phase 1

Per-node image **and asset** cache view, powered by spec 15's
Image Cache Manager (assets share the same data structures and
eviction policy as images). One tab for images, one for assets,
one combined free-disk-by-pool number per node.

Originally specced as `/blobs` (a generic-storage label) and
implemented under that path through P1.2.c; renamed to `/images` in
2026-05 after the operator review noted the page's vocabulary
(image cache, image tier, image-affinity scheduling) made the
generic name a friction point. The page is still the same
view — tier vocabulary is the live `ImageTier` set documented in
`docs/deployment/images.md "Tier vocabulary"`.

- Per-node row: cached image count, free disk, in-use / soon /
  recent / cold tier counts, evicted-in-last-24h count.
- Click a node → drill-down to per-image table: image ref, size,
  tier, last-used, in-use refcount, score (per spec 15's eviction
  formula), pinned-flag.
- Active warmup directives shown in a separate panel: progress
  (pulled / pending / failed), deadline, originating trainer.
- Operator actions (phase 1+, behind auth): pin / unpin a template's
  image, evict a specific image immediately, cancel a warmup
  directive.

### Health (`/health`)

Surface flags an operator wants to see in 5 seconds:

- **Under-utilized nodes**: `current_concurrent < 0.3 *
  estimated_max` for >15 minutes. Suggests removing the node or
  shifting workload.
- **Saturated nodes**: any resource at >90% peak in last hour.
  Suggests adding capacity.
- **Long-running & queued sessions**: sandboxes / raw rollouts alive
  longer than 2× the default hard deadline. An age heuristic for
  triage, not a failure signal — long-horizon rollouts and persistent
  substrate containers legitimately exceed it, and an `acquiring` raw
  rollout is queued for capacity (admission backpressure), not hung. A
  `state` column distinguishes the cases; the section does not flip the
  health banner.
- **Heartbeat-late nodes**: heartbeat age > 10 s.
- **Rollout failure rate**: per-template `failed / started` over
  last 30 min, threshold-flagged. A `capacity_rejected` raw rollout —
  the scheduler declined to place the acquire within `queue_timeout_s`
  (the pool was at capacity the whole wait) — is **not** a failure: the
  work never ran and the consumer typically retries. It carries its own
  terminal status (a sibling of `reaped`; both distinct from `failed`),
  is excluded from this rate and from the overview's failed tile, and
  surfaces on a separate "capacity-paced" tile + `/users` `paced` column
  so pacing volume is visible without inflating the failure signal. Its
  `error`/reason field states the two operator levers: raise
  `queue_timeout_s` to wait longer for a slot, or have the caller retry.

## Data sources

- `NodeRegistry` (live) — node table, heartbeat ages, hw probe.
- `StateStore` (spec 20 schema) — sandboxes, rollouts, events,
  pending_rollouts, audit, sessions (phase 3).
- `CapacityEstimator.matrix()` — capacity grid; binding-constraint
  values include `disk:image_cache`, `disk:sandbox_writable`,
  `disk:asset_cache`, `disk:session_state` per spec 10's multi-pool
  disk accounting.
- **Resource samples ring buffer** — phase 0, in-process, 60 min at
  5-second resolution per node. Populated from heartbeats.
- Trajectory store — viewer fetches via spec 21
  `FetchTrajectoryCommand`; cache lives at
  `~/.xrlenv/admin-cache/trajectories/` (spec 20 path defaults).

No external Prometheus / Grafana / Loki required.

## Refresh strategy

- **Phase 0**: live-updating via SSE (Server-Sent Events) from the
  control plane to the browser. The control plane publishes
  state-change events (sandbox created/destroyed, rollout
  finished/failed, heartbeat updates, resource samples) and the
  browser updates the relevant DOM fragments via HTMX's SSE
  extension. No WebSocket bidirectionality needed since phase 0 is
  read-only.
- **Phase 1**: 24-hour metric history backed by a small embedded
  tsdb (rolling on disk per node) or scraped from an external
  Prometheus when one is available; under-utilization alerts persisted
  in the events log.

## Auth

Defers to spec 19's identity table:

- **Phase 0**: no auth on localhost-only bind; spec-19 guard
  rejects public binds without auth. Audit events fire on every
  page view of admin actions (none in phase 0; phase 1 onward).
- **Phase 1**: HTTP basic auth using the operator-token identity
  from spec 19 (`operator.admin` scope); rate-limit failed
  logins; audit log entry per page view.
- **Phase 2**: OIDC; RBAC scopes drive which actions and tabs
  render; multi-tenant filters consume `owner_id` / `project_id`.

## Phase 0 actions

None. Read-only. The CLI (`xrlenv kill <sandbox>`, `xrlenv drain
<node>`) covers operator actions; the panel does not duplicate them.

## Phase 1 actions

- Kill a sandbox.
- Drain a node (mark unschedulable, wait for current sandboxes to
  finish, signal the user when safe to remove).
- Force GC.
- Adjust template capacity overrides (the same overrides exposed via
  the CLI in phase 0).
- Manage port-forwards (spec 07): list active forwards, revoke,
  show TTL countdown.

## Additional views in later phases

- **`/forwards`** (phase 1): active port-forwards, target
  sandbox, internal port, bind type, TTL, requesting identity.
  Revoke action.
- **`/sessions`** (phase 3, with spec 18): attached / detached
  sessions, log lengths, snapshot ages, hot-tier size, reattach
  button (operator-side debug only).
- **`/audit`** (phase 1, with spec 19): structured audit events
  with filter; default sort = recent. Retention is the
  audit-specific value pinned in spec 20 (90 days default,
  configurable; **not** subject to the shorter generic events
  retention). Phase 2 ships a signed archive option per spec 19
  for compliance-grade retention beyond the configured window.
- **`/images`** (phase 1; briefly named `/blobs` during P1.2.c —
  see "Images" above): per-node image cache + asset cache view
  (spec 15).

## Non-goal

This is **not** an alerting system. It does not page anyone. The
"Health" view is for "I'm at my desk, what's going on?", not for
3 a.m. on-call. A user who needs paging should scrape the `/metrics`
endpoint into their own alertmanager.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: all 7 views, **live-updating via SSE**, read-only,
  60-min ring buffer. Auth posture per spec 19: localhost is
  unauthenticated; public bind requires `--admin-allow-public` and
  basic auth (no no-auth public bind allowed).
- **Phase 1**: basic auth on every bind; 24-hour history; actions
  (kill sandbox, drain node, force GC, manage port-forwards);
  under-utilization alerts persisted in events log; `/images` view
  for images + assets; `/forwards` and `/audit` views.
- **Phase 2**: OIDC; full audit-log surface; richer actions;
  multi-tenant filters for shared clusters.
- **Phase 3**: `/sessions` view (with spec 18).
