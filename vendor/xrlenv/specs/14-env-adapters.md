# 14 — Env Adapters

## Scope (under the slim pivot)

`EnvAdapter` is the **case-1 mechanism**: it carries the wire
contract for **RL training** workloads where the trainer drives an
`act → obs` step loop end-to-end. The pre-slim-pivot draft of this
spec positioned EnvAdapter as the universal mechanism for all
benchmark integration; the slim pivot (P1.7) revised that:

- **case-2** (docker-py-shaped evaluation harnesses, e.g. SWE-bench)
  plug in via the docker-py drop-in
  (`xrlenv.compat.docker_client.from_env`); see spec 01 §"In-sandbox
  primitives" + the docker-py drop-in shape doc at
  `docs/integration/evaluation/supported_benchmarks_and_harnesses/swe-bench.md`.
- **case-3** (harness frameworks with their own `BaseEnvironment`
  Protocol, e.g. harbor) plug in via per-framework adapters
  (`xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster`); see
  `docs/integration/evaluation/supported_benchmarks_and_harnesses/harbor.md`.
- **case-1** (RL training, this spec) keeps the EnvAdapter Protocol
  as the wire contract.

The Protocol below applies to case-1 only. Case-2/3 specs live
outside this document.

## Purpose

A case-1 RL training benchmark typically has its own Python
`Environment` class — gym-shaped, with `reset()` / `step(action)` /
domain-specific action and observation spaces. Examples in scope
for case-1:

- A custom gym env that wraps a long-horizon agent task.
- A simulator wrapper (kitchen, web-shop, etc.).
- A user's own `Environment` class for a custom agent task.

We do **not** want to reimplement these. They encode benchmark-
specific semantics (action grammars, observation formats, scoring
rules) that the benchmark authors maintain. The right design is to
**wrap them**: an `EnvAdapter` is a thin shim that lifts an existing
Environment class into XRLEnv's step protocol.

This also means a user can bring their **own** custom Environment
class for a custom agent task without touching XRLEnv's core, just by
shipping a Python class that implements the adapter protocol.

## The protocol

```python
class EnvAdapter(Protocol):
    """Lives inside the sandbox alongside the stub. Wraps a benchmark
    Environment class (or a user-defined one) and exposes a uniform
    step interface."""

    # Declares which RewardContract modes (spec 02) this adapter can
    # serve. The catalog rejects a template that pairs an adapter
    # with an unsupported mode.
    supported_reward_modes: ClassVar[set[str]]   # subset of
        # {"env_step", "in_sandbox_final", "consumer_final",
        #  "external_final", "token_level"}

    async def setup(self, init_params: dict) -> Observation:
        """One-time init at sandbox creation. Returns the first obs."""

    async def step(self, action: Action) -> StepResult:
        """Apply action, return (obs, reward, done, info, truncated).

        For modes other than `env_step` / `token_level`, the adapter
        returns reward=0.0 every step; the final scalar is computed
        by whichever side owns it (in-sandbox cmd, trainer, external
        service) per spec 02's RewardContract.
        """

    async def teardown(self) -> None:
        """Cleanup before sandbox destroy."""

    async def time_left(self, soft_deadline_s: float) -> None:
        """Optional: emit a soft-deadline signal into the next obs."""

    @property
    def action_space(self) -> Any: ...
    @property
    def observation_space(self) -> Any: ...

    @classmethod
    def capabilities(cls) -> "AdapterCapabilities":
        """Introspection so the stub can validate compatibility at setup."""

@dataclass
class AdapterCapabilities:
    xrlenv_api_version_supported: list[str]      # e.g. ["1.0", "1.1"]
    supported_reward_modes:       set[str]        # subset of spec 02 modes
    supports_resume:              bool = False    # spec 18 sessions
    supports_enumerate_tasks:     bool = False    # spec 16 benchmark analysis
    supports_binary_refs:         bool = False    # spec 14 BlobRef streaming
    supports_soft_deadline_signal: bool = False
```

