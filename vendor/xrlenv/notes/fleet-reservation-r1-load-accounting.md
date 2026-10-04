# Fleet reservation — R1: the load-entry invariant (accounting note)

Resolves risk **R1** in `notes/evoclaw-fleet-reservation-to-do.md` — the single
correctness crux of fleet reservation. Generic; no consumer specifics. Spec:
`specs/10` §Fleet reservation accounting, `specs/03` §Fleet reservation.

Review this before any code. The whole decision reduces to *one function*.

---

## The key realization: it's a list transformation, not a math change

Raw-container load reaches the scheduler through exactly one seam:
`RawContainerCoordinator.iter_load_entries()` → `Scheduler.set_raw_session_provider`
→ folded into `_gather_cluster_load()` → summed by `capacity._running_cpu` /
`fits()`. Today (`raw_container_service.py`):

```python
def iter_load_entries(self) -> list[RawSessionLoad]:
    return [
        RawSessionLoad(
            node_id=s.node_id,
            template_name=f"raw-container/{s.image}",
            effective_resources=s.effective_resources,   # this session's own cpu/mem/disk
            task_key=s.task_key,
        )
        for s in self._sessions.values()                 # ONE entry per session
    ]
```

`_gather_cluster_load` appends each entry's `effective_resources` to the node's
running list; `_running_cpu` sums `cpu_request` over that list. **Neither the
gather nor the sum knows or cares what an entry represents.** So the fleet change
is purely: *emit a different list of entries.* `_gather_cluster_load`,
`_running_cpu`, `_cpu_cap`, `_mem_cap`, `fits()` — **unchanged**.

Fleet-aware version:

```python
def iter_load_entries(self) -> list[RawSessionLoad]:
    entries = []
    # (1) non-fleet sessions — one entry each, byte-for-byte as today
    for s in self._sessions.values():
        if s.fleet_id is None:
            entries.append(RawSessionLoad(
                node_id=s.node_id,
                template_name=f"raw-container/{s.image}",
                effective_resources=s.effective_resources,
                task_key=s.task_key,
            ))
        # else: a fleet member — contributes NOTHING here (covered by its fleet)
    # (2) open fleets — exactly one footprint entry each
    for f in self._fleets.values():                       # FleetReservation table
        entries.append(RawSessionLoad(
            node_id=f.node_id,
            template_name=f"raw-fleet/{f.fleet_id}",
            effective_resources=f.footprint,               # the DECLARED peak, once
            task_key=f.task_key,
        ))
    return entries
```

That function *is* the R1 decision. Everything below is the invariant it must
uphold and the edges it must get right.

---

## The invariant (exact)

For every node, on every placement decision:

1. **One open fleet ⇒ exactly one footprint load entry.** `iter_load_entries`
   yields one `RawSessionLoad(effective_resources = fleet.footprint)` per entry
   in `self._fleets`, keyed by `fleet_id`. Never zero (while open), never more
   than one.
2. **Members contribute no per-container scheduler load.** A session with
   `fleet_id is not None` is *skipped* in the non-fleet loop and never emitted.
   Its own `effective_resources` never enters the scheduler sum — the fleet's
   footprint stands in for it. (No double-count: node load for a fleet = footprint,
   not footprint + Σ members, and not Σ members.)
3. **Companion budget checks use the members' own requested resources** — but on
   a *different* read path (below), never via the load entries.
4. **No-fleet path is byte-for-byte today.** A session with `fleet_id is None`
   takes loop (1), which is the current code verbatim. `self._fleets` empty ⇒
   `iter_load_entries` returns the identical list it returns today.

The single source of truth for "is this container part of a fleet" is
`session.fleet_id`; for "what does the fleet reserve" is
`FleetReservation.footprint`. Nowhere else.

---

## Point 3 in detail — the companion budget check is a separate path

`FleetOverBudget` is **not** an accounting-sum concern; it is a per-fleet
admission check in the coordinator's companion branch (§2 of the slice), reading
the members' **own** `effective_resources` (stored on their session rows), not
the load entries:

```python
used_cpu = sum(m.effective_resources.cpu_request for m in fleet.members)  # members' OWN
used_mem = sum(m.effective_resources.mem_request for m in fleet.members)
if used_cpu + new.effective_resources.cpu_request > fleet.footprint.cpu_request \
   or used_mem + new.effective_resources.mem_request > fleet.footprint.mem_request:
    raise FleetOverBudget(...)                              # before any node command
```

So two reads of two different things, deliberately not conflated:
- **Load accounting** (points 1–2) reads `fleet.footprint` — what other placements
  must route around.
- **Budget check** (point 3) reads the members' `effective_resources` — whether
  *this* fleet is honoring its own declaration.

This is why the footprint can be an over-declaration (safe: it reserves more than
members use) and the budget check still catches an under-declaration (members try
to exceed it).

---

