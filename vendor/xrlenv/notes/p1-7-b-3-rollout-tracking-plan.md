# P1.7.B.3 — persistent raw-rollout tracking + admin visibility

**Status:** SHIPPED 2026-05-07 in commit `5d8e7e5`; audit response (Admin-M1 + Docs-M1) shipped in the follow-up commit. Closes the "all past runs are never tracked on the rollouts page" gap for case-2/3 raw-container traffic.

**Source conversation:** 2026-05-07 design discussion. Locked five decisions (D1-D5 below) before any code landed.

**Shipped-vs-plan deltas (audit 2026-05-07 P1.7.B.3-Docs-M1):**
- **API name `rollout_metadata`, not `rollout_labels`.** Earlier draft sketches used `rollout_labels(...)` with a free-form dict. The shipped API is `xrlenv.rollout_metadata(*, artifact_path=None, displayed_name=None)` — typed kwargs, two recognized fields. A few residual `rollout_labels` references in lower sections are obsolete; treat the typed-kwargs API in §10 as authoritative.
- **ContextVar propagation through `ThreadPoolExecutor` is NOT automatic.** Earlier draft claimed Python 3.7+ workers inherit the parent context by default. They do not. The shipped smoke driver works around this by setting the contextvar **inside** the worker function (`_run_one_instance` wraps `run_instance(...)` with `with xrlenv.rollout_metadata(...):`), so cross-thread propagation is never relied upon. asyncio Tasks DO inherit context by default, so an asyncio-shaped harness would work either way.
- **Admin per-rollout artifact view lists immediate children only.** Earlier draft sketched per-file viewing (open report.json inline, etc.). Shipped admin renders the immediate-children directory listing and stops there — operator SSHes for deeper inspection. Keeps the attack surface minimal; per-file viewing is an additive follow-up if real demand surfaces.
- **`/rollouts` filter surface (Admin-M1):** initial ship had no raw-status filter; closed in audit response — `raw_status=acquiring|running|released|cancelled|failed` query param + dropdown above the raw-rollouts table. Case-1 status / template / since / pagination filters apply only to case-1 rows; raw rollouts use the `raw_status` filter and a fixed limit (no pagination yet — additive when raw rollout volume grows).

**References:** `notes/p1-7-b-2-image-affinity-plan.md` (the previous slice this builds on), CLAUDE.md spec-00 invariant 1 ("Sandbox identity ≠ rollout identity").

---

## 1. Goal (one paragraph)

Every raw-container acquire produces a durable record in the StateStore — surviving control-plane restart, queryable from admin, with the cluster's full lifecycle status (acquiring → running → released / cancelled / failed). The audience's docker-py drop-in harness keeps its one-line `xrlenv.from_env()` swap intact; per-instance metadata (`artifact_path`, `displayed_name`) flows from the smoke driver into the rollout record via the `xrlenv.rollout_metadata(...)` typed-kwargs context manager that the drop-in reads at `containers.create` time. Admin's existing `/rollouts` page surfaces raw rollouts alongside case-1 template-driven rollouts; the per-rollout detail page renders the metadata and, when the `artifact_path` is reachable on the control-plane host's filesystem, lists the immediate-children directory contents. **No artifact upload over the wire — the cluster only ever holds metadata and pointers.**

## 2. Decisions taken (locked in conversation)

