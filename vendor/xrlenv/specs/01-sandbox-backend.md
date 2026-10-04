# 01 — Sandbox Backend

## Purpose

Define the runtime-agnostic interface that every sandbox runtime
(`docker`, `cubesandbox`, future `firecracker`/`gvisor`/`k8s-pod`) must
implement, plus the in-sandbox "stub" protocol that carries exec/file
operations into a running sandbox.

This spec is *only* about the primitive operations on a sandbox. The
gym/step layer that turns these primitives into RL semantics is in
[02-rollout-api.md](02-rollout-api.md).

## `SandboxBackend` interface

All methods are async. Streaming methods return async iterators.

### Supporting data shapes

```python
@dataclass
class MountSpec:
    """A host-path bind-mount made visible inside the sandbox at create time.

    WHY: lets many sandboxes share an installed agent binary, a model
    checkpoint, or a dataset without copying it in over the wire per
    sandbox. For the "ship one big binary into 1000 sandboxes" pattern
    this is dramatically faster than `write_file` per sandbox. Read-only
    by default so a misbehaving sandbox can't corrupt shared state.
    """
    host_path: str       # absolute path on the node
    sandbox_path: str    # absolute path inside the sandbox
    readonly: bool = True

@dataclass
class ServiceSpec:
    """Description of a long-lived in-sandbox process.

    Used both by `spawn_service` (post-create dynamic launch) and by the
    template manifest's `services:` block (declared, started by the
    in-sandbox stub during init).
    """
    name: str                          # unique within the sandbox; addressable
    cmd: list[str]                     # argv
    port: int | None = None            # internal TCP port the service listens on
    env: dict[str, str] | None = None  # extra env vars
    health_check: list[str] | None = None     # argv probed until exit-0 (or None)
    startup_timeout_s: float = 30.0    # raise if health check doesn't pass in time
    wait_until_ready: bool = True      # block spawn_service until health passes
    depends_on: list[str] = field(default_factory=list)  # other ServiceSpec.name
    restart: Literal["never", "on_failure", "always"] = "never"

@dataclass
class ResourceSpec:
    cpu_request: float
    cpu_limit: float
    mem_request_bytes: int        # bytes; YAML accepts "8GB" / "512MB" forms
    mem_limit_bytes: int
    disk_request_bytes: int
    gpu_required: bool = False
    mounts: list[MountSpec] = field(default_factory=list)
```

YAML manifests (spec 06) use convenience strings (`"8GB"`,
`"512MB"`, `"4Gi"`); the manifest loader in the catalog
normalizes them to integer bytes before constructing
`ResourceSpec`. SI suffixes (`KB`, `MB`, `GB`, `TB`) are powers of
1000; binary suffixes (`KiB`, `MiB`, `GiB`, `TiB` and the short
`Ki`, `Mi`, `Gi`, `Ti`) are powers of 1024. The estimator and
admin panel always operate on the canonical integer-bytes form.

### The protocol

