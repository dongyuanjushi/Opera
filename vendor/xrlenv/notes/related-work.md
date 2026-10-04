# Related work — design lineage notes

This file records other open-source / public projects whose
designs xrlenv has studied, what we considered borrowing, and
what we explicitly chose NOT to borrow. The intent is to leave a
paper trail so future readers know **why** xrlenv ended up with
the specific shapes it did, not just **what** they are.

Each entry has the same structure:

- **Mission** — one paragraph on what the project is.
- **Studied** — date + the bits we looked at.
- **Borrowed** — patterns / shapes we adopted, with the
  xrlenv slot they landed in.
- **Rejected** — patterns we considered and explicitly chose NOT
  to take, with the reasoning.
- **Caveats** — places where the upstream docs were thin or
  unclear (so a future second pass knows where to look).

---

## CubeSandbox (Tencent Cloud) — `TencentCloud/CubeSandbox`

**Studied:** 2026-05-01 (during phase-1 planning, post-phase-0
acceptance).

### Mission

Open-source "Instant, Concurrent, Secure & Lightweight Sandbox
Service for AI Agents" built on RustVMM + KVM. Markets sub-60 ms
cold start, <5 MB memory overhead per microVM, kernel-level
isolation via dedicated guest kernels, and **E2B SDK
compatibility** (drop-in replacement by changing an env var). The
repo ships a full multi-node orchestration layer alongside the
microVM hypervisor — not just a sandboxing primitive.
Apache 2.0; created 2026-04-10. Stack: Rust 51% / Go 26% /
C 18% (eBPF datapath).

### What we read

- `docs/architecture/overview.md`, `docs/architecture/network.md`.
- `docs/guide/templates.md`, `multi-node-deploy.md`, `self-build-deploy.md`.
- `CubeMaster/api/services/cubebox/v1/cubebox.proto` and the
  duplicated proto under `Cubelet/api/services/...`.
- `CubeMaster/api/services/images/v1/images.proto`.
- `Cubelet/pkg/store/image/image.go` — per-node image store.
- `Cubelet/pkg/masterclient/client.go` — Cubelet → master
  REST heartbeat.
- `Cubelet/pkg/cubelet/cubelet.go` — node loop. Notably contains
  files literally named `kubelet_node_status_*.go` —
  **Cubelet is a fork of Kubernetes' kubelet**, retargeted from
  PodSpec to CubeSandbox.

### Architecture overview (their terminology)

| Component | Role | Language |
|---|---|---|
| **CubeAPI** | E2B-compatible REST gateway | Rust |
| **CubeMaster** | Cluster orchestrator (apiserver-shaped) | Go |
| **CubeProxy** | Reverse proxy parsing `<port>-<sandbox_id>.<domain>` | OpenResty |
| **Cubelet** | Per-node lifecycle agent (kubelet fork) | Go |
| **CubeShim** | containerd Shim v2 ↔ in-VM agent bridge | Rust |
| **CubeHypervisor** | KVM microVM driver (rust-vmm lineage) | Rust |
| **cube-agent** | In-VM PID-1; ttrpc/vsock sandbox API | Rust |
| **CubeVS** | eBPF virtual switch (TC + XDP) | C/eBPF + Go |

**Wire protocols:** all sandbox-control RPCs are **gRPC unary**
(no streaming anywhere in the public proto). Cubelet → master
status is **plain HTTP REST** (POST + periodic heartbeat). In-VM
cube-agent speaks **ttrpc over vsock**.

### Borrowed (with xrlenv slot)

- **Three image-distribution strategies, including
  `shared_storage`.** Their `Image` struct carries `NfsRootfs`
  and `CosRootfs` fields — alternate per-image storage backends
  where one node builds and uploads, every node mounts read-only.
  This is exactly the third option D20 was groping toward.
  - **xrlenv slot:** B3.3 / D20. `image_pin_mode` becomes a
    three-mode enum: `registry_digest`, `per_node_local`,
    `shared_storage`. New B3.4 companion script
    `deploy/mount-shared-rootfs.sh`.
- **`CommitSandbox` / `AppSnapshot` RPC shape.** Their
  `CubeboxMgr` service ships `AppSnapshot(create + snapshot +
  destroy as a build helper)` and `CommitSandbox(running →
  template)`. The latter lets an operator (or RL pipeline) run
  an interactive sandbox to set up a complex environment, then
  freeze it into a template that subsequent rollouts boot from
  in tens of ms. This is the killer pattern — it's not that
  microVM cold-start is faster, it's that *cold start is replaced
  with resume-from-snapshot*.
  - **xrlenv slot:** new B10.4. Define proto RPC shapes for
    `Snapshot` / `Restore` / `CommitSandbox` in phase 1 even
    though execution stays phase 3. Cheap forward-compat.
    Spec-18-aligned.