## State shape (minimal)

- **Per session**: add `fleet_id: str | None` (default `None`) to the raw-container
  session row — set from the `xrlenv.fleet_id` label at acquire.
- **Per fleet**: the `FleetReservation` table (slice §1), keyed by `fleet_id`:
  `{node_id, footprint: ResourceSpec, members: set[session_id], opened_ts,
  last_acquire_ts, task_key}`. `iter_load_entries` loop (2) walks this table.
- **Fairness falls out for free**: `_gather_cluster_load` increments
  `per_node_task_count` per emitted entry whose `task_key` matches. The fleet
  emits **one** entry ⇒ counts as **one** toward `max_runs_per_task`; members
  emit nothing ⇒ add nothing. (Set `RawSessionLoad.task_key` on the fleet entry
  from the reservation's `task_key`; members' `task_key` never reaches the sum.)

---

## The atomic open-fleet handoff (exact order + the one scheduler change)

The handoff crosses an ownership boundary: `_pending` + `_pending_lock` are
private to `Scheduler.place()`; the `FleetReservation` table lives in the
coordinator; and `iter_load_entries` (coordinator) + `_pending` (scheduler) are
the two load sources `_load_with_pending` folds together. The footprint must be
covered by **at least one** of them at every instant — a *gap* is an under-count
(a second placement over-places into reserved capacity); a brief *overlap* is a
safe over-count (conservative). This is the **same discipline the single-container
path already uses**: `place()` reserves in `_pending`, the session registers (so
`_gather_cluster_load` covers it), then `commit_placement` drops `_pending` —
commit strictly *after* register, so there's never a gap.

**The one scheduler API change.** `place()` today reserves the *container's own*
resources in `_pending`. A fleet-opening placement must reserve the **footprint**.
Add an explicit reserve amount so it happens inside the existing lock — **no new
cross-object transaction is needed**:

```python
# scheduler.place(...) — one added kwarg
def place(self, manifest, *, task_key=None, backend=None, image_present=None,
          preferred_home_node=None,
          reserve: ResourceSpec | None = None) -> Placement:
    #   reserve is None      -> today's behavior (reserve manifest resources)
    #   reserve is footprint -> gate + put the FOOTPRINT into _pending, under
    #                           _pending_lock, keyed by placement.reservation_id
```

(Equivalently a thin `place_fleet(footprint, ...)` wrapper — same body. Either
way the footprint enters `_pending` atomically under `_pending_lock`.)

**Exact order (fleet-opening acquire), each step annotated with what covers the
footprint:**

1. `place(..., reserve=footprint)` → gate the whole footprint on one node, reserve
   it in `_pending[reservation_id]`. — *covered by `_pending`.*
2. Coordinator creates the `FleetReservation` (node from the placement, footprint,
   empty members). `iter_load_entries` now emits the fleet entry. — *covered by
   BOTH `_pending` and the fleet entry: a brief, safe over-count.*
3. `commit_placement(placement)` → drop `_pending[reservation_id]`. — *covered by
   the fleet entry alone.*
4. Acquire the lead container; register its session with `fleet_id` set — a
   **suppressed member** (loop 1 skips it), adds no charge.

**Safety-critical rule:** step 3 (`commit`) must run strictly **after** step 2
(`FleetReservation` created). Committing before the reservation exists opens an
under-count gap. This is the fleet analogue of the existing "commit only after the
sandbox is in `list_sandboxes()`" rule.

**Failure / crash paths:**
- Reservation creation (step 2) fails → `release_placement(placement)` drops
  `_pending`; no fleet; consumer gets the error (mirrors the existing acquire
  try/commit/except-release).
- Crash between steps 2 and 3: `_pending` is **in-memory only**, so a CP process
  restart simply drops it — there is nothing to leak or reconcile on the
  `_pending` side. The real recovery concern is the opposite: a `FleetReservation`
  already created in step 2 (and its live member containers) must be **rebuilt**
  after restart, or the footprint goes un-accounted while the containers keep
  running on the node. That rebuild is the reconcile path (R7 / slice §5): read
  the surviving containers' `xrlenv.fleet_id` labels via
  `list_raw_containers`/node reconcile, plus the persisted reservation rows in the
  StateStore, and reconstruct `self._fleets` — the same discipline `NodeRegistry`
  uses to reconcile sandboxes that outlived a control-plane restart. The
  reconcile-on-reconnect closes the window.
- A **companion** acquire never touches `_pending` for load — it's already inside
  the footprint the reservation holds; it only does the budget check + registers a
  suppressed member.

## Last-member release

When `fleet.members` empties on node-confirmed destroy, drop the
`FleetReservation` (slice §5). Loop (2) stops emitting it ⇒ the footprint returns
to free capacity in one step, atomically with the reservation delete
(invariant 2).
## Disk: the MVP decision (explicit)