```python
class SandboxBackend(Protocol):
    """Runtime-agnostic interface to one sandbox runtime (Docker, Cube, ...).

    Returned handles are opaque to the caller — only the backend that
    issued a handle ever dereferences it. The control plane / node agent
    pass handles around but never look inside them.
    """

    name: str            # "docker" | "cubesandbox"
    capabilities: SandboxCapabilities

    # ── Lifecycle ────────────────────────────────────────────────────

    async def create(
        self, template: TemplateRef, resources: ResourceSpec,
        network_policy: NetworkPolicy,
    ) -> SandboxHandle:
        """Instantiate a new sandbox from a template; return its handle.

        WHY: the only entry point that yields a SandboxHandle. Every
        other method operates on a handle returned from here (or from
        `restore`). `resources` (cgroup / hypervisor caps),
        `network_policy`, and `resources.mounts` (host bind-mounts) are
        baked in *at create time* because they are hard or impossible
        to retrofit on a running sandbox — Linux namespaces, mount
        propagation, and microVM device tables are decided at boot.
        """

    async def destroy(self, sb: SandboxHandle) -> None:
        """Terminate the sandbox and release all its resources.

        WHY: explicit, eager teardown is mandatory because the capacity
        estimator and scheduler must know precisely when CPU / memory /
        disk free up. We cannot rely on Python GC or runtime auto-cleanup
        (Docker `--rm`, etc.): truncated rollouts must release their
        sandbox *within the hard-deadline window* so the next batch
        isn't starved. Idempotent: destroying an already-destroyed
        sandbox is a no-op, never an error, since recovery paths may
        double-call.
        """

    # ── Action primitives (the agent acts via these) ─────────────────

    async def exec(
        self, sb: SandboxHandle, cmd: list[str],
        stdin: bytes | None = None, env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecChunk]:
        """Run a one-shot command; stream stdout/stderr; finish with an
        ExitCode chunk.

        WHY: the primary action primitive for shell-shaped envs
        (terminal-bench, SWE-bench). Streaming (vs return-the-string) is
        required because a long command must not block the orchestrator
        and observations may need to be consumed incrementally by the
        agent. `stdin` for commands that read piped input;
        `env` for per-command env vars (e.g., `PYTHONPATH` overrides);
        `timeout_s` because an agent-driven command can hang forever
        and we must enforce step deadlines locally rather than relying
        on the hard-deadline at the rollout level.
        """

    async def read_file(self, sb: SandboxHandle, path: str) -> bytes:
        """Read a small file from inside the sandbox by absolute path.

        WHY: many tasks operate on files directly — SWE-bench patches,
        OSWorld task configs, reward signal files dropped by in-sandbox
        judges. Going through `exec("cat ...")` is brittle for binary
        content (encoding round-trip), large payloads (full buffer in a
        single ExecChunk stream), and quoting (paths with spaces /
        special characters).

        Use `read_file_stream` for files larger than ~16 MB to avoid
        holding the whole payload in memory.
        """

    async def write_file(
        self, sb: SandboxHandle, path: str, data: bytes,
    ) -> None:
        """Write bytes to a file inside the sandbox by absolute path.

        WHY: dropping init configs at sandbox start, applying patches
        mid-rollout, seeding inputs the agent needs to act on. Same
        brittleness argument as `read_file` — a direct write avoids
        shell-escape pitfalls of `echo`/heredoc and handles binary
        cleanly. Creates parent directories as needed; truncates if
        the file already exists.

        Use `write_file_stream` for payloads larger than ~16 MB.
        """

    async def read_file_stream(
        self, sb: SandboxHandle, path: str,
    ) -> AsyncIterator[bytes]:
        """Read a file as a chunked async byte stream.

        WHY: large files (model checkpoints, multi-MB result blobs)
        should not be buffered in memory. Streaming lets the SDK pipe
        bytes straight to a local file via `download(local_path)` with
        bounded memory. Backends choose chunk size; 1 MB is a
        reasonable default.
        """

    async def write_file_stream(
        self, sb: SandboxHandle, path: str,
        src: AsyncIterator[bytes],
    ) -> None:
        """Write a chunked async byte stream to a file.

        WHY: the upload counterpart to `read_file_stream`. Used by the
        SDK's `upload(local_path)` helper for large binaries (custom
        agent CLIs, models, packaged dependency tarballs). The backend
        is responsible for backpressure: it must consume `src` only as
        fast as the in-sandbox stub can write. Atomic semantics
        (write-to-temp + rename) so a crashed upload doesn't leave a
        half-written file at `path`.
        """

    # ── Long-lived in-sandbox processes ──────────────────────────────

    async def spawn_service(
        self, sb: SandboxHandle, spec: ServiceSpec,
    ) -> ServiceHandle:
        """Start a single long-lived process inside the sandbox.

        WHY: tool services like Chromium (web-search), an X server +
        screencap (OSWorld), a Python kernel, or the EnvAdapter wrapper
        itself must persist *across many steps* of one rollout. Booting
        Chrome on every step would dominate the step latency.

        The richer `ServiceSpec` (vs raw `cmd`) handles the rough edges
        of multi-service launch: `name` for later lookup
        (`sandbox.service("browser")`), `health_check` + `wait_until_ready`
        so the call doesn't return until the service is actually serving
        (no race with the agent's first step), and `restart` policy so a
        crashed service can be auto-revived. `depends_on` is honored
        only inside `spawn_services` (the batched form below).
        """

    async def spawn_services(
        self, sb: SandboxHandle, specs: list[ServiceSpec],
    ) -> list[ServiceHandle]:
        """Start a set of services with ordering and shared port discovery.

        WHY: many sandboxes need several co-running processes whose start
        order matters — Service B (browser) only comes up after Service A
        (X server) is healthy. Doing this with N separate `spawn_service`
        calls forces the caller to encode the topology and poll readiness.
        This batched form does both: a topological sort of `depends_on`,
        health-gated launch per node of the DAG, and atomic failure
        semantics (any service that fails to come up causes prior ones
        to be torn down).

        Side effect: every spawned process gets the env var
        `XRLENV_SERVICE_PORTS_JSON` injected, mapping
        `{name → internal_port}` for every service in the batch. This is
        how an EnvAdapter inside the sandbox reaches its dependent
        services without hard-coded ports.
        """

    # ── Network exposure ─────────────────────────────────────────────

    async def port_forward(
        self, sb: SandboxHandle, internal_port: int,
    ) -> str:
        """Expose an internal sandbox port at a node-reachable URL.

        WHY: primarily for debugging (a developer wants to point a
        browser at the OSWorld VNC view, or attach a remote debugger to
        an in-sandbox process). Occasionally for letting an external
        tool reach an in-sandbox service. Returns a URL string so
        callers stay agnostic about how the host port was allocated
        (Docker `-p 0:N`, Cube proxy route).
        """

    # ── Snapshot / restore (capability-gated) ────────────────────────

    async def snapshot(self, sb: SandboxHandle) -> SnapshotID:
        """Capture the sandbox's state into a restorable artifact.

        WHY: three uses, all RL-specific: (1) episode-level checkpointing
        — fork at step *t* for MCTS-style search or off-policy
        correction; (2) fast resets — restoring is dramatically cheaper
        than full create+init for templates with expensive setup
        (SWE-bench's per-instance image pull, OSWorld's desktop boot);
        (3) recovery — resume a long-running rollout across node failure
        on backends that capture live state. Backends that don't
        natively support this raise BackendCapabilityMissing; the
        `capabilities.supports_snapshot` flag advertises support so the
        scheduler refuses to place a template that requires snapshot on
        a backend that can't deliver.
        """

    async def restore(self, snapshot: SnapshotID) -> SandboxHandle:
        """Create a fresh sandbox from a previously taken snapshot.

        WHY: the consume side of `snapshot`. Returns a *new* handle, so
        the caller can run independent rollouts that share the same
        prefix (branch semantics). Resource limits and network policy
        are inherited from the original `create` that produced the
        snapshot — a restore is faithful to the original sandbox's
        constraints.
        """

    # ── Observation primitive (resource accounting / debugging) ──────

    async def stats(self, sb: SandboxHandle) -> ResourceUsage:
        """Sample current CPU / memory / disk / network usage of the sandbox.

        WHY: three consumers — (1) the capacity estimator's online
        refinement loop, which needs per-template p95 actual usage to
        update declared resource profiles (spec 10); (2) the admin
        panel's per-sandbox resource graphs (spec 13); (3) per-sandbox
        debugging when an operator wants to know why a sandbox is slow.
        Cheap to call (cgroup file reads or hypervisor counters);
        sampled by the node agent on each ~5-second heartbeat.
        """
```

