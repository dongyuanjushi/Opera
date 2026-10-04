# Design: per-node per-runtime concurrency cap (sysbox-fs wedge prevention)

**Status:** APPROVED (2026-07-08) — decisions locked, ready to implement.

## Problem

The scheduler gates placement on CPU/mem (capacity estimator), per-node AIMD
health limits, and disk pressure. The node also serializes sysbox *creates*
(`sysbox_create_concurrency=1`). **Nothing bounds the number of concurrently
*running* sysbox containers on a node.**

During the 2026-07-07 TerminalWorld sweep (`--max-workers 32`, 22 sysbox tasks),
up to ~22 sysbox containers ran at once against one sysbox-fs FUSE daemon and
overwhelmed it: FUSE requests were dropped, container processes blocked in
uninterruptible **D-state** on `fuse_flush` / `request_wait_answer`, two
containers became un-removable orphans, and the sweep hung on teardown. Kernel
stacks confirmed the mechanism. Reboot-free recovery = abort the wedged FUSE
connections; but the real fix is to **never enter the wedge regime**. The
investigation batch stayed healthy at concurrency 4; the wedge appeared around
20+.

The create-cap serializes the *create burst*; it does not bound *steady-state*
concurrent sysbox containers. The scheduler is the only component that controls
how many sysbox containers land on a node.

## Decisions (locked)

1. **General per-runtime cap, not sysbox-specific.** A per-node map
   `max_concurrent_by_runtime: {runtime: N}` (e.g. `{"sysbox-runc": 8}`).
   Default empty ⇒ unlimited ⇒ **current behavior byte-for-byte** for every
   non-capped runtime (runc, None, and any runtime without an entry).
   Mechanism-not-policy: core ships the primitive; operators set values.
2. **Configured per-node in `nodes.yaml`.** New `NodeEntry` field
   `max_concurrent_by_runtime`. Operators tune per box (a 192-core node and a
   small node need different caps). The CP loads it and hands a
   `{node_id: {runtime: cap}}` map to the `Scheduler`.
3. **Starting value: `sysbox-runc: 8`** on the sysbox node(s). Wedge was ~22;
   conc-4 creates validated healthy; 8 running leaves clear headroom. Tunable.

## Mechanism

A new placement gate in `Scheduler._place_impl`, mirroring the existing AIMD and
disk gates and running **inside the `_pending_lock`** so concurrent `place()`
calls see a consistent count:

- Count, per node, the running sysbox sessions (from the raw-session load
  provider) **plus** in-flight `_pending` reservations of the requested runtime.
- Exclude any node where `count >= cap` for the requested runtime.
- If the exclusion empties an otherwise-non-empty candidate pool → raise
  `CapacityExhausted`. The **admission queue holds** the request (no failure —
  the same overflow path as capacity / AIMD / disk) and a node-confirmed sysbox
  destroy **kicks** the queue → the request lands when a slot frees.

The gate is a no-op when `container_runtime` is unset, or the node has no cap
for that runtime — so runc / managed-sandbox placement is unchanged.

Counting both **running + pending** is essential: without the pending term, a
burst of concurrent `place()` calls would all pass the gate before any becomes a
running session, over-placing past the cap (the same race the `_pending`
mechanism already solves for CPU/mem).

## Blast radius

- `xrlenv/control/nodes_yaml.py` — `NodeEntry.max_concurrent_by_runtime:
  dict[str, int] = {}`.
- `xrlenv/control/scheduler.py`:
  - `_PendingPlacement` + `RawSessionLoad` gain `container_runtime: str | None`.
  - `Scheduler.__init__` gains `runtime_caps: dict[str, dict[str, int]] | None`
    (node_id → runtime → cap).
  - `place` / `_place_impl` stamp `container_runtime` onto `_PendingPlacement`.
  - new `_runtime_count_with_pending(runtime)` (raw sessions + pending).
  - the cap gate in `_place_impl` after the AIMD gate.
- `xrlenv/control/raw_container_service.py`:
  - `RawContainerSession` gains `container_runtime`, stamped at acquire.
  - `iter_load_entries` emits `RawSessionLoad(container_runtime=…)`.
- `xrlenv/control/distributed_runtime.py` — build the `{node_id: {runtime: cap}}`
  map from the loaded `nodes.yaml` and pass it to the `Scheduler`.
- `nodes.yaml` — set `max_concurrent_by_runtime: {sysbox-runc: 8}` on the sysbox
  node(s).
- No node-side / wire / proto change.

## Tests (required)

1. **At-cap excludes the node.** Node with `sysbox-runc: 2` holding 2 running
   sysbox sessions → a 3rd sysbox `place()` skips it (or `CapacityExhausted`
   when it's the only capable node).
2. **Pending counts toward the cap.** Two back-to-back `place()` calls (no
   session registered yet) exhaust a cap of 2 via `_pending` alone — the 3rd is
   refused. Guards the over-place race.
3. **Overflow queues then drains.** At-cap → admission holds → a sysbox destroy
   frees a slot + kicks → the queued acquire lands. (End-to-end via the
   admission queue.)
4. **Non-sysbox unaffected.** runc / None placement, and a runtime with no cap,
   are byte-for-byte unchanged; a runc container never counts against a
   sysbox-runc cap.
5. **Multi-node spread.** Cap N per node, M capable nodes → up to N×M sysbox
   containers spread across them before overflow.
6. **nodes.yaml plumb-through.** `max_concurrent_by_runtime` parses and reaches
   the scheduler as the per-node cap map.