**Decision: disk is NOT part of the fleet footprint in MVP — it stays
per-container, including for fleet members. The footprint reserves cpu + mem
only.** Rationale: the fleet-starvation problem is cpu (eval-slot exhaustion),
not disk; and forcing consumers to declare a disk footprint up front is fragile
(build / `node_modules` disk is hard to predict, and under-declaration would
raise spurious `FleetOverBudget` on disk).

The trap flagged in review — a `fleet_disk_request` that **defaults to 0** while
members are fully suppressed — is exactly what we must *not* do: it would make
members' real disk **invisible** (a silent disk under-count). We reject it. The
invariant instead gets **one explicit, documented exception**:

> A fleet member is suppressed for **cpu and mem** (the footprint covers those)
> but **still contributes its own `disk_request`** to scheduler load, exactly like
> a non-fleet container.

Concretely in `iter_load_entries` loop (1): a fleet member is *not* fully skipped
— it emits a **disk-only** entry,
`RawSessionLoad(effective_resources=ResourceSpec(cpu_request=0, mem_request=0,
disk_request=member.disk_request))`; the fleet's footprint entry (loop 2) carries
`disk_request=0`. `_disk_cap_remaining` (which sums `disk_request` from the
`sandbox_writable` pool) then sees each member's real disk, while cpu/mem come
from the footprint — no under-count on any axis, no silent zero. mem is
footprint-covered exactly like cpu; only disk is the exception.

**Spec 10 must state this explicitly** (follow-up edit, below): footprint = cpu +
mem; disk stays per-container for fleet members; disk-in-footprint is a phase-2
refinement for when consumers can reliably declare it. There is no default-0
fleet disk.

**v1 adds no `fleet_disk_request` field of any kind.** The footprint is parsed
from exactly two labels — `xrlenv.fleet_cpu_request` and `xrlenv.fleet_mem_request`
— and `FleetReservation.footprint` is a `ResourceSpec(cpu_request, mem_request,
disk_request=0)` where the `disk_request=0` is structural (ResourceSpec has the
field) and never read on the fleet path. There is no `xrlenv.fleet_disk_request`
label, no fleet disk parameter on any API, and no fleet disk term in accounting.
Disk reaches the scheduler **only** through per-member disk-only entries (above).
Adding fleet disk is a deliberate phase-2 decision, not an omission to backfill.

---

## Why this is the minimal correct change

- **Untouched**: `_gather_cluster_load`, `_running_cpu`, `_cpu_cap`, `_mem_cap`,
  `_disk_cap_remaining`, `fits()`, the `capacity` API, the whole StateStore sandbox
  load path. They sum a list; the list is just shaped differently.
- **New**: `session.fleet_id`, the `FleetReservation` table, the two-loop
  `iter_load_entries`, the companion budget check, and the reservation
  create/release lifecycle.
- **Default guard**: every new branch is gated on `fleet_id is not None`; the
  `fleet_id is None` path is the current code, unmodified.

---

## Test hooks (generic — synthetic fleets, no consumer fixtures)

Assert the invariant directly on the seam, so it can't regress silently:
- `iter_load_entries` with one open fleet (footprint `{cpu:18}`) + two member
  sessions (`cpu 2`, `cpu 16`) sharing its `fleet_id` returns **exactly one**
  entry, `effective_resources.cpu_request == 18` — not three, not 36.
- Same fleet ⇒ `_gather_cluster_load[node]` shows `cpu` used `== 18` (not 2+16+18,
  not 2+16), and `task_count == 1` (not 2).
- Mixed: one open fleet + one plain non-fleet session (`cpu 4`) ⇒ two entries,
  sum `22`.
- `self._fleets` empty ⇒ `iter_load_entries` output is **identical** to the pre-
  change function for the same sessions (golden equality test — the point-4
  guarantee).
- Companion budget: a third member (`cpu 4`) against footprint `{cpu:18}` with
  members already `2+16` ⇒ `FleetOverBudget`, and `iter_load_entries` still shows
  `18` (reservation unchanged, no partial charge).
- Release: destroy the last member ⇒ the fleet entry disappears, node `cpu` used
  returns to its non-fleet baseline.
- **Disk exception**: an open fleet (footprint `{cpu:18, mem:.., disk:0}`) with a
  member declaring `disk_request=D` ⇒ node `cpu`/`mem` used come from the footprint
  (member suppressed), but node **disk** used `== D` (the member's own disk still
  counts). Asserts the "no silent disk under-count" rule: cpu/mem footprint-covered,
  disk per-member.
- **Atomic handoff (no gap)**: between `place(reserve=footprint)` and
  `FleetReservation` creation, `_load_with_pending` shows the footprint via
  `_pending`; after creation-and-before-commit it shows it via *both* (over-count,
  fine); after commit, via the fleet entry alone — and at **no** point is it
  missing (a test that a second `place()` for a full node is refused throughout).