`SandboxCapabilities` advertises what the runtime actually supports:

```python
@dataclass
class SandboxCapabilities:
    supports_snapshot: bool
    supports_chainable_snapshot: bool   # overlaybd-style cheap incremental snapshots
    live_state_captured: bool           # snapshot captures running process state
    supports_port_forward: bool
    supports_gpu: bool
    isolation_class: Literal["container", "microvm", "none"]
    fast_create_p50_ms: int          # rough cold-start budget for the scheduler
```

`supports_chainable_snapshot` and `live_state_captured` are required
by spec 18 sessions: cold-resume correctness depends on cheap
per-step snapshots that capture live state. Docker has
`supports_snapshot=True` but `supports_chainable_snapshot=False` /
`live_state_captured=False`; Cube has both true.

Templates declare required capabilities; the scheduler refuses to place a
template on a node whose backends don't supply them.

### Manifest field → capability mapping

Templates declare requirements with manifest-level field names
that the catalog rewrites into `SandboxCapabilities` constraints
at register time. This is the canonical mapping (spec 06's
manifest fields on the left, capability flags on the right):

| Manifest field | Required `SandboxCapabilities` predicate | Scheduler validation |
|---|---|---|
| `required_backends: [docker]` | backend driver in node's set | reject placement if missing |
| `required_backends: [cubesandbox]` | backend driver in node's set | reject placement if missing |
| `isolation: container` (informational) | `isolation_class in {"container", "microvm"}` | warn-only |
| `isolation: microvm` | `isolation_class == "microvm"` | reject placement |
| `resources.gpu_required: true` | `supports_gpu: true` | reject placement |
| `snapshot_required: true` | `supports_snapshot: true` | reject placement at register |
| `image.lazy_load: required` | `supports_lazy_load: true` | reject placement on non-capable nodes |
| `image.lazy_load: preferred` | (none — fallback to full pull on non-capable nodes) | warn-only; emit `lazy_fallback` event |
| `session.enabled: true` (phase 3) | `supports_chainable_snapshot && live_state_captured` | reject placement at register |