| # | Decision | Why |
|---|---|---|
| D1 | **Status enum: `acquiring | running | released | cancelled | failed`.** "Acquiring" = container creation in flight (image pull / build / dispatch). "Running" = container alive + harness driving. "Released" = normal end-of-life (paired with `acquire`; symmetric verb pair). "Cancelled" / "failed" = abnormal exits. | "Released" is mechanism-faithful and avoids "destroyed" which reads alarming. |
| D2 | **Separate `RawRolloutRecord` table.** Case-1 `RolloutRecord` (steps, trajectory_uri, final_reward) and case-2/3 raw-container records (container_id, container_name, labels) have genuinely different shapes. Don't unify; don't add a `kind` column. | Case-1 is reserved for trainer-driven RL workloads (different audience pattern from evaluation). Forcing one schema warps both. |
| D3 | **Per-rollout metadata via typed-kwarg contextvar: `xrlenv.rollout_metadata(artifact_path=..., displayed_name=...)`.** Two recognized fields today — both optional, both string. Drop-in's `create_container` override reads the contextvar and emits docker labels with the `xrlenv.rollout.*` prefix; control plane parses them off `AcquireContainerCommand.labels` and writes to the typed columns on `RawRolloutRecord`. Future extension = add a kwarg + a column + parse one more key (linear, additive). | Per-instance scoping needs per-call context. The smoke driver sets the contextvar **inside the worker function** (so `ThreadPoolExecutor` cross-thread propagation is never relied on); asyncio Tasks inherit by default. Typed kwargs (vs free-form dict) keep the surface small + IDE-discoverable; no consumer-side accidental key dumping; xrlenv core stays minimal. |
| D4 | **Artifacts: pointer + best-effort local-FS read; never upload.** Smoke writes `xrlenv.rollout.artifact_path=<absolute path on consumer machine>` as a label. Admin tries to read that path; if it resolves on the control-plane host (laptop dev, shared NFS), it renders inline; if not, shows the path as a string for the operator to navigate to externally. | Cluster knows status from the wire; consumer owns artifact bytes. Upload is bandwidth + storage + security overhead xrlenv doesn't need. |
| D5 | **Smoke driver: copy + customize upstream's driver loop, not the harness.** Upstream's `run_evaluation.main()` / `run_instances()` / `run_evaluation_with_progress()` is **driver code** (instance loop, threadpool, progress bar) — fair game for the audience to mirror or replace. Upstream's `run_instance()` is the **harness** — we never modify it. Our smoke's per-instance worker wraps the call to `run_instance(...)` in `with xrlenv.rollout_metadata(...):`. | Driver code is the audience's territory by design; substituting it doesn't violate "don't reinvent the harness wheels." |

## 3. Architecture

```
[smoke driver — our code]
    ↓
    with xrlenv.rollout_metadata(artifact_path=..., displayed_name=iid):
        run_instance(test_spec, pred, ..., client=client)   # ← upstream, untouched
            ↓
            build_container(test_spec, client, ...)         # ← upstream, untouched
                ↓
                client.containers.create(image, name, ...)   # ← upstream, untouched
                    ↓
                    docker-py manager → client.api.create_container(image, name, ...)
                        ↓
                        [xrlenv drop-in override: xrlenv/compat/docker_client.py]
                            scoped_metadata = current_rollout_metadata()
                            scoped_labels = metadata_to_labels(scoped_metadata)
                            merged = {**(labels or {}), **scoped_labels}
                            ↓
                            AcquireContainerCommand(image=..., labels=merged)  ← spec-21 wire
                                ↓
                                [control plane]
                                    RawContainerCoordinator.acquire(...)
                                        - mints rollout_id (uuid)
                                        - StateStore.record_raw_rollout(
                                            rollout_id=..., status="acquiring",
                                            labels=merged, image=..., ...)
                                        - dispatches to chosen node
                                        - on ack: status → "running"
                                        - on destroy: status → "released"
```

## 4. Schema

```python
RawRolloutStatus = Literal[
    "acquiring",   # AcquireContainerCommand dispatched, awaiting node ack
    "running",     # container alive on node; harness driving
    "released",    # destroy_container succeeded; normal end
    "cancelled",   # operator-initiated cancel before normal end
    "failed",      # acquire-time error or mid-run failure
]

class RawRolloutRecord(BaseModel):
    rollout_id: str                     # cluster-minted uuid; primary key
    status: RawRolloutStatus
    image: str
    node_id: str | None = None          # None during early "acquiring" phase
    container_id: str | None = None     # None until node acks; volatile (future snapshot/resume)
    container_name: str | None = None
    artifact_path: str | None = None    # xrlenv.rollout.artifact_path label
    displayed_name: str | None = None   # xrlenv.rollout.displayed_name label
    created_at: float                   # acquire dispatch time
    finished_at: float | None = None    # set on released / cancelled / failed
    error: str | None = None            # populated when status == "failed"
```

**Two metadata columns today (`artifact_path`, `displayed_name`); add columns as new metadata kwargs land** — additive SQLite migration, no schema churn for existing rows.

**rollout_id ≠ container_id**: separate columns from day 1. Today's case-2/3 use is 1:1 (one acquire = one container = one release), but the schema doesn't constrain them — future snapshot/resume rotates `container_id` while `rollout_id` stays stable.

## 5. Scope

### In

