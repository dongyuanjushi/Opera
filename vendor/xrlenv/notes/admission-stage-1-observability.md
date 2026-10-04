# Stage 1 — node health signals + admin "Cluster health" rework

Slice plan for Stage 1 of `notes/admission-capacity-design.md`. Stage 1 is
**observability only**: compute the per-node health signals P1 will later
control on, plumb them to the control plane, and rebuild the admin health
page to render them. No controller, no wire-protocol change, no SDK change.

## Goal

After Stage 1: for every connected node the control plane continuously knows
its `docker run` p95 latency, docker error/timeout rate, create-gate
contention, disk headroom, and heartbeat age — and the admin **Cluster
health** page shows them in one per-node table. This is the instrument panel
P1 (Stage 3) will read; it is also immediately useful to a human operator.

## Scope

**In:** node-side signal collection; a `Heartbeat` proto extension; the
control-side receive + StateStore mirror; the `health.html` + `_health_blocking`
rework.

**Out (later stages):** the AIMD controller (Stage 3 — Stage 1 only *shows*
signals, it doesn't act on them); separating queue/run clocks (Stage 2);
the streaming `AcquireContainer` RPC + live SDK queue output (Stage 2); the
"current adaptive limit / last contraction" panel column (Stage 3 — there is
no limit yet).

## Decisions within Stage 1

- **D1 — measure node-side.** The `docker run` call is node-side
  (`raw_container.py`); time it there. Computing p95 needs a sample window;
  the node keeps a small rolling ring buffer and computes percentiles
  locally — the heartbeat then carries finished numbers, not raw samples.
- **D2 — latency is create-specific; error/timeout counts span all docker
  ops.** Create-call latency is the *smooth* saturation signal, so measure it
  on the create path only (exec/destroy latency is noisier). Docker
  error/timeout *counts* are the *emergency* signal — increment them at the
  single existing chokepoint, `_translate_docker_error`, so they cover
  create + destroy + archive uniformly.
- **D3 — mirror to a `nodes.health_json` TEXT column.** Health stats refresh
  every heartbeat (~5s) on the same row as `last_seen_at`. One nullable JSON
  column avoids SQLite migrations when Stage 3 adds AIMD fields, and the
  panel only ever reads it per-node (no aggregation query). The admin panel
  is a separate process reading `state.db`, so the signals *must* land in
  SQLite, not just the in-memory registry.

## Deliverables

### 1a — node-side health collector

New `NodeHealthCollector` (suggested: `xrlenv/node/health.py`), held by the
node agent, shared with `RawContainerManager`.

- A rolling window (ring buffer, e.g. last 120 s) of `docker run` durations.
- `record_create(duration_s)` — called from `raw_container.py.acquire`
  around the `containers.run` `to_thread` call (inside the `_create_gate`,
  timing the call itself).
- `record_docker_error(*, is_timeout: bool)` — called from
  `_translate_docker_error` (the single chokepoint), so every node-side
  docker failure is counted, timeout vs other.
- `snapshot() -> NodeHealthStats` — p50/p95 of the window, create count,
  error count, timeout count over the window, plus live create-gate
  in-use + waiter count (read from the `_create_semaphore`).

Files: new `node/health.py`; `node/raw_container.py` (instrument
`containers.run`, `_translate_docker_error`, expose `_create_semaphore`
depth); `node/agent.py` (own the collector, hand it to the manager).

### 1b — heartbeat carries the signals

Extend the `Heartbeat` proto with a nested health message:

```
message NodeHealthStats {
  uint32 window_s             = 1;
  double create_p50_ms        = 2;
  double create_p95_ms        = 3;
  uint32 create_count         = 4;   // completed creates in the window
  uint32 docker_error_count   = 5;   // all docker ops, in the window
  uint32 docker_timeout_count = 6;   // subset of error_count
  uint32 create_inflight      = 7;   // create-gate permits in use
  uint32 create_queued        = 8;   // acquires waiting on the create gate
}
message Heartbeat { ... existing fields ... ; NodeHealthStats health = 7; }
```

Regenerate `_pb2` via `scripts/gen_protos.sh`.

- `node/grpc_link.py` `_heartbeat_loop` — call `collector.snapshot()` and
  attach `health` to the `Heartbeat`. (Cheap, in-memory; no daemon round
  trip, so it doesn't reintroduce the #18 heartbeat coupling.)
- `control/grpc_endpoint.py` — the heartbeat handler (`touch()` / the reader
  loop) stashes the latest `NodeHealthStats` on the `RemoteNodeTransport`
  and mirrors it to the StateStore.
- `control/state.py` — `nodes` schema gains `health_json TEXT`; a
  `update_node_health(node_id, health_json)` method (or fold into the
  existing heartbeat mirror); `NodeRecord` gains an optional `health` field.

Files: `api/proto/node_control.proto` + regen; `node/grpc_link.py`;
`control/grpc_endpoint.py`; `control/state.py`.

### 1c — rebuild the admin "Cluster health" page

Rework `_health_blocking` + `health.html`:

- **New: per-node health table** — one row per connected node:
  running containers · `docker run` p95 (ms) · docker error / timeout count
  (window) · create-gate in-use / queued · disk free · heartbeat age.
  Colour a row when a signal crosses a coarse threshold (p95 high, errors
  non-zero, disk low, heartbeat stale).
- **Delete the "Heartbeat-late nodes" placeholder** — heartbeat age is now a
  real column above; the placeholder section goes.
- **Reframe "Under-utilized nodes"** — keep utilisation, but show it as
  running-vs-capacity in the per-node table (both under- and over-use are
  visible there); drop the standalone under-use-only section.
- **Make the triage sections raw-container-aware** — "stuck sandboxes" and
  "failure rate per template" are case-1-only and read empty for raw
  workloads; either extend them to cover `raw_rollouts` or drop them. (Pick
  during implementation; covering raw is preferred.)

Files: `admin/server.py` (`_health_blocking`, `_HEALTH_CHECK_COUNT`);
`admin/templates/health.html`; `admin/__init__.py` (the page's cell-id set).

## Tests

- `node/health.py` — ring-buffer percentiles; window eviction; counter
  semantics. (qa-test-engineer.)
- `raw_container.py` — a create records a duration; a docker error
  increments the right counter.
- `grpc_link` / `grpc_endpoint` — a heartbeat round-trips `NodeHealthStats`;
  the control side mirrors it to `nodes.health_json`.
- `admin` — `_health_blocking` renders the per-node table from
  `health_json`; raw-only cluster is not blank; no placeholder section.

## Acceptance

- On a live (or smoke) cluster the admin **Cluster health** page shows a
  populated per-node table with non-zero `docker run` p95 while a workload
  runs; nothing reads as a phase-1 placeholder; a raw-container-only run
  produces a non-empty page.
- `gen_protos.sh` clean; full unit suite + `ruff` green.

## Suggested commit breakdown

1. `node/health.py` collector + `raw_container.py` instrumentation.
2. proto extension + regen + node sends + control mirrors to StateStore.
3. admin "Cluster health" rework.