A template's manifest cannot directly set `supports_*` flags; it
can only express requirements via the fields above. The catalog
runs the rewrite once and stores the resulting predicate set
alongside the digest (spec 00 invariant 4).

## Sandbox stub protocol

A small Python server runs **inside** every sandbox as PID 1's child.
It listens on:

- A unix domain socket (Docker; mounted via volume), or
- A vsock (Cube; standard microVM transport).

The wire format is **a strict subset of E2B's HTTP REST API**. This is
deliberate: CubeSandbox already speaks E2B-compat REST, so its adapter
is a thin remap; and any third-party tool that targets E2B (browser
agents, code interpreters) works with no extra glue.

The stub exposes:

- `POST /commands` — exec
- `GET/POST/PUT /files{path}` — read/write small files (≤16 MB)
- `POST /files/stream{path}` / `GET /files/stream{path}` — chunked
  streaming upload/download for large blobs. Upload uses a temp path +
  rename for atomic-on-success semantics.
- `POST /processes` — spawn one long-lived service (`ServiceSpec`)
- `POST /processes/batch` — batched, topologically-sorted, health-gated
  launch of a service set. Atomic: any failure tears the batch down.
  Injects `XRLENV_SERVICE_PORTS_JSON` into every spawned process.
- `GET /processes/{name_or_id}/logs` — stream
- `POST /processes/{name_or_id}/signal` — send signal (SIGTERM, etc.)
- `GET /processes` — list services with their `name → port` map
- `GET /healthz`
- `POST /env/setup` / `POST /env/step` / `POST /env/teardown` — driven by
  the in-sandbox `EnvAdapter` (see [spec 14](14-env-adapters.md)).
  These are how the trainer SDK's `RolloutSession.step(action)` lands
  inside the sandbox; the adapter wraps benchmark Environment classes
  (OSWorld's `DesktopEnv`, terminal-bench's `Terminal`, …) or
  user-defined ones.

Phase 0 ships a Python implementation. Phase 1 considers a Rust rewrite
if cold-start RSS becomes a bottleneck (Python stub ~25 MB resident is
significant when packing 100s per node).

## Docker adapter (phase 0)

- Driver: `docker-py` over the local Docker socket.
- Each sandbox = one container with:
  - PID 1 = `tini`, child = the stub server.
  - Mounted unix socket bridge to the node agent (`/run/xrlenv/stub.sock`).
  - Per-sandbox bridge network (`xrlenv-sb-<id>`) so each sandbox is
    network-isolated from the others.
  - `cgroupv2` limits matching `ResourceSpec`.
  - `MountSpec`s passed via `-v host:sandbox[:ro]`. Read-only by
    default; required-mode honored from the spec.
  - `--rm` is *not* used; we manage lifecycle explicitly so we can
    inspect dead containers.
