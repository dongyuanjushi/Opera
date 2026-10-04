# Proposal: artifact-export primitive (bulk container→out-of-band, not through the control plane)

**Status:** proposal / design-only (not accepted, not implemented).
**Author context:** raised while fixing the EvoClaw node-lost bug (whole-`/testbed` copies).
**Would amend:** spec 04 (node agent), spec 08 (observability / trajectory sinks), spec 20 (state & storage), spec 21 (node control protocol). Phase target: **phase 2** (object-store mode); a phase-1 node-local-disk mode is a cheap first step.

---

## 1. Problem

Benchmarks legitimately need to get *bulk* data out of a container after a rollout:

- EvoClaw's `cleanup()` does `docker cp {container}:/testbed .` — the whole repo (source + build output + `node_modules`), hundreds of MB per task.
- swebench/OSWorld may want full run logs, screenshots, or a working tree for debugging.

Today the only way out is `container_get_archive`, which relays the tar **through the control plane**:

```
eval container (node) → node agent → CONTROL PLANE (reassembles whole tar in RAM) → client → host disk
```

This violates two load-bearing rules:

- **Invariant 6** (spec 00): *"State store holds metadata; blobs live on disk or object store. Trajectory bodies, snapshot artifacts, image layers never go into"* the control path. A 773 MiB tarball on the control bidi stream is exactly that.
- **The three-plane split**: the control plane is orchestration/metadata, not a data pipe. Bulk transfers there congest the stream the heartbeat shares and force the single-process (phase-0/1) control plane to buffer the whole tarball in RAM (× concurrent transfers).

We just added two guardrails that make this *safe* but not *good*:
1. **Node-side streaming get_archive** (off the event loop) + a concurrent-transfer cap — a big copy can't freeze the node.
2. **`DEFAULT_MAX_GET_ARCHIVE_RELAY_BYTES` (128 MiB) relay cap** — a get_archive over the cap is refused (`ArchiveTooLarge`), failing that one transfer cleanly.

The relay cap is a *guardrail*, not a *feature*: it says "you may not do this here." This proposal is the *feature* — a first-class way to export large artifacts that keeps the bytes off the control plane.

## 2. Goals / non-goals

**Goals**
- Export an arbitrary container path (file or dir) as an artifact **without** the bytes transiting the control plane.
- Control plane exchanges only **metadata** (a URI / handle + size + digest), honoring invariant 6.
- Bounded, back-pressured, and node-autonomous (no control-plane RAM buffering).
- Byte-compatible with what a consumer expects (tar of the path), so benchmark harnesses can adopt it with a thin shim.