- **`template_locality` scheduling signal.** Their kubelet-fork
  scheduler has a `template_locality` filter: prefer nodes that
  already have the template's rootfs/snapshot loaded. Same idea
  as our D18 image-affinity scheduling.
  - **xrlenv slot:** D18 / B3.1. Validation that the deferred D18
    design is the right shape; we don't need to invent it.
- **ttrpc-over-vsock as the in-VM stub transport** (when xrlenv
  adds a microVM backend, phase 2). xrlenv's phase-0 stub is
  uds/tcp over Docker's Linux-namespace; for microVM the natural
  transport is vsock + ttrpc. Reusing the protocol shape would
  also let us potentially adopt their `cube-agent` (rustjail-
  backed in-VM init) wholesale.
  - **xrlenv slot:** phase 2 sandbox-pool extensions, when the
    CubeSandbox backend lands.

### Rejected

- **Kubelet-fork node-agent + REST-heartbeat / inbound-cubelet-
  gRPC topology.** Cubelet is literally a fork of K8s kubelet
  (`kubelet_node_status_others.go` etc. live in their tree).
  That's why their orchestration looks K8s-flavored — it
  inherited polling-heartbeat + status-update wholesale.
  - **Why we don't borrow:** xrlenv invariant 5 is
    outbound-only nodes with bidi gRPC streaming. Better firewall
    posture (no inbound listener on data-plane VMs), event-push
    without polling, and cleaner failure-mode reasoning. Reversing
    invariant 5 to match CubeSandbox would be a regression for
    cloud / restricted-network deployments. Recorded in spec 00
    invariant 5 + spec 04 §"Outbound-only contract".
- **MySQL + Redis as state-store split.** CubeSandbox uses MySQL
  for durable cluster state and Redis for hot cache / pub-sub.
  - **Why we don't borrow:** xrlenv's phase-1 plan calls for
    Redis only (B6.1) for the 500-1k concurrent target.
    Adding MySQL on top doubles operational footprint without
    a payoff at our scale. SQL queries don't dominate the hot
    path; Redis sorted-sets cover the event journal. Phase 2's
    durable-trajectory-store work could revisit this.
- **No auth on the public surface.** CubeSandbox's master gRPC
  and cubelet ↔ master REST channels carry no documented
  authentication mechanism. The proto messages have no auth
  fields beyond image-pull annotations.
  - **Why we don't borrow:** xrlenv ships bearer tokens + scoped
    audit (Slice 8) and plans mTLS in P1.x. CubeSandbox's
    auth-absence is a cautionary tale — "in production it's
    handled by network isolation" leaves operators with no clear
    contract.
- **kubelet-style PodSpec abstraction in cubelet.** They retained
  enough kubelet shape (`kl.syncNodeStatus` polled via
  `wait.JitterUntil`) that the codebase carries the K8s mental
  model.
  - **Why we don't borrow:** xrlenv's `node_agent.py` is ~500
    lines; forking kubelet would balloon it and inherit Pod /
    Container abstractions we don't want. Spec 04 deliberately
    keeps the node-agent surface narrow.

### Caveats / thin spots in upstream docs

- **Image distribution between nodes is not documented.** The
  `NfsRootfs` / `CosRootfs` fields in their image struct hint
  at object-storage-backed shared rootfs, but `multi-node-
  deploy.md` doesn't describe how an image gets from "built on
  one node" to "available on all nodes". Any future deeper
  borrow on `shared_storage` will need to read the cubelet
  source path that resolves these fields.
- **Image-cache eviction policy isn't visible** in the file we
  read (`Cubelet/pkg/store/image/image.go`). They have
  `imageRefTimeMap` access-time tracking flushed every 10 min,
  and a `Pinned: true` flag, but no LRU eviction implementation
  surfaced. Worth a deeper look when we implement A6 / D16.
- **No event/metrics catalog.** The cube-agent exports Prometheus
  metrics from inside the VM, but the master-side observability
  surface is undocumented. Their `docs/` doesn't have an obvious
  metrics page.

### One-paragraph summary

CubeSandbox is significantly ahead of xrlenv on the
**sandboxing primitive** (microVM + memory-snapshot hot start
+ eBPF datapath) and roughly comparable in ambition on
**orchestration**, but the topology choices diverge sharply:
they're a kubelet-fork polling architecture with REST
heartbeats and gRPC unary RPCs, no documented auth, MySQL +
Redis state. xrlenv is an outbound-only bidi-stream
architecture with bearer tokens (mTLS planned), Redis-only state
(phase 1+). The most-likely-borrowable patterns are (a) the
**three-mode image-pin enum including `shared_storage`** that
inspired our updated B3.3, (b) the **`Snapshot` / `Restore` /
`CommitSandbox` RPC shapes** that inspired our new B10.4
forward-compat hook, and (c) **template-locality scheduling**
which validates D18's design. We explicitly do not borrow the
kubelet-fork topology, the MySQL-split state store, or the
auth-absent posture.