| # | Item | Where |
|---|---|---|
| W1 | `RawRolloutRecord` model + status enum | `xrlenv/control/state.py` |
| W2 | StateStore methods: `record_raw_rollout`, `update_raw_rollout`, `list_raw_rollouts`, `get_raw_rollout`. SQLite + InMemory variants. New table in the SQLite schema (additive — case-1 unchanged). `update_raw_rollout(rollout_id, **fields)` takes a per-row partial update so callers can set status, container_id/name, finished_at, error in one shot rather than via a status-only setter. | `xrlenv/control/state.py` |
| W3 | Internal `_ROLLOUT_METADATA_VAR: ContextVar[_RolloutMetadata]` (frozen dataclass with `artifact_path: str \| None`, `displayed_name: str \| None`) + public `xrlenv.rollout_metadata(*, artifact_path=None, displayed_name=None)` context manager that pushes/pops the dataclass. | `xrlenv/compat/docker_client.py` (or new `xrlenv/compat/metadata.py` if cleaner) |
| W4 | Re-export `rollout_metadata` from `xrlenv/__init__.py`. | `xrlenv/__init__.py` |
| W5 | Drop-in `create_container` override reads the contextvar and merges `xrlenv.rollout.artifact_path=...` + `xrlenv.rollout.displayed_name=...` into outgoing docker labels (cluster mode only; local mode no-ops). Existing operator-passed labels via `containers.create(labels=...)` are preserved; xrlenv-reserved keys take precedence on conflict (logged at INFO). | `xrlenv/compat/docker_client.py` |
| W6 | `RawContainerCoordinator.acquire`: mint uuid → parse `xrlenv.rollout.artifact_path` / `xrlenv.rollout.displayed_name` off `AcquireContainerCommand.labels` → write record (`acquiring`) → dispatch → update on ack (`running`). `destroy`: update (`released`). Cancel/fail paths surface as `cancelled` / `failed`. | `xrlenv/control/raw_container_service.py` |
| W7 | Admin `/rollouts` page surfaces raw rollouts alongside case-1. The "Name" column shows `displayed_name` when set (or `rollout_id` short prefix as fallback); new `kind` column tags rows (`template:swebench-verified`, `raw:swebench/sweb.eval...`). Filterable by status (the new enum joins existing case-1 statuses in the dropdown). | `xrlenv/admin/server.py` + `xrlenv/admin/templates/rollouts.html` |
| W8 | Admin `/raw-rollouts/<rollout_id>` detail page for raw rollouts: status timeline, image, node_id, container_id/name, displayed_name, error (when `failed`). Artifact-path resolution: if `artifact_path` is set AND resolves on the control-plane host's filesystem, render the immediate-children directory listing (no recursion, no per-file viewer); otherwise show the path as a string with a "this path is on the consumer's machine" note. Per-file viewing is deferred — additive when real demand surfaces; SSH or shared-FS works for now. | `xrlenv/admin/server.py` + new `xrlenv/admin/templates/raw_rollout_detail.html` |
| W9 | Smoke driver: customize the per-instance loop to wrap `run_instance(...)` in `with xrlenv.rollout_metadata(artifact_path=..., displayed_name=instance_id):`. The `displayed_name=instance_id` default makes the admin's `/rollouts` row read "astropy__astropy-7166" instead of "r-7f2a09e1". | `examples/benchmarks-onboarding/swebench-verified/smoke.py` |
| W10 | Tests: state store CRUD round-trip; coordinator lifecycle transitions; contextvar set+restore semantics + per-thread isolation when each thread sets its own metadata (the smoke's actual pattern; cross-thread propagation is NOT relied on); admin renders raw rollout detail with reachable + unreachable artifact paths. | `tests/unit/...` |

### Out

| # | Item | Why |
|---|---|---|
| O1 | Artifact upload over the wire | D4 — bandwidth + storage + security overhead the cluster doesn't need. |
| O2 | Unifying RawRolloutRecord with RolloutRecord | D2 — different shapes, different audiences. |
| O3 | Free-form labels dict / generic JSON column on the record | D3 — only two recognized typed kwargs (`artifact_path`, `displayed_name`). Future kwargs land additively. Unknown kwargs to `rollout_metadata(...)` raise `TypeError` (Python's normal kwargs-validation path). |
| O4 | Trajectory step persistence for raw rollouts | Case-2/3 doesn't have a step-by-step trajectory; the harness owns its semantics. Status + labels + artifact pointer are sufficient. |
| O5 | Cross-control-plane artifact mirroring | Same as O1; never upload. |
| O6 | Modifying upstream `run_instance()` / `build_container()` | D5 — the harness stays untouched. Driver code is fair game; harness code isn't. |

## 6. Critical files

**Modify:**

- `xrlenv/control/state.py` — RawRolloutRecord + StateStore methods + SQLite schema migration (additive table).
- `xrlenv/compat/docker_client.py` — contextvar + `create_container` label merge.
- `xrlenv/__init__.py` — re-export `rollout_metadata`.
- `xrlenv/control/raw_container_service.py` — write/update records on acquire/destroy/cancel/fail paths.
- `xrlenv/admin/server.py` — `/rollouts` lists raw rollouts; `/rollouts/<id>` detail renders labels + artifact-path read.
- `xrlenv/admin/templates/rollouts.html` — `kind` column.
- `xrlenv/admin/templates/raw_rollout_detail.html` — new file.
- `examples/benchmarks-onboarding/swebench-verified/smoke.py` — wrap `run_instance(...)` call with `with xrlenv.rollout_metadata(...):`.

**Read-only / reference:**

- `tests/smoke/test_swebench_drop_in.py` — labels-passing pattern (if any) to mirror.
- `xrlenv/control/state.py` existing `RolloutRecord` schema — for stylistic consistency.

## 7. Verification

1. `.venv/bin/python -m pytest -q` — full suite stays green.
2. `.venv/bin/python -m mypy` — clean.
3. Unit: state store records + retrieves a raw rollout end-to-end (acquiring → running → released).
4. Unit: contextvar set+restore semantics + per-thread isolation — each ThreadPoolExecutor worker sets its own `rollout_metadata(...)` block and reads back its own values; concurrent workers don't interfere. The smoke's actual pattern; cross-thread propagation is NOT relied on (ThreadPoolExecutor workers don't inherit by default — see §8 Risks).
5. Local smoke (`--local`): pass `with xrlenv.rollout_metadata(...):` around a `run_instance` call against local Docker, assert the docker container has the merged labels via `docker inspect`.
6. Cluster smoke (against real GCP cluster): run `examples/benchmarks-onboarding/swebench-verified/smoke.py --save-artifacts`. Inspect admin `/rollouts` — case-1 list at the top, then a "Raw container rollouts" section below with 8 rows whose name column shows the instance_id (via `displayed_name`, e.g. `astropy__astropy-7166`), status `released`, image `swebench/sweb.eval...`. Click into one (`/raw-rollouts/<id>`) — see status / image / node / container / `artifact_path`. If the operator runs the admin on the same machine as the consumer, the Artifacts section lists the immediate children of the artifact directory (`report.json`, `run_instance.log`, etc.); otherwise see the path string + "consumer's machine" note.
7. Restart durability: prior `released` rollouts stay visible in `/rollouts` after the control plane restarts (covered by the `test_sqlite_persists_across_reopen` unit test). **Sweep-on-startup for stuck `acquiring`/`running` rows is NOT in this slice** — case-1's `sweep_stuck_transients` was not extended to raw rollouts in `5d8e7e5`; tracked as a deferred follow-up. Until that ships, `acquiring`/`running` rows whose coordinator-side session is gone (control-plane restart killed the in-memory map) stay in their pre-restart status until manually cleared.

## 8. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Contextvar doesn't propagate to worker thread | **Corrected post-ship:** ThreadPoolExecutor workers do NOT inherit the parent's contextvar by default. The shipped smoke driver sets the contextvar **inside** the worker function (`_run_one_instance` wraps `run_instance(...)` in `with xrlenv.rollout_metadata(...):`), so cross-thread propagation is never relied on. asyncio's `Task` does inherit context (copy-per-task semantics); event-loop harnesses can set the contextvar in the awaiting coroutine and the drop-in's `create_container` (called from a downstream awaited call) sees it. Multiprocessing-with-spawn doesn't inherit; consumer sets the contextvar inside each child. |
| Label dict gets too large (consumer dumps free-form data) | Cap at the proto level — Docker's label limit is ~64KB total per container; cluster-side cap at 100 labels × 256 char value = ~25KB; reject oversize at `create_container` with a clear error. |
| Artifact-path is a privileged path the operator shouldn't read from admin | Path read is `os.access(path, os.R_OK)` first; permissions-denied paths show "this path requires elevated permissions" instead of opening the file. No traversal beyond the declared root. |
| Restart mid-batch leaves rows in `acquiring`/`running` forever | **Deferred — not shipped in `5d8e7e5`.** Case-1 has `sweep_stuck_transients` for in-flight transient rows; extending it to raw rollouts (force-mark `acquiring`/`running` rows whose coordinator session is gone → `cancelled` with reason) is a small additive change tracked as a follow-up. Practical impact today: the operator sees stale `running` rows post-restart for sessions that won't recover. Mitigation while deferred: case-2/3 evaluations are short-lived (minutes) and harnesses retry / re-acquire on connection error, so the stuck-row window is small in practice. |
| Schema migration on existing SQLite DB | Additive table only (no alter on existing tables); SQLite auto-creates on first access. Test against a fresh DB and a populated case-1 DB. |

## 9. Estimate

~3-4 hours total:

- W1+W2 schema + StateStore methods: 45 min
- W3+W4+W5 contextvar + drop-in merge + re-export: 30 min
- W6 coordinator lifecycle: 30 min
- W7+W8 admin views: 60-90 min
- W9 smoke driver wrap: 15 min
- W10 tests: 30-45 min

## 10. Metadata API surface (locked)

**Two recognized fields today**, both optional, both `str | None`. Typed-kwargs API; future fields land additively as kwargs + columns:

```python
# Public API
import contextlib

@contextlib.contextmanager
def rollout_metadata(
    *,
    artifact_path: str | None = None,
    displayed_name: str | None = None,
) -> Iterator[None]:
    """Set per-rollout metadata for the duration of the block.

    Reads on every ``client.containers.create(...)`` inside the
    block via the docker-py drop-in's cluster-mode override; the
    drop-in emits the corresponding ``xrlenv.rollout.*`` docker
    labels which the control plane's RawContainerCoordinator
    parses + persists onto the RawRolloutRecord's typed columns.

    Args:
        artifact_path: Absolute filesystem path on the consumer
            machine where the harness's per-instance artifacts
            live (e.g. ``<repo>/tmp/<job-id>/logs/run_evaluation/
            <run-id>/<model>/<instance-id>/``). Admin's per-
            rollout detail page renders this directory's contents
            inline if the path resolves on the control-plane
            host's filesystem; otherwise shows the path as a
            string for the operator to navigate to externally.
        displayed_name: Operator-friendly name for the admin's
            /rollouts row (e.g. ``"astropy__astropy-7166"``
            instead of the synthetic uuid). Optional; defaults
            to the rollout_id short prefix.
    """
```

**Smoke usage:**

```python
with xrlenv.rollout_metadata(
    artifact_path=str(artifact_root),
    displayed_name=instance_id,
):
    run_instance(test_spec, pred, ..., client=client)
```

**Future extension (illustrative — out of scope for this slice):**
add a kwarg → add a column → parse one more `xrlenv.rollout.*` key. No schema churn for existing rows; old smokes keep working without the new kwarg.

## 11. Change log

- **2026-05-07 v1**: drafted after design conversation. D1-D5 locked.
- **2026-05-07 v2**: §10 metadata API locked — typed kwargs (`artifact_path`, `displayed_name`), no free-form labels dict. Schema (§4) gets two typed columns instead of a generic labels JSON. W3/W5/W6/W7/W8/W9 updated to match.
- **2026-05-07 v3 — SHIPPED**: full slice landed in `5d8e7e5`. 25 new tests (1288 total), mypy + ruff clean.
- **2026-05-07 v4 — audit response**: closed `P1.7.B.3-Admin-M1` (added `raw_status` query param + dropdown above the raw-rollouts table; `_list_raw_rollouts_blocking` accepts the optional status; invalid values logged + ignored rather than 500-ing) and `P1.7.B.3-Docs-M1` (this delta block + change-log entry; risks-§8 ContextVar entry rewritten with the correct semantics; `rollout_labels` references retired in favor of `rollout_metadata`).
- **2026-05-07 v5 — audit follow-up**: closed remaining `P1.7.B.3-Docs-M1` partial-staleness in the lower sections of the plan. **D3** (§3 Decisions): contextvar rationale rewritten — smoke sets inside the worker function rather than relying on ThreadPoolExecutor propagation. **W2** (§5 Scope): method names corrected to the shipped surface (`update_raw_rollout`, not `update_raw_rollout_status`); the `**fields` partial-update shape documented. **W8** (§5 Scope): "per-file viewer" claim dropped — admin renders the immediate-children directory listing only; per-file viewing is deferred. **W10** (§5 Scope) + **§7 Verification step 4**: cross-thread propagation claim replaced with per-thread isolation testing (the smoke's actual pattern). **§7 Verification step 6**: `kind=raw:*` column claim dropped (admin shows raw rollouts in a separate "Raw container rollouts" section, not via a `kind` column); label key `xrlenv.rollout.instance_id` corrected to `displayed_name`; "per-instance log files inline" softened to "directory listing of immediate children." **§7 Verification step 7** + **§8 Risks**: `sweep_stuck_transients` extension claim retracted — not shipped in `5d8e7e5`; explicitly documented as a deferred follow-up with the practical-impact note.