**Non-goals**
- Not a live bind mount / continuous sync (that's a separate, heavier feature).
- Not a replacement for `container_get_archive` for *small* reads (reward files, patches) — those stay on the control plane; they're cheap and the round trip is convenient.
- Not trajectory storage (spec 08 sinks already own that).

## 3. Design

### 3.1 Primitive shape

A new node-executed command (spec 21):

```
ExportArtifactCommand {
  rollout_id, container_id, source_path,
  sink: { kind: NODE_LOCAL | OBJECT_STORE, ... },
  compression: NONE | GZIP,
}
→ ExportArtifactReply { uri, size_bytes, sha256, entry_count }   // METADATA ONLY
```

The node tars `source_path` (reusing the same off-loop chunk pump as the fixed `get_archive_stream`) and writes it to the sink:

- **`NODE_LOCAL`** (phase-1-cheap): write to the node's run-dir on local disk, e.g. `runs/<rollout_id>/artifacts/<name>.tar[.gz]`. The reply URI is `file://<node_id>/<path>` (or a node-relative handle). Operators/clients fetch out-of-band (rsync/scp/`xrlenv artifacts pull`). Bytes never leave the node until someone explicitly pulls them.
- **`OBJECT_STORE`** (phase-2): stream-upload to S3/GCS (multipart), reply URI is `s3://bucket/key`. The client/trainer fetches from object store directly. This is the scalable target.

### 3.2 Data path

```
eval container (node) → node tars + streams → sink (node disk / object store)
control plane  ⇠ ExportArtifactReply { uri, size, sha256 }   (metadata only)
client/operator ⇠ fetches from the sink out-of-band (not via CP)
```

The control plane never sees the blob. Peak control-plane RAM per export = one small reply.

### 3.3 Back-pressure & bounding

- Reuse the node's `_archive_gate` (concurrent bulk-transfer cap) so exports and get_archives share one bound.
- Object-store uploads are multipart-streamed (constant memory); node-local writes stream chunk-by-chunk (constant memory).
- A per-node total-bytes/hour or disk-headroom guard (tie into the existing disk-pressure guard) prevents artifact writes from filling the data root.

### 3.4 Retention / GC

- Node-local artifacts live under the rollout's run-dir and are swept by the existing run-dir GC (spec 09) — same lifecycle as trajectories/logs.
- Object-store artifacts get a lifecycle policy (TTL) set at bucket config; xrlenv records the URI in the StateStore (metadata) and the GC reconciler can issue deletes.

### 3.5 Client/consumer surface

- SDK: `session.export_artifact(source_path, sink=...) -> ArtifactRef` (URI + size + digest).
- `xrlenv artifacts pull <rollout_id>` CLI for node-local mode.
- Benchmark adoption: a thin shim maps the harness's `docker cp {c}:/path host` to `export_artifact` + (optionally) an out-of-band pull, instead of a control-plane get_archive. EvoClaw's `copy_testbed` would target this.

## 4. Interaction with the relay cap (already shipped)

- `container_get_archive` keeps the 128 MiB relay cap for the small-read case; anything over it says "use export_artifact."
- The relay cap's `ArchiveTooLarge` message should point at this primitive once it exists.
- Until this lands, large-artifact capture is simply **not supported** on the cluster — and that's stated loudly (EvoClaw wrapper warns; the cap error is explicit). No silent truncation.

## 5. Alternatives considered

- **Raise the relay cap to whole-repo sizes.** Rejected: keeps bulk data on the control plane (invariant 6 violation), and CP RAM buffering × concurrency doesn't scale on a single-process control plane.
- **Chunk get_archive straight to the client but bypass CP buffering (CP as pass-through, not reassembler).** Partial improvement (removes CP RAM buffering) but the bytes still ride the control bidi stream the heartbeat shares — still the wrong channel. A separate data channel is essentially "object store / node-local" anyway.
- **A dedicated bulk-data gRPC stream (separate channel, still node↔CP↔client).** More moving parts than writing to a sink the client can already reach; only worth it if we can't assume shared object store / node reachability. Revisit if the deployment can't provide either.

## 6. Open questions

1. Node-local pull ergonomics when the operator can't reach the node directly (VM-only access): may force OBJECT_STORE as the only practical mode → makes this genuinely phase-2 (needs object-store credentials on nodes, spec 19 auth).
2. Do we need per-artifact digests for reproducibility/caching, or is size+URI enough?
3. Should `export_artifact` be exposed on the *rollout* API (client-driven) or only as a node/coordinator capability the benchmark adapter calls? (Mechanism-not-policy suggests: expose the primitive; let adapters decide when to call it.)
4. Compression default: GZIP saves bandwidth/disk for source trees but costs CPU on an already-busy node — probably opt-in.

## 7. Increment plan

- **P-a (phase 1, cheap):** `ExportArtifactCommand` with `NODE_LOCAL` sink → run-dir; reply URI + size + sha256; reuse `_archive_gate` + off-loop pump; run-dir GC covers retention. Unblocks "capture the testbed to node disk for later inspection" without touching the control plane.
- **P-b (phase 2):** `OBJECT_STORE` sink (multipart streaming upload), StateStore URI record, bucket TTL, spec 19 node credentials.
- **P-c:** SDK `export_artifact` + `xrlenv artifacts pull`; EvoClaw shim retargets `copy_testbed` here.
