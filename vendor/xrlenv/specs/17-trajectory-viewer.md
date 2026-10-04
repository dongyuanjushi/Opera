# 17 — Trajectory Viewer

## Purpose

Trajectories live on whichever node ran the rollout — the control
plane and the admin panel are on different hosts. An operator who
clicks "show me rollout X" in the admin panel needs the platform to
locate the trajectory, fetch it from the right node, and render it
in a way that's actually useful for debugging an agent rollout
(step-by-step, token-aware, image-aware, searchable).

This spec describes that path end-to-end: the locator, the fetch
protocol, the sink-aware reader, the cache, and the viewer UI.

It is **not** a competitor to wandb / tensorboard. Those tools
excel at training curves and aggregate metrics. The trajectory
viewer is for **per-rollout inspection** — the thing wandb is bad at.

## The four pieces

```
Admin panel (control plane)                      Node agents
┌───────────────────────────────────┐            ┌─────────────────────────┐
│  Viewer UI  (HTMX + Chart.js)     │            │  TrajectoryReader       │
│        │                          │            │   (sink-aware)          │
│        ▼                          │            │     │                   │
│  TrajectoryCache (LRU on disk)    │            │     ├── platform-jsonl  │
│        │                          │            │     ├── slime-sample    │
│        ▼                          │            │     ├── verl-dataproto  │
│  GetTrajectory client     ────────┼─ gRPC ────►│     ├── multi:[...]     │
│        ▲                          │            │     └── none → 404      │
│        │                          │            │                         │
│  Locator (StateStore lookup)      │            └─────────────────────────┘
└───────────────────────────────────┘
```

### 1. Locator — where is the trajectory?

The canonical `TrajectoryLocator` schema is defined in spec 20.
Every sealed rollout's row in the StateStore carries it:

```
rollout_id
  ├── status, started_at, ended_at, final_reward, ...
  └── trajectory_locator:
        sink:        "platform-jsonl" | "slime-sample" | ...
        node_id:     "gcp-1"          | null if sink=none
        uri:         file://, slime://, verl://, gs://, s3://, blob://
        size_bytes:  2_847_192        (rough; for cache budgeting)
        children:    [...]            (set when sink=multi)
```

`uri` is **opaque to the control plane**; only the
`TrajectoryReader` on the matching node (or on the operator
machine when `uri` is an object-store URI per spec 20 phase 2)
knows how to dereference it. For `platform-jsonl` it points at the
canonical run-dir layout (`~/.xrlenv/runs/<date>/<rollout_id>/`,
spec 20); for `slime-sample` it is a buffer key in Slime's data
store; for `verl-dataproto` it's the Ray object ref or on-disk
DataProto path.

When a rollout uses sink `none`, the locator's `node_id` and
`uri` are null and the viewer returns `ReplayUnavailable`. See
spec 08's durability matrix for the per-sink retention story —
`slime-sample` and `verl-dataproto` may be unreachable after the
trainer process exits, and the viewer surfaces that explicitly
rather than silently 404-ing.

### 2. Fetch — `FetchTrajectoryCommand` on the reverse stream

Trajectory fetch is one of the commands defined by the node
control protocol (spec 21). The viewer dispatches a
`FetchTrajectoryCommand` to the rollout's owning node; the node
streams `TrajectoryChunk` messages tagged with the same
`command_id` back. There is **no separate RPC surface** — the
existing reverse stream carries everything, multiplexed via
`command_id`. See spec 21 for the full message envelope and flow-
control caps.

Light metadata is fetched the same way with
`FetchTrajectoryCommand { range: SUMMARY_ONLY }`, returning a
single `TrajectorySummary` chunk. This avoids paying for
screenshots in list views.

`include_binary=false` (default) returns step records with
`BlobRef` placeholders in observation/info fields; the renderer
fetches blobs on demand for the visible step window via a follow-
up command with `include_binary=true` over the same stream. The
`BlobRef` schema is defined in spec 14.