The stub calls `capabilities()` after dynamic import and before
`setup`; mismatches are reported as
`RolloutFailed("adapter_incompatible:<field>")`. This catches the
common "user built the image with an old adapter against a newer
platform" failure at setup time rather than mid-rollout.

`Action` and `Observation` are *opaque* to XRLEnv core. They round-trip
through the trainer SDK as JSON-serializable payloads (or msgpack if
binary, e.g. screenshots). The agent and the EnvAdapter agree on the
shape; XRLEnv only routes bytes.

## Where it runs

Inside the sandbox, loaded by the in-sandbox stub at startup:

```
sandbox process tree
└── tini (PID 1)
    └── stub-server.py
        ├── exec / files / spawn_service primitives  ← spec 01
        └── EnvAdapter instance                      ← THIS spec
            └── (wraps) Terminal / DesktopEnv / SWEBenchHarness / ...
```

A new stub endpoint:

- `POST /env/setup` — calls `adapter.setup(init_params)`, returns first obs.
- `POST /env/step` — calls `adapter.step(action)`, returns `StepResult`.
- `POST /env/teardown` — calls `adapter.teardown()`.

The trainer-side `RolloutSession.step(action)` becomes a thin RPC over
this endpoint, transparent to the user.

**Why in-sandbox, not in the coordinator**: most benchmark Environment
classes hold local state (tmux panes, X controllers, browser handles)
that must live next to the workload. Calling them from the coordinator
would force every observation back to the orchestrator, defeating the
sandbox isolation and hammering the network with screenshots.

## Built-in adapters

Each is in `xrlenv/envs/<name>.py` with its dependency declared in
`xrlenv/envs/<name>/requirements.txt` so users only install what their
templates need.

Reward mode declarations (spec 02 RewardContract) per built-in adapter:

| Adapter | `supported_reward_modes` |
|---|---|
| `TerminalEnvAdapter` | `{in_sandbox_final, env_step}` (terminal-bench grader runs at done by default; `env_step` allowed for shaped per-step rewards) |
| `SWEBenchEnvAdapter` | `{in_sandbox_final}` (`pytest` + grader at done) |
| `DesktopEnvAdapter` | `{in_sandbox_final, consumer_final}` (OSWorld per-task graders, optional LLM-judge final-reward) |
| `ShellEnvAdapter` | `{in_sandbox_final, consumer_final, external_final}` (delegated to template's `RewardContract`) |
| `GymEnvAdapter` | `{env_step}` (classical gym semantics) |

The catalog enforces that the template's `reward.mode` is in the
adapter's `supported_reward_modes` at register time.

### `xrlenv.envs.terminal_bench.TerminalEnvAdapter` (phase 0)

Wraps `terminal_bench.terminal.Terminal`. `setup` constructs the
`Terminal` instance with the requested task config. `step(action)`
where `action: str` is a shell command — calls
`Terminal.send_keys(action)` and reads the resulting pane buffer for
the observation. Reward is computed by terminal-bench's grader at
`done`.

### `xrlenv.envs.swebench.SWEBenchEnvAdapter` (phase 0)

Wraps SWE-bench's per-instance harness. `setup(init_params)` clones
the relevant repo, applies the prepatch, identifies the test list.
Action is one of: `{type: "shell", cmd: "..."}` or
`{type: "patch", diff: "..."}`. Observation is the most recent shell
output or patch-apply result. `done` triggers `pytest`; reward is the
fraction of `FAIL_TO_PASS` tests that now pass.

### `xrlenv.envs.osworld.DesktopEnvAdapter` (phase 0)

Wraps `desktop_env.DesktopEnv`. `setup` boots the OSWorld
infrastructure (Xvfb, controller, target apps). Action is the OSWorld
action grammar (`{type: "pyautogui", code: "..."}` or
`{type: "click", x: 100, y: 200}`). Observation is a packed
`{screenshot: bytes, a11y_tree: dict}`. Reward is OSWorld's per-task
grader at `done`.

### `xrlenv.envs.shell.ShellEnvAdapter` (phase 0, generic)

For one-off custom envs that just want "send shell command, get
output back." Action = command string, observation = stdout, reward
delegated to the template's `RewardContract` (spec 02). Useful as a default for new
agent tasks before a dedicated adapter is written.

### `xrlenv.envs.gym.GymEnvAdapter` (phase 0, generic)

Wraps any class conforming to `gym.Env` / `gymnasium.Env`. Action and
observation pass through as the env declares them; the only
constraint is JSON or msgpack serializability. Useful for wrapping
research code with minimal glue.

## Wrapping sync / non-thread-safe upstream envs

Most benchmark Environment classes (OSWorld's `DesktopEnv`,
terminal-bench's `Terminal`, SWE-bench harnesses, classical gym
envs) are **sync** and often **thread-affine** — they hold X11
connections, GIL-heavy native objects, controller handles, or
internal threads that don't tolerate cross-thread access.

The architectural shield is **process isolation**: every sandbox is
its own process (Docker container or microVM). One Environment
instance per sandbox; many sandboxes = many processes; no shared
state across sandboxes regardless of their thread safety. The only
place thread safety matters is *inside* the sandbox stub when it
drives the env.

### `SyncEnvAdapter` base class (recommended pattern)

For wrapping a sync upstream class, adapter authors should inherit
from `SyncEnvAdapter`, which:

- Holds the upstream env as `self._env`.
- Pins all env calls to a **single dedicated worker thread** per
  sandbox: `ThreadPoolExecutor(max_workers=1,
  thread_name_prefix=f"env-{sandbox_id}")`. Single-thread is
  important — even thread-pool=2 breaks thread-affine envs.
- Bridges async ↔ sync via `asyncio.to_thread` (or
  `loop.run_in_executor(self._exec, ...)`), so the wire-level
  `setup` / `step` / `teardown` are async while the underlying
  calls run sequentially on the pinned thread.
- Wraps every call with a timeout sourced from the rollout's
  `Deadline` (spec 02 per-phase overrides). A hung sync call would
  otherwise block the stub forever; with a timeout the adapter
  returns a `step_timeout` result and the rollout can be
  hard-deadlined cleanly by the coordinator.

```python
class SyncEnvAdapter(EnvAdapter):
    """Base class for wrapping a sync, possibly thread-affine env."""

    def __init__(self, *, sandbox_id: str):
        self._exec = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"env-{sandbox_id}"
        )
        self._env = None              # subclass sets in setup()

    async def _call(self, fn, *args, timeout_s: float, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._exec, lambda: fn(*args, **kwargs)),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            raise StepTimeout(...)

    async def setup(self, init_params):
        return await self._call(self._do_setup, init_params,
                                timeout_s=init_params.get("init_timeout_s", 120))

    async def step(self, action):
        return await self._call(self._env.step, action,
                                timeout_s=...)

    async def teardown(self):
        await self._call(self._env.close,
                         timeout_s=30)
        self._exec.shutdown(wait=True)
```

OSWorld's `DesktopEnvAdapter`, terminal-bench's
`TerminalEnvAdapter`, and the generic `GymEnvAdapter` all derive
from `SyncEnvAdapter`. Adapter authors implement `_do_setup` and
optionally override `step` / `teardown` for sink-specific
conversion; the threading and timeout machinery is inherited.

### When the upstream env is async-native

If an Environment class is genuinely async-native (rare today, will
become more common), the adapter inherits from `EnvAdapter`
directly — no executor needed, just plumb the awaitables.

## Adapter contract for benchmark analysis (phase 1)

Adapters that wrap a benchmark suite (terminal-bench-2, SWE-bench,
OSWorld) implement two additional methods so the analysis tool
(spec 16) can ingest the benchmark in its native format and the
resolver can dispatch consolidated or upstream images interchangeably:

```python
def enumerate_tasks(self) -> Iterator[TaskDescriptor]:
    """Yield every task in the benchmark, in its native ordering.

    Adapter-specific: terminal-bench-2 walks task.toml files;
    OSWorld reads its config JSONs; SWE-bench iterates dataset.json
    rows. This is the only step in the analysis pipeline where the
    benchmark's native format matters; everything downstream operates
    on the normalized TaskDescriptor.
    """

def resolve_instance(self, task_id: str,
                     plan: "Plan | None" = None) -> ResolvedInstance:
    """Resolve a single task to a concrete sandbox spec.

    plan=None  -> faithful upstream behavior (per-task image).
    plan given -> dispatch by the consolidation plan (spec 16).
    """
```

`TaskDescriptor`, `ResolvedInstance`, and `Plan` shapes are defined
in spec 16. Adapters that wrap a single Environment class with no
task suite (a generic shell env, a one-off custom task) don't need to
implement these — the analysis tool simply skips them.

## Custom adapters

A user with their own Environment class declares it in their template:

```yaml
# xrlenv/templates/my_custom_task/template.yaml
name: my-custom-task
image: { ref: "myorg/my-custom-task:0.1", pre_pull: true }

env_adapter:
  module: my_custom_task.adapter      # importable inside the sandbox image
  class:  MyTaskEnvAdapter
  init_params:                        # passed to adapter.setup()
    task_difficulty: "hard"
```

The adapter class is a Python module shipped *in the sandbox image* —
not in XRLEnv core. The image ships the benchmark code (or the user's
custom code) plus any deps. The stub imports it dynamically at
`/env/setup` time, validates it satisfies the `EnvAdapter` protocol,
and starts driving it.

This means:

- XRLEnv core stays small. It doesn't need to know about every
  benchmark.
- Adding a new benchmark = write a ~100-line adapter + a Dockerfile
  pinning the benchmark's deps. No changes to the orchestrator.
- The user's custom RL task = write a custom Environment class +
  adapter, pin dependencies in the image, register the template. No
  fork required.

## Template integration

The `env_adapter:` block replaces the earlier `step.driver` / `step.protocol`
fields in `template.yaml`. Concretely, every template carries:

```yaml
env_adapter:
  module: ...           # python import path inside the sandbox
  class:  ...           # class name implementing EnvAdapter protocol
  init_params: {...}    # static config passed to setup()
  action_space:         # optional, advertised to the SDK for typing
    type: "json-schema"
    schema: {...}
  observation_space:    # optional, advertised to the SDK
    type: "json-schema"
    schema: {...}
```

When the trainer asks the SDK
`client.action_space(template_id) → Schema`, the SDK returns these
declared schemas. Useful for type-checking agent code; not strictly
required.

## Action / observation transport

### Inline vs reference

- **Inline payloads** — text, structured ops, and small bytes
  fields (default threshold 64 KB). Carried as JSON / msgpack
  fields in the step request/response.
- **`BlobRef` payloads** — anything above the inline threshold
  (screenshots, files, qcow2 diffs). The adapter returns a
  `BlobRef` in `info` / `obs` instead of the bytes; the bytes
  travel separately as a chunked stream.

### `BlobRef` schema

```python
@dataclass
class BlobRef:
    blob_id:    str        # UUIDv7 generated by the stub at write time
    media_type: str        # "image/png", "image/jpeg", "application/octet-stream", ...
    size:       int        # bytes
    sha256:     str        # hex
    uri:        str        # "blob:<rollout_id>/<step>/<blob_id>" (locator-only)
    step:       int        # 0-based step index this blob attaches to
    field:      str        # e.g. "obs.screenshot", "info.test_log"
    inline_after_size: bool = False    # if True, viewer/replay materializes inline
```

`uri` is a logical locator — it is resolved by the spec-17 viewer
and `client.replay` through the same `TrajectoryReader` plugin
that reads the trajectory body. The bytes themselves live as
sidecars under `~/.xrlenv/runs/<date>/<rollout_id>/blobs/` (spec
20) and stream over the existing trajectory-fetch RPC (spec 21).

### Inline threshold and limits

- `inline_threshold_bytes`: default 64 KB. Tunable per-rollout
  via `client.rollout(..., inline_threshold_bytes=...)` or per-
  template via `observability.inline_threshold_bytes`.
- `max_blob_size_bytes`: 256 MB by default. Larger blobs raise
  `BlobTooLarge` at the stub; the adapter is expected to chunk
  the payload itself (e.g. multi-step delivery of a big asset).
- `max_blobs_per_step`: 32 by default. Above this the stub
  rejects the step result with `TooManyBlobs`.

### Lifetime and retention

- A `BlobRef` lives as long as its rollout's run dir does (spec
  09 layer 4 — `retention_days`, default 14). Phase 2 mirrors
  the `blobs/` subdirectory to object storage at seal.