- File streaming uses chunked HTTP/1.1 over the stub socket; 1 MB
  chunks; backpressure via TCP flow control on the unix socket.
- `spawn_services` is implemented inside the stub: topo-sort the
  `depends_on` graph, launch one process per node of the DAG, run the
  `health_check` until it returns 0 or `startup_timeout_s` elapses.
  `XRLENV_SERVICE_PORTS_JSON` is set in each process's environment.
- `snapshot` is `docker commit` of the writable layer. Marked
  `supports_snapshot=True` but with `live_state_captured=False` —
  best-effort. Phase 2 may add CRIU but current consensus is "not
  worth the fragility."
- `port_forward` defaults to the control-plane-proxied form
  defined in spec 07 (`https://<control-plane>/forward/<token>/`),
  not a host-bind URL. When the operator opts into
  `bind: node-host`, Docker's built-in port mapping is used and
  the URL is `http://<bind_host>:<published_port>` —
  `bind_host` is `127.0.0.1` by default and `0.0.0.0` only if the
  node was started with `--allow-public-port-forward`.

## Port allocation across many copies of the same template

A common confusion: "if my template declares services on ports
5900 / 22 / 8000 / 9222 / 8888, and I run 50 sandboxes of it on one
node, don't the ports collide?"

They don't. **Every sandbox has its own network namespace**, by
construction:

- Docker adapter: per-sandbox bridge network `xrlenv-sb-<id>`.
- Cube adapter: per-microVM tap interface, isolated by `CubeVS`
  (eBPF).

Implications:

- **Internal ports are private to the sandbox.** Sandbox-1's port
  5900 and sandbox-2's port 5900 are different network endpoints.
  No collision regardless of how many copies run concurrently.
- **Inside the sandbox**, the EnvAdapter and tool services reach
  each other via `localhost:<port>` plus the
  `XRLENV_SERVICE_PORTS_JSON` env var (injected by `spawn_services`)
  for name → port lookup. No hard-coded port assumptions in the
  template; service A finds service B by name.
- **From outside the sandbox**, `port_forward(internal_port)`
  allocates a host port dynamically from a configurable ephemeral
  range (default `40000-49999` per node). The returned URL embeds
  the host-side port. Each sandbox gets a *different* host port for
  the same internal port; no host-side collision either.

### Multiple ports per env, multiple envs per node

A template like OSWorld declares many internal ports
(VNC 5900, SSH 22, controller 8000, debug 9222, jupyter 8888).
Running 50 OSWorld sandboxes on a node uses:

- **50 × per-sandbox network namespaces** (one per sandbox),
  inside each: `localhost:5900`, `localhost:22`, etc., bound by
  the sandbox's services. No collisions.
- **Zero host-side ports** unless an operator explicitly
  `port_forward`s for debugging — most rollouts never expose any
  port to the host.
- **N host-side ports** when `port_forward` is invoked, drawn from
  the ephemeral range. With a 10000-port range and reasonable
  cleanup, supports thousands of concurrent forwards.

### Host-side port exhaustion

If pathological usage exhausts the ephemeral range, `port_forward`
returns `PortRangeExhausted`. Mitigations are operator-side:

- Tune `ephemeral_port_range` per node.
- Cap `max_concurrent_port_forwards` per node (defaults to 1024).
- Avoid auto-forwarding services that don't need external access;
  most tool services should be sandbox-internal only and reached
  via `XRLENV_SERVICE_PORTS_JSON` not `port_forward`.

### Cross-sandbox traffic

Disallowed by default. Phase 0 places each sandbox on its own
isolated network (per-sandbox bridge in Docker; per-microVM tap in
Cube), and the node agent's iptables / eBPF rules drop sandbox →
sandbox traffic. Spec 07 details the network policies.

Cross-sandbox communication for multi-agent rollouts is a phase-2
feature with its own port-discovery mechanism (a `RolloutGroup`
gets a shared private subnet and a service registry keyed by
sandbox name within the group).

## CubeSandbox adapter (phase 1)

- Driver: thin async HTTP client against `CubeAPI` (the E2B-compatible
  REST gateway).