Idempotency key derivation (spec 21):
`rollout_id + ":" + range_hash`. Repeated fetches for the same
range return the cached reply on the node side.

### 3. Sink-aware reader — speaks every sink the platform supports

A `TrajectoryReader` lives on each node, with one implementation
per `TrajectorySink` (spec 08):

| Sink | Reader behavior |
|---|---|
| `platform-jsonl` | Opens `<run_dir>/trajectory.jsonl`, parses lines, normalizes to the wire `TrajectoryStep` shape. Cheapest path; the default. |
| `slime-sample` | Reaches into Slime's data buffer using the rollout's buffer key, fetches the `Sample`, converts back to `TrajectoryStep` for the wire (token / loss_mask / response_length fields populated). Works only when Slime is installed and the buffer is reachable from the node agent process. |
| `verl-dataproto` | Reads the DataProto from disk or Ray object store, projects per-step rows back into `TrajectoryStep`. |
| `multi:[a, b, ...]` | Reads from the *first* sink in the list whose data is locally readable; falls back through the rest. |
| `none` | Returns `ReplayUnavailable("sink=none")` immediately. The viewer shows "no recording" instead of a step list. |

The conversion *to a normalized wire shape* — every sink emits
`TrajectoryStep` records with the same schema — is the key
abstraction. The viewer doesn't have to know which sink wrote the
trajectory; it always sees the same step records (with
sink-specific fields like `tokens` populated when available).

### 4. Control-plane cache

Trajectories can be megabytes; an operator clicking around the
admin panel doesn't want a fresh node-agent fetch per click.

`TrajectoryCache` lives in the control-plane process:

- Local disk: `~/.xrlenv/admin-cache/trajectories/<rollout_id>/`
- LRU with a configurable budget (default 5 GB).
- TTL-bounded (default 1 h since last view).
- On miss: dispatch `GetTrajectory` to the rollout's node, stream
  to disk, hand back to the renderer.
- Invalidation: a sealed trajectory is immutable, so the cache
  needs no invalidation other than LRU eviction.

For very large trajectories (OSWorld with hundreds of screenshots),
the cache can be set to **lazy-binary** mode: cache the step records
fully, fetch screenshots on-demand as the user scrolls. The viewer
sends a follow-up `GetTrajectory(range, include_binary=true)` for
the visible step window only.

## Viewer UI (admin panel `/rollouts/<id>`)

The "Rollout detail" page from spec 13. Renders one trajectory
end-to-end. Phase 0 ships a basic version; phase 1 lights up the
features below.

### Header