- `client.rollout(..., include_binary=False)` (default) returns
  trajectories with `BlobRef`s present but bytes not loaded; the
  trainer fetches on demand via `traj.fetch_blob(blob_ref)`.
- `include_binary=True` materializes all blobs into memory at
  fetch — only suitable for small-blob templates or for replay
  of curated trajectories.

### Trajectory serialization

The platform-jsonl sink writes `BlobRef`s inline in the step
record (just the metadata, not the bytes). The bytes are written
as a separate file in `blobs/<blob_id>.<ext>` at the same time
the step is appended. A reader that wants the bytes either:

1. Resolves the `BlobRef.uri` locally (file:// path under the
   run dir).
2. Calls the spec-21 `FetchTrajectoryCommand` with
   `include_binary=true`; chunks return interleaved with step
   records.

Slime / verl native sinks may or may not preserve `BlobRef`s —
spec 08's durability matrix shows whether each sink supports the
"native raw download" column. When a native sink does not, the
SDK warns at adapter init that binary blobs will be dropped on
write.

### Wire format

- **Stub → control plane**: chunked binary over the existing
  uds/vsock transport. 1 MB chunks; checksum (sha256) computed on
  the stub side and verified on the control-plane side.
- **Control plane → viewer / SDK**: chunks ride spec-21
  `TrajectoryChunk` messages on the reverse stream; the viewer
  multiplexes per-blob fetches via `command_id` (spec 21).