- Templates map 1:1 to Cube template IDs (registered out-of-band via
  `cube-cli` or REST).
- Each sandbox = one microVM. `supports_snapshot=True` with full live
  state. `isolation_class="microvm"`. Snapshots are **chainable**
  (overlaybd-style COW layers) — successive snapshots build on the
  previous one efficiently, enabling fast periodic checkpoints used
  by spec 18's preemption-safe sessions. Phase 1.
- Networking goes through `CubeVS` (eBPF). The adapter sets
  `network_policy` by registering a Cube network policy at create time.
- vsock is the stub transport — no socket bridge mount needed.
- `MountSpec`s map to virtio-fs shares. Read-only mounts use the
  hypervisor's read-only mode (filesystem-level), not just an `ro`
  flag, so a misbehaving guest cannot corrupt the host file.
- Streaming and `spawn_services` semantics are identical to Docker
  (the stub protocol is the same; only the transport differs).

## Function-Call execution mode (phase 1)

Some workloads — math eval, code snippet execution, short tool calls
inside an agent loop — don't need a per-invocation sandbox. Creating
and destroying a full container for a 30 ms Python expression burns
2–3 orders of magnitude more time and memory than the work itself.

**Function-Call mode** is an alternative execution substrate
alongside `SandboxBackend`. A pool of long-lived **executor
containers** per template per node accepts stateless invocations:

```python
class FunctionExecutor(Protocol):
    """One long-lived container that serves many stateless invocations.

    The executor runs an in-container RPC server that accepts a
    workdir-scoped command and returns its result. Each invocation
    runs in a freshly created tempdir under a wipe-on-finish policy;
    no state survives across calls inside one executor."""

    name: str

    async def invoke(
        self,
        cmd: list[str],
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        workdir_files: dict[str, bytes] | None = None,   # tiny prelude files
    ) -> InvocationResult:
        """Run cmd in a fresh tempdir; return stdout/stderr/exit_code.

        WHY this is not the same as `exec` on a SandboxBackend:
        - No per-call create/destroy. Pool of N executors handles
          thousands of invocations.
        - Stateless by construction. The platform wipes the tempdir
          after each call; no smell of a previous call remains.
        - ~1 ms dispatch latency vs ~200 ms for a full sandbox spin.
        """
```

A node's `FunctionExecutorPool` keeps `pool_size` executors warm per
template (configurable; default 4). Invocations queue when all
executors are busy; queue depth is exposed to the scheduler so a
saturated node sheds load to a peer.

### When to use which mode

Per template, choose:

| `execution_mode` | When |
|---|---|
| `sandbox` (default) | Stateful agentic tasks: terminal-bench, SWE-bench, OSWorld. The sandbox holds a long-lived workspace across many steps. |
| `function-call` | Short, stateless tool calls embedded in an agentic flow: a math evaluator a code-tool an agent calls inside a larger rollout, a search-result re-ranker, anything where each invocation is independent. |