`rollout_id`, `template`, `task_key`, `group_id`, status (with
color), final reward, # steps, duration, node it ran on, sink. Link
to the upstream task definition when the EnvAdapter exposes one
(e.g. terminal-bench-2's `instruction.md`).

### Step list

Collapsible per step. For each:
- Timestamp + step number + reward.
- **Action**: pretty-printed for known shapes (shell command,
  pyautogui-script, structured tool call, JSON action). Raw bytes
  available via "show raw."
- **Observation**: inline rendering by type — text, JSON,
  screenshot (PNG/JPEG with click-to-zoom), terminal pane buffer
  with monospace + ANSI handling.
- **Info**: foldable JSON tree; surfaces interesting fields
  (`exit_code`, `tool_call`, `screenshot_diff_pct`) as one-line
  summaries.

### Token-level overlay (when token fields present)

When `tokens` / `logprobs` / `loss_mask` / `response_length` are
populated (LLM-RL cases), an alternate "token view" tab:
- Prompt and response rendered as a continuous token stream.
- **Logprob heatmap** — per-token color from green (high prob) to
  red (low prob). Hover for exact logprob.
- **Loss-mask highlighting** — tokens contributing to the gradient
  have a faint blue underline; tokens with mask=0 are dim.
- Click a token to anchor it to its step (so the user can jump
  back to action/obs context).

### Search

`/find <query>` over the rendered text: matches inside actions,
observations, tool calls, and (when present) tokens. Hits jump in
the step list.

### Compare two trajectories (phase 1)

`?compare=<other_rollout_id>` opens side-by-side view:
- Step-aligned (when steps match) or independent (when they
  diverge).
- Diff highlights on actions and rewards.
- Useful for "same task, different policies" or "same task, two
  iterations apart."

### Download

- "Download raw" gives the trajectory in its native sink format
  (jsonl for platform-jsonl, sample dump for slime-sample, etc.).
- "Download as jsonl" always renders the normalized
  `TrajectoryStep` schema regardless of sink — useful for downstream
  tooling that wants one format.

## List view, filters, bulk

The admin panel's `/rollouts` list (spec 13) gets filter + bulk
operations:

- Filter by template, status, reward range, task_key, group_id,
  time window, node.
- Sort by reward / duration / timestamp.
- Bulk select → "Download N as tarball" (control-plane streams a
  tar of jsonls fetched lazily from each owning node).
- Bulk select → "Pin" (mark immune to retention rotation, see
  storage below).

Bulk fetch is concurrency-limited per node so an operator's "give
me a thousand trajectories" doesn't starve the node-agent stream of
heartbeats and ops calls.

## Storage / retention

The retention matrix lives in spec 20; viewer-relevant rows:

- Per-node run-dir rotation (`retention_days`, default 14;
  pinned-rollout exemption preserved by the spec-09 GC).
- Optional async archival to object storage in phase 1 — once
  configured (spec 20 `object_store.trajectories`), the seal hook
  uploads after closing the local file; the locator's `uri` flips
  to the remote form once the upload succeeds.
- Phase 2 promotes object storage to primary; per-node disk
  becomes a hot cache. The viewer's `TrajectoryReader` plugin set
  is unchanged — readers consult the locator and dispatch to
  whichever URI scheme is current.

## Cross-references

- **Spec 03** (control plane): `rollouts` row carries the
  `trajectory_locator` populated at seal.
- **Spec 04** (node agent): the `TrajectoryReader` plugin set
  lives here.
- **Spec 08** (observability): sinks + the normalized per-step
  schema this viewer reads against; durability matrix tells the
  viewer whether a fetch can succeed after the trainer has exited.
- **Spec 11 / 12** (Slime / verl): the `slime-sample` and
  `verl-dataproto` reader implementations live in those adapters,
  optional-import pattern.
- **Spec 13** (admin panel): the `/rollouts` and
  `/rollouts/<id>` pages this spec drives.
- **Spec 14** (env adapters): `BlobRef` schema for screenshots /
  large blobs the viewer renders.
- **Spec 20** (state and storage): canonical `TrajectoryLocator`
  schema; per-rollout run-dir layout; retention matrix.
- **Spec 21** (node control protocol): `FetchTrajectoryCommand`
  message envelope, multiplexing rules, idempotency.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: locator + StateStore columns; `GetTrajectory` RPC;
  `platform-jsonl` reader; control-plane cache (in-memory + on-disk
  with LRU + TTL); admin-panel rollout-detail page renders step
  list + actions + observations + info; search; raw download;
  per-node retention rotation.
- **Phase 1**: sink-aware readers for `slime-sample` and
  `verl-dataproto`; token-level overlay tab; image rendering
  inline; lazy-binary cache mode; comparison view; bulk download;
  optional archival to object storage; pinning.
- **Phase 2**: full object-storage backing; cross-cluster federation
  (an operator querying one control plane sees archived
  trajectories from another); programmatic API
  (`xrlenv trajectories query --filter ...`) for offline analysis
  pipelines.

## Non-goals

- Not training-metrics dashboarding. Reward-over-iterations,
  KL/entropy, eval accuracy → wandb / tensorboard. We render *one
  rollout* at a time, well.
- Not a long-term archival service by itself. We hand off to object
  storage when the user has a bucket; we don't ship a managed
  storage layer.
- Not an annotation / labeling UI. Phase 2 may add lightweight
  bookmarks; rich annotation is out of scope.