Phase 0 ships HTTP/1.1 chunked over uds/vsock between stub and
node agent; phase 1 considers HTTP/2 / gRPC if profile shows blob
ser/de is the bottleneck.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: protocol; built-in adapters for terminal-bench,
  SWE-bench, OSWorld; generic `ShellEnvAdapter` and `GymEnvAdapter`;
  custom-adapter loading via `template.yaml`.
- **Phase 1**: web-search adapter (wraps a Playwright/Chromium
  controller as an Environment class); soft-deadline signal in obs;
  per-step latency budget so a slow adapter can't starve the
  scheduler's batch.
- **Phase 2**: snapshot/branch hooks in the protocol
  (`adapter.snapshot()` / `adapter.restore(...)`); multi-agent group
  envs.

## Deferred: GEM protocol support

[axon-rl/gem](https://github.com/axon-rl/gem) (`pip install gem-llm`) is
an LLM-focused gym variant: `gem.make(id)` returns gymnasium-shaped envs
across math / games / QA / reasoning / code categories, with optional
tool wrappers (Python, Search, MCP). Its API matches our `EnvAdapter`
shape directly (5-tuple step, `reset → (obs, info)`).

A `xrlenv.envs.gem.GEMEnvAdapter` would be a thin `gem.make(id)`
wrapper. Adding it is straightforward; what makes the broader story
worth more design work — and is the reason this is deferred rather
than implemented in phase 0 — is that **most GEM envs are trusted
pure-Python**, so running each in a Docker container (with a stub
server) is wasteful at the 10k+ concurrent scale GEM is designed for.
Doing GEM justice means promoting the spec-01 `local-process` backend
from debug-only to first-class for trusted lightweight templates, plus
a `requires_isolation: false` flag on the template manifest, plus
deciding between process-per-rollout vs adapter-instance-per-rollout
in a shared host process. We will revisit when there's a concrete
need for GEM workloads on this platform.

Until then: a user who wants GEM today can write a custom EnvAdapter
that does `gem.make(id)` internally and ship it inside their template's
Docker image — no XRLEnv core change required, just the standard
custom-adapter path.
