# Design: chunked `container_put_archive` (upload direction)

**Status:** IMPLEMENTED 2026-08-07 (surfaced 2026-08-05 by the frontier-swe
`dart-style-haskell` oracle). Offline tests green (`tests/unit/test_put_archive_chunking.py`
+ 2026 node/control/client regression pass); **cluster validation pending** (re-run
`dart-style-haskell` with a redeployed node to confirm the 340 MB upload succeeds → gate 7).
Implemented exactly as designed below: proto `ContainerPutArchiveChunk`
(node) + `ContainerPutArchiveStream` (client) + `NodeHello.chunked_put_archive_capable`;
client stream+unary-fallback; CP client-stream reassembly + chunked CP→node send via
`extra_frames`; node `_accumulate_put_archive_chunk`; `RESOURCE_EXHAUSTED`-oversize →
`ArchiveTooLarge` (not `CapacityExhausted`).

## Problem

`container_put_archive` (client → CP → node, the *upload* path harbor uses to stage
`solution/` into a container) is a **unary** RPC that packs the whole tarball into a
single `ContainerPutArchiveCommand.tarball` field. gRPC's send ceiling is
`DEFAULT_MAX_MESSAGE_BYTES = 128 MiB`, so any upload larger than that fails
deterministically:

```
grpc RESOURCE_EXHAUSTED: "CLIENT: Sent message larger than max (639180917 vs. 134217728)"
  xrlenv/client/transport.py:1093  container_put_archive
```

frontier-swe's `dart-style-haskell` oracle bundles a **340 MB Dart SDK** under
`solution/`; harbor tars it into a 639 MB `put_archive` → over the cap → the oracle
never runs. It would fail identically in isolation (payload size, not contention).

Two distinct bugs:

1. **No chunking on the upload path.** The **download** path (`get_archive`) was
   already fixed for exactly this (`ContainerGetArchiveStream` → `ContainerGetArchiveChunk`,
   proto tag 30, "oversized-reply fix"). The upload path is the un-mirrored twin.
2. **Mislabel.** `transport.py:553` maps *every* `grpc RESOURCE_EXHAUSTED` →
   `CapacityExhausted`. An oversized-message error is not capacity — and because
   `CapacityExhausted` is in the sweep's infra-retry set, it burned 6 pointless
   retries on a deterministic failure and reported a misleading cause.

## Precedent to mirror (the download fix already in tree)

The get_archive fix is the exact template — reuse its shapes/constants:

- proto: `ContainerGetArchiveChunk { bytes data; bool done; }` + a server-streaming
  `ContainerGetArchiveStream` RPC; node→CP hop reuses the bidi `CommandReply`
  chunk-by-`command_id` pattern.
- client (`transport.py:1106`): prefer the stream, reassemble `data` slices until
  `done=true`, **fall back to the unary RPC on `UNIMPLEMENTED`** (old peer).
- constants: `ARCHIVE_CHUNK_BYTES = 4 MiB`, `MAX_OUTBOUND_MESSAGE_GUARD_BYTES = 127 MiB`.
- `rollout_endpoint.py:782` slices the tarball in `ARCHIVE_CHUNK_BYTES` on the relay.

## Proposed change (symmetric, upload direction)

1. **proto** (`node_control.proto` + the client-facing rollout proto):
   - Add `ContainerPutArchiveChunk { bytes data = 1; bool done = 2; }` (mirror of
     `ContainerGetArchiveChunk`).
   - Client → CP: a **client-streaming** RPC `ContainerPutArchiveStream` — the first
     chunk (or a small header message) carries `rollout_id` / `container_id` /
     `target_dir`; subsequent chunks carry `data`; the last sets `done=true`.
   - CP → node: stream the tarball to the node in `ContainerPutArchiveChunk` NodeMsgs
     sharing one `command_id` (mirror the get_archive node hop), terminated by a
     `PutArchiveReply` (tag 16, already the put_archive result type). The node
     reassembles in order, then calls docker `put_archive` once.
2. **client** (`transport.py::container_put_archive`): slice `tarball` into
   `ARCHIVE_CHUNK_BYTES` and send via the stream; on `UNIMPLEMENTED` (old CP), fall
   back to today's unary `ContainerPutArchive` (keeps the 128 MiB cap — the best an
   old server can do), exactly like `container_get_archive`.
3. **CP** (`grpc_endpoint.py` / `rollout_endpoint.py` / `node_transport.py`):
   reassemble the client stream (bounded by a total-size guard), relay to the node as
   chunks, await the node's `PutArchiveReply`. Keep the 300 s `_ARCHIVE_DEADLINE_S`
   ceiling on the whole stream.
4. **error mapping** (`transport.py:553`): only map `RESOURCE_EXHAUSTED` →
   `CapacityExhausted` when it is **not** the client-side "Sent message larger than
   max" case. For the oversize case raise a precise error (reuse `ArchiveTooLarge`,
   or a new `MessageTooLarge`) that is **not** in the infra-retry set, so it fails
   fast + loud instead of retrying. With chunking in place this becomes a
   belt-and-suspenders guard (each chunk is < cap).

## Backward compatibility

- **New client ↔ old CP:** client gets `UNIMPLEMENTED` on the stream, falls back to
  unary (128 MiB cap) — same behavior as the get_archive fallback.
- **New CP ↔ old node:** the CP can detect the node lacks the chunked put command and
  fall back to a single node command (128 MiB), symmetric to how a new CP still
  handles an old node's single `ContainerGetArchiveReply`.
- No task/plan/schema changes; purely a transport capability.

## Test plan

- Unit: chunk/reassemble round-trips a >128 MiB payload byte-for-byte; a `done`
  terminator with empty `data` is handled; a mid-stream node FAILED reply aborts.
- Unit: the oversize `RESOURCE_EXHAUSTED` no longer maps to `CapacityExhausted`.
- Integration (cluster): re-run the frontier-swe sweep with `dart-style-haskell`
  **removed from EXCLUDE** — expect its 340 MB `solution/` to upload and the oracle
  to produce a gradeable reward (then it rejoins the green set → 5/5).

## Scope / risk

Medium: touches the node-control proto + three transport layers, but the shape is a
direct mirror of the shipped get_archive stream, so the pattern, constants, and
fallback discipline are all already proven in-tree. No benchmark-side or task-schema
change. Recovers `dart-style-haskell` and unblocks any future >128 MiB upload
(large seed corpora, bundled toolchains).