A template declares its mode (spec 06's `execution_mode:` field).
Function-Call mode templates expose `client.invoke(template, ...)`
in the SDK (spec 05) instead of `client.rollout(template, ...)`.

### Pool, request, result schemas

```python
@dataclass
class FunctionExecutorPoolSpec:
    template:        str            # which template defines the executor image
    pool_size:       int = 4        # warm executors per node
    max_pending:     int = 64       # queue depth before InvokeRejected
    recycle_after_invocations: int = 10000
    recycle_after_idle_s:      int = 300
    trust_level:     Literal["trusted-only", "untrusted-allowed"] = "trusted-only"

@dataclass
class InvokeRequest:
    template:    str
    cmd:         list[str]
    stdin:       bytes | None = None
    env:         dict[str, str] | None = None
    timeout_s:   float = 5.0
    workdir_files: dict[str, bytes] | None = None    # ≤256 KB total
    max_stdout_bytes: int = 1_048_576                # 1 MB
    max_stderr_bytes: int = 1_048_576
    request_id:  str | None = None                   # idempotency

@dataclass
class InvocationResult:
    exit_code:   int
    stdout:      bytes      # truncated to max_stdout_bytes; tail dropped
    stderr:      bytes
    truncated:   dict[str, bool]   # {"stdout": True/False, "stderr": ..., "result": ...}
    duration_s:  float
    executor_id: str
    error:       str | None        # set on InvokeRejected / InvokeTimeout / ExecutorCrashed
```

### Pool lifecycle and accounting

- Each node registers `FunctionExecutorPoolSpec` instances at
  startup; pools count toward node capacity (spec 10) — an
  executor consumes its template's `cpu_limit` / `mem_limit` like
  any sandbox. The scheduler treats `(node, template, "func")`
  capacity as `pool_size` invocation slots.
- On invocation arrival, the node agent picks an idle executor or
  enqueues to `max_pending`. Past `max_pending`, returns
  `InvokeRejected("pool_full")` — the SDK retries on a peer node
  via the control plane.
- After every invocation, the executor wipes its tempdir;
  `recycle_after_invocations` and `recycle_after_idle_s` cap
  long-term drift (memory leaks, cached imports, stray files in
  paths outside tempdir).
- `request_id` deduplicates retries for the invocation lifetime
  (60 s) — the same `request_id` arriving twice returns the cached
  `InvocationResult`.

### Security and audit

`trust_level` is mandatory in the manifest (`execution_mode:
function-call` requires it). Two values:

- `trusted-only` — the template is for code the operator trusts
  (a math evaluator, a known judge, an in-house tool). The
  platform refuses to dispatch agent-generated commands to this
  pool unless the trainer explicitly opts in.
- `untrusted-allowed` — the template authors accept the weaker
  isolation (shared interpreter, shared kernel) and the operator
  has acknowledged the risk in the manifest. Documented as
  unsuitable for adversarial code.

Every invocation is recorded as a structured log event
`function.invoke` (request_id, template, exit_code, duration_s,
stdout/stderr sizes, truncation flags) and surfaces in the
trajectory of the rollout that issued it (when invoked from inside
an EnvAdapter) under `info["function_calls"]`.

### Metrics

`xrlenv_function_invocations_total{template,result}`,
`xrlenv_function_invoke_seconds{template}`,
`xrlenv_function_pool_pending{node,template}`,
`xrlenv_function_executor_recycles_total{template,reason}` —
mirrored in spec 08.

### Trade-off: less isolation across calls

Function-Call mode shares one executor process across many calls.
That means:

- Same kernel, same image, same Python interpreter (when applicable)
  across calls.
- No filesystem isolation between calls — only tempdir wipe at end
  of each call. A misbehaving invocation that writes to `/tmp/foo`
  outside its wipe path can leak to the next call.
- Memory leaks accumulate inside the executor; the pool periodically
  recycles executors after `recycle_after_invocations` (default
  10000) or `recycle_after_idle_s` (default 300).

For untrusted code: stay on `sandbox` mode (Docker) or
`sandbox`+Cube (microVM). For agent-tool code that's known-safe:
`function-call` is significantly faster and cheaper.

## Local-process-debug adapter (debug only, phase 0)

A **no-isolation** backend that runs commands as plain subprocesses
on the host with a per-sandbox `tempfile.mkdtemp()` working
directory. There is no chroot, no namespace, no cgroup — the
sandbox shares the host filesystem, network, processes, and user
identity. Used in unit tests for the trainer SDK and for very fast
laptop iteration where isolation isn't the goal. **Trusted code
only**; never deployed; never used for agent-generated commands.

Capability surface: `supports_snapshot=False`,
`supports_chainable_snapshot=False`, `live_state_captured=False`,
`supports_port_forward=False`, `isolation_class="none"` so any
template that requires real isolation refuses placement on this
backend.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: `docker`, `local-debug`. Snapshot is best-effort
  (`docker commit`). No port forward in default templates.
- **Phase 1**: `cubesandbox` lands. Port forward exposed. Snapshot
  becomes first-class for Cube templates. Egress allowlists wired
  through (see [07-networking.md](07-networking.md)).
- **Phase 2**: `firecracker` direct (sub-Cube layer if needed),
  `k8s-pod` (one pod per sandbox or pods-as-nodes).
