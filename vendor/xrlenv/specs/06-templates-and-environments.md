# 06 — Templates and Environments

## Purpose

A *template* is the declarative description of an environment kind:
what image to run, what resources it needs, how it accepts actions and
emits observations, how reward is computed. Templates are the unit
trainers reference (`Template.SWE_BENCH`); the catalog binds names to
manifests.

## Shared base images

Phase 0 ships a small set of canonical base images that every template
should derive from. This maximizes Docker's automatic layer
deduplication on each node — pulling `terminal-base` after `swebench-base`
reuses every layer up to the diverging point, instead of pulling the
universe twice.

```
xrlenv/templates/_base/
├── ubuntu22/Dockerfile          # ubuntu 22.04 LTS + tini + the in-sandbox stub binary
├── python311/Dockerfile         # FROM xrlenv-base-ubuntu22 + python 3.11 + uv
└── desktop22/Dockerfile         # FROM xrlenv-base-ubuntu22 + Xvfb + GUI deps
```

Phase 0 templates derive from these:

- `terminal-base` ← `xrlenv-base-ubuntu22`
- `swebench-base` ← `xrlenv-base-python311`
- `osworld-base`  ← `xrlenv-base-desktop22`

This is convention, not a hard requirement (a user can ship a fully
custom Dockerfile if they need to), but the catalog warns when a
registered template doesn't derive from any base — that's nearly
always a layer-dedup waste.

## `template.yaml` schema

```yaml
name: swebench-base
version: 1
xrlenv_api_version: "1.0"              # version of the platform spec the manifest targets
required_backends: [docker]            # subset of node's backends
isolation: container                    # informational; "container" | "microvm"

resources:
  cpu_request: 4.0
  cpu_limit:   4.0
  mem_request: 8GB
  mem_limit:   8GB
  disk_request: 30GB
  gpu_required: false
  mounts:                                # host bind-mounts (spec 01)
    - host_path: /opt/xrlenv-shared/agents/claude-code
      sandbox_path: /opt/agent
      readonly: true

network_policy: none                    # none | egress-allowlist | open
egress_allowlist: []                    # used when policy = egress-allowlist

execution_mode: sandbox                  # sandbox | function-call (spec 01)

image:
  ref: "ghcr.io/xrlenv/swebench-base:0.1"
  pre_pull: true                        # node-agent fetches at registration
  lazy_load: disabled                   # disabled | preferred | required (phase 1; spec 15 eStargz / overlaybd lazy mount)

init:
  cmd: ["/opt/xrlenv/init-swebench.sh", "--instance", "{{init.instance_id}}"]
  timeout_s: 120

env_adapter:                            # see spec 14
  module: xrlenv.envs.swebench
  class:  SWEBenchEnvAdapter
  init_params:
    test_command: "pytest -x"
  # action_space / observation_space are optional schema declarations
  # the EnvAdapter publishes for type-checking on the consumer side.

image_builder:                          # P1.6 — optional; control-plane-driven image builds
  # Plug-in's :class:`BenchmarkImageBuilder` (xrlenv/control/image_builder.py).
  # When set, ``xrlenv build apply build-plan.yaml`` can dispatch
  # per-image build jobs to node-agents that load this builder via
  # :func:`load_image_builder` and run it in-process. Optional —
  # plug-ins that omit this block fall back to operator-side
  # ``build-task-images.sh`` workflows. Plug-ins that ship a builder
  # MUST also document the builder's static ``IMAGE_SIZE_HINT_BYTES``
  # so the bin-packer can budget per-node disk before any image is
  # actually built.
  module: xrlenv_plugins.benchmarks.<your_plugin>.image_builder
  class:  YourImageBuilder

reward:
  mode:    in_sandbox_final             # spec 02 RewardContract; one of:
                                         #   env_step | in_sandbox_final | consumer_final | external_final | token_level
  cmd:     ["pytest", "--exitcode-as-reward"]
  timeout_s: 60
  on_error: fail_rollout                # fail_rollout | zero_reward | partial

deadline:
  default_soft_s: 540
  default_hard_s: 600
  ttl_s: 3600
  idle_ttl_s: 120                       # client must touch within this window
  # Per-phase budgets; per-rollout overrides (spec 02 Deadline) win over these.
  init_timeout_s: 120
  step_timeout_s: 90
  reward_timeout_s: 60
  teardown_timeout_s: 30
  image_pull_timeout_s: 600             # SWE-bench-style large-image cases

services:                                # long-lived in-sandbox processes (spec 01 ServiceSpec shape)
  # Example for an OSWorld-style template:
  # - name: x_server
  #   cmd: ["Xvfb", ":99", "-screen", "0", "1280x1024x24"]
  #   health_check: ["xdpyinfo", "-display", ":99"]
  #   startup_timeout_s: 10
  # - name: browser
  #   cmd: ["chromium", "--headless=new", "--remote-debugging-port=9222"]
  #   port: 9222
  #   depends_on: [x_server]
  #   health_check: ["curl", "-fsS", "http://localhost:9222/json/version"]
  #   restart: on_failure

warm_pool:                              # phase 1
  min_idle: 2
  max_idle: 8

snapshot_required: false

observability:
  # Which TrajectorySink writes per-step records. See spec 08.
  # Default: platform-jsonl. Trainer adapters (Slime, verl) install
  # their own sinks at adapter init; the *adapter default* is
  # `multi:[platform-jsonl, <native>]` so the platform-jsonl shadow
  # is always written unless the operator explicitly opts out via
  # `allow_native_only_trajectory_sink: true` below.
  trajectory_sink: platform-jsonl       # platform-jsonl | none | slime-sample | verl-dataproto | multi:[...]
  trajectory_sink_pin: false            # true to refuse adapter overrides
  allow_native_only_trajectory_sink: false   # true permits Slime/verl native-only (drops platform-jsonl shadow); cite the durability matrix in a comment when flipping
```

Manifests are validated at load time. Unknown fields are rejected (no
silent drift). Versioning is explicit for forward-compat.

### `xrlenv_api_version` and adapter compatibility

The control plane and node agent each carry a build-time
`platform_api_version` ("1.0", "1.1", ...). At template register:

- Manifests with a missing `xrlenv_api_version` are rejected
  (operators must set it explicitly so future skew is visible).
- A manifest's `xrlenv_api_version` must satisfy
  `manifest_major == platform_major` (semver-like) or registration
  fails. Minor-level mismatches register with a warning.

At sandbox setup, the in-sandbox stub additionally calls the
EnvAdapter's `capabilities()` (spec 14) and verifies:

- The adapter's reported `xrlenv_api_version_supported` includes
  the platform's.
- The adapter declares `supported_reward_modes` that include the
  template's `reward.mode`.
- For session-bearing templates: the adapter declares
  `supports_resume=True`.
- For analysis-bearing templates: `supports_enumerate_tasks=True`.

A mismatch surfaces as `RolloutFailed("adapter_incompatible:<reason>")`
*at setup time* rather than as an opaque crash mid-rollout.

## Phase 0 templates

### `terminal-base`
- Image: Ubuntu 22.04 + common CLI tools (`coreutils`, `git`, `curl`,
  `python3`, `nodejs`, `tmux`) + terminal-bench's `Terminal` Python
  package installed.
- Harness: terminal-bench's evaluation harness vendored in
  `/opt/terminal-bench/`.
- Resources: 2 CPU / 4 GB / 5 GB disk.
- Network: `none`.
- EnvAdapter: `xrlenv.envs.terminal_bench.TerminalEnvAdapter` wraps
  `terminal_bench.terminal.Terminal`. Action = shell command string;
  observation = terminal pane buffer. Spec 14.
- Reward: `mode: in_sandbox_final` (terminal-bench's grader).

### `swebench-base`
- One *outer* template using the **Pattern A `instances:` resolver**
  (see "Importing existing benchmark task suites" below). SWE-bench's
  convention is one image per task instance; the resolver maps each
  `instance_id` to its image ref + per-instance resources, and the
  Image Cache Manager (spec 15) handles pulling on demand or via
  `client.warmup(...)`.
- Resources: 4 CPU / 8 GB / 30 GB disk.
- Network: `none`.
- EnvAdapter: `xrlenv.envs.swebench.SWEBenchEnvAdapter`. Action is
  one of `{type: "shell", cmd}` or `{type: "patch", diff}`;
  observation is the most recent shell output or patch-apply result.
- Reward: `mode: in_sandbox_final` (`pytest` + SWE-bench grader).
- **Note**: per-instance images are large (1–4 GB each, hundreds of
  instances). Phase 0 pre-pulls a configurable subset at template
  registration; everything else is lazy. Capacity estimator's disk
  budget accounts for the warm-cache.

### `osworld-base`
- Image: OSWorld's reference Docker image (Ubuntu desktop + Xvfb +
  X11vnc + the OSWorld controller + target apps).
- Resources: 4 CPU / 8 GB / 15 GB disk.
- Network: `open` (OSWorld tasks visit real sites; phase 1 will tighten
  with allowlists).
- EnvAdapter: `xrlenv.envs.osworld.DesktopEnvAdapter` wraps OSWorld's
  `desktop_env.DesktopEnv`. Action grammar is OSWorld-native
  (`pyautogui` code or structured `{click, type, scroll, key}`);
  observation is `{screenshot, a11y_tree}`.
- Reward: `mode: in_sandbox_final` (per-task grader scripts shipped with OSWorld).
- Services: a `screencap` long-lived process that renders the X
  framebuffer (managed by `DesktopEnv` itself).
- **Caveat**: OSWorld's GUI workload is the limit of what shared-kernel
  Docker buys you. Marked as a phase-1 candidate to migrate to
  CubeSandbox for stronger isolation.

## Phase 1 templates

### `web-search-base`
- Image: minimal + Chromium + a persistent `browser-service` (one
  Chrome process per sandbox, reused across steps, not torn down per
  action).
- Resources: 2 CPU / 4 GB / 5 GB disk.
- Network: `egress-allowlist`.
- Action protocol: navigate / click / type / read-dom / extract.
- Reward: trainer-side (LLM judge over trajectory) by default;
  per-task overrides allowed.

## Phase 2 templates

### `deep-research-base`
- Image: persistent research workspace (paper PDFs, code, notes).
- Resources: 8 CPU / 32 GB / 100 GB persistent disk.
- Network: `egress-allowlist` with broader allowlist (arxiv, GitHub,
  pip, npm).
- Action protocol: rich tool set (search, exec_python, read_file,
  write_file, browser, web_fetch).
- Reward: external service (rubric-based judge) + final-state diff.
- Snapshot required: true (long rollouts need fork/branch for credit
  assignment).

## Custom templates with user-defined Environment classes

A user with their own task-specific Environment class doesn't need to
modify XRLEnv. They:

1. Write a Python class implementing the `EnvAdapter` protocol
   (spec 14).
2. Build a Docker image that pip-installs their code + deps.
3. Drop a `template.yaml` referencing it:
   ```yaml
   name: my-task
   image: { ref: "myorg/my-task:0.1" }
   env_adapter:
     module: my_task.adapter
     class:  MyTaskEnvAdapter
     init_params: {...}
   resources: {...}
   reward: { mode: in_sandbox_final, cmd: [...], timeout_s: 60 }
   ```
4. `xrlenv template register ./templates/my-task/template.yaml`.

Their template now appears in the catalog and can be referenced by any
trainer. No XRLEnv code change.

### Plug-in resolution and the in-sandbox import path

External adapter modules reach the sandbox via two mechanisms:

1. **In-tree** — the `xrlenv_plugins/` namespace package next to the
   imported `xrlenv` package. The Docker backend bind-mounts it at
   `/opt/xrlenv-pkg/xrlenv_plugins`; `PYTHONPATH=/opt/xrlenv-pkg`.
2. **External pip-package or filesystem checkout** — discovered via
   the `xrlenv.benchmarks` entry-point group or the
   `XRLENV_TEMPLATE_DIRS` env var (B11). For each external manifest
   that lives under an `xrlenv_plugins/` ancestor, the runtime
   bind-mounts that ancestor's parent at `/opt/xrlenv-extras/<idx>`
   (read-only, indexed positionally) and prepends the mount target to
   `PYTHONPATH`. Multiple external roots stack on PYTHONPATH; PEP-420
   namespace-package semantics merge their `xrlenv_plugins/`
   contributions so `import xrlenv_plugins.benchmarks.<name>.adapter`
   resolves natively.

Operators don't choose between in-tree and external — both register
through the same catalog, both reach the sandbox the same way. See
`docs/integration/overview.md` for the publishing recipes.

## Pattern: expensive install once, snapshot, restore per rollout

For a template whose init runs an expensive install step (e.g.
`npm install -g <agent>`, `apt install <large-package>`,
`pip install -r requirements.txt` against a 500-line lockfile), paying
that cost on every sandbox creation is wasteful when 1000 rollouts will
hit the same path. Two answers in our existing toolbox:

1. **Bake into the template image** at *build* time. Right answer for
   anything stable. Use a multi-stage Dockerfile that installs deps in
   a layer and freezes them.
2. **Snapshot after install, restore per rollout** (spec 01's
   `snapshot` / `restore`). Right answer when the install depends on
   per-cluster secrets, license tokens, or a host-specific binary
   bind-mount that can't live in the image. The flow:
   - First sandbox of the template runs `init.cmd` end-to-end and
     completes the install.
   - The node agent calls `backend.snapshot(sb) → SnapshotID`,
     persists the ID against the template.
   - Subsequent sandboxes are created via `backend.restore(snapshot)`,
     skipping init entirely. Cheap on Cube; best-effort on Docker
     (`docker commit`-based, see spec 01 caveats).

Templates opt into pattern 2 with:
```yaml
init:
  cmd: [...]
  timeout_s: 300
  cache_via_snapshot: true       # snapshot after first successful init
```

## Pattern: shared package cache (phase 1)

Repeated rollouts in the same template often re-download identical pip
wheels, npm tarballs, or apt packages. A bind-mount of a node-shared
cache eliminates that waste. **Per-template opt-in**, gated by spec
19's `cache_trust_mode` because shared caches across untrusted
sandboxes are a poisoning vector. Defaults:

```yaml
cache_trust_mode: readonly-prebaked     # readonly-prebaked | rw-trusted-template | rw-untrusted-disabled
```

- `readonly-prebaked` (default for any new template) — cache is
  mounted read-only; populated by `xrlenv cache prefetch <template>`
  on the operator side.
- `rw-trusted-template` — read-write; allowed only when the
  manifest is signed (phase 1) by a publisher in the operator's
  trust list.
- `rw-untrusted-disabled` — sandbox sees no shared cache; package
  manager downloads run inside the sandbox each time. Auto-set
  for any template with `network_policy: open` and any template
  loaded from a public registry without a signed manifest.

Safe to share read-write *only* under `rw-trusted-template`
(lockfile-based, concurrency-safe package managers):
```yaml
resources:
  mounts:
    - host_path: /var/cache/xrlenv/pip
      sandbox_path: /root/.cache/pip
      readonly: false
    - host_path: /var/cache/xrlenv/uv
      sandbox_path: /root/.cache/uv
      readonly: false
    - host_path: /var/cache/xrlenv/npm
      sandbox_path: /root/.npm
      readonly: false
```

**Not safe** to share read-write (no concurrency story):
- `/var/cache/apt` — apt's lock files don't span hosts/sandboxes well.
  If you need apt-package sharing, prefer baking into the image at
  build time.
- `~/.cargo/registry` — cargo's locking is process-local; concurrent
  writes can corrupt the index.

The node agent provisions the host paths at startup
(`mkdir -p /var/cache/xrlenv/{pip,uv,npm}`) with appropriate
permissions; templates just declare the mount.

## Pattern: shared SWE-bench instance image cache (phase 1)

SWE-bench's per-instance images (1–4 GB each, hundreds of instances)
are the workload most affected by node-disk pressure. When the cluster
operator has shared storage available (NFS, EFS, GCS-FUSE), the
recommended pattern is a *node-shared, possibly cluster-shared*
docker layer store:

1. Mount shared storage at `/var/cache/xrlenv/docker-layers` on every
   node (operator-set-up; the node-agent doesn't need to know how it's
   provisioned).
2. Configure the node's Docker daemon to use that path as a
   `--data-root` for a *secondary* image store, or use a layer-pull
   proxy that lands content in the shared cache (`registry`-as-a-cache
   pattern; see spec 09).
3. The node agent's image cache manager (spec 15) treats the shared
   cache as a higher-tier read-through layer: a cache miss on the
   local SSD becomes a cache hit on shared storage instead of a fresh
   registry pull.

This is documented as a recommended pattern, not a feature — it
depends on the user's infra. If shared storage isn't available, spec
15's per-node smart-eviction is the fallback.

## Importing existing benchmark task suites

Real-world benchmarks ship task definitions in their own formats — not
ours — and we should not force users to hand-port every task.
Two upstream patterns dominate; spec 06 supports both.

### Pattern A: per-task pre-built Docker image (terminal-bench-2 style)

[terminal-bench-2](https://github.com/harbor-framework/terminal-bench-2)
ships one task per directory:

```
build-cython-ext/
├── task.toml             # metadata + [environment] block (docker_image, cpus, mem, ...)
├── environment/Dockerfile  # source for the image, for rebuild
├── instruction.md
├── solution/
└── tests/
```

`task.toml`'s `[environment]` declares a pre-built image:
```toml
[environment]
docker_image = "alexgshaw/build-cython-ext:20251031"
cpus = 1
memory = "2G"
storage = "10G"
build_timeout_sec = 600.0
```

XRLEnv handles this with an **instance resolver** declared on the
template manifest. One template (`terminal-bench-2`) covers the whole
suite; each rollout's `init_params` selects which task, and the
resolver maps the task to its image and resources:

```yaml
# xrlenv_plugins/benchmarks/<your_plugin>/manifest.yaml
name: your-bench
required_backends: [docker]

# Per-instance config comes from a resolver, not from this manifest.
instances:
  module: xrlenv_plugins.benchmarks.<your_plugin>.adapter
  resolver: YourInstanceResolver
  index_path: ./tasks/                    # vendored upstream task dirs
  # The resolver reads each task.toml and yields:
  #   { task_id, image_ref, cpus, memory_bytes, disk_bytes,
  #     mounts: [...], init_params: {...} }
  # Per-instance fields override the defaults below.

# Defaults (override per-instance from the resolver).
image:
  pre_pull: false                          # too many to pre-pull all by default
resources:
  cpu_request: 1.0
  mem_request: 2GB
  disk_request: 10GB

env_adapter:
  module: xrlenv_plugins.benchmarks.<your_plugin>.adapter
  class: YourEnvAdapter
  init_params: {}                          # filled per-instance from resolver

reward:
  mode: in_sandbox_final
  cmd: ["/opt/xrlenv/run-tests.sh"]

deadline:
  default_hard_s: 900
  init_timeout_s: 600
  image_pull_timeout_s: 600
```

Flow per rollout:

1. Trainer calls `client.rollout(template="your-bench",
   init={"task_id": "<some-task>"}, ...)`.
2. Coordinator asks the resolver to expand the request:
   `resolver.resolve("<some-task>")` returns
   `{image_ref: "registry.example/<task>:<rev>", cpus: 1,
   mem: 2GB, mounts: [{host: ".../tasks/<task>/tests",
   sandbox: "/opt/tests", readonly: true}, ...], ...}`.
3. The Image Cache Manager (spec 15) ensures the image is on the
   target node — pull on demand, or pre-fetched via
   `client.warmup(template="your-bench", instance_ids=[...])`.
4. Backend creates the sandbox with the resolved image and resource
   spec.
5. EnvAdapter loads inside the sandbox; `setup({task_id: ...})`
   bind-mounts include `tests/`, `solution/`, `instruction.md` so
   the in-sandbox grader can run.

The resolver lives in plug-in code
(`xrlenv_plugins.benchmarks.<your_plugin>.adapter`), not platform
core. Importing an upstream task suite = pip-install the upstream
package + clone/symlink the task tree under `./tasks/`. **No
XRLEnv core change** to add a new task — adding a new directory
under `./tasks/` is enough; the resolver picks it up.

> **Slim-pivot scope note (P1.7).** Pattern A is the **case-1**
> mechanism (RL training, step-driven `act → obs` loops). Case-2/3
> evaluation harnesses do per-task resolution inside the harness
> itself (SWE-bench's `swebench.harness.run_evaluation`,
> harbor's task layout) — see spec 14's scope note + the
> Integration docs picker.

### Build-from-source variant (bring-your-own-Dockerfile)

When the upstream's pre-built image is unavailable (private fork, air-
gapped cluster, custom modifications), or a user wants to iterate on
their own environment without waiting on the operator to pre-build it,
the manifest or the resolver's per-instance return can declare a
**build** instead of a pull:

```yaml
# On the manifest, or inside the resolver's per-instance return:
image_build:
  context: "./tasks/build-cython-ext/environment"   # dir with a Dockerfile + build context
  dockerfile: Dockerfile                            # optional; defaults to "Dockerfile"
  build_args: { FOO: bar }                          # optional; folded into the content-addressed tag
  durable_to: "reg.mycorp.internal:5000/team/env"   # optional; omit → scratch-only + GC warning
  # git: { repo: ..., ref: ..., subdir: ..., dockerfile: ... }  # alternative to context:
  # tag: ...                                        # optional; default is content-addressed
```

`context:` is the bring-your-own-Dockerfile path (a local dir; no git
required); `git:` is the alternative for a context that already lives
in a repo. The Image Cache Manager treats `image_build` like any other
image ref: it builds **once for the fleet, on demand**, distributes by
registry pull, tracks it as a normal cached image, and evicts via the
same LRU. The built image lands in a **scratch registry** that is
quota-bounded and GC'd (so build-on-demand never grows the private
registry unboundedly); `durable_to` copies it, digest-preserved, into a
user-owned registry that survives GC. The build is content-addressed
over `(base@digest, Dockerfile, context, build_args)`, so it is built
exactly once and rebuilds only when an input changes.

> **Full design:** `notes/scratch-registry-build-on-demand.md` — the
> scratch registry (`:5012`), content-addressed build-once, the durable
> bring-your-own-registry tier, GC + quota, and the `image_pin_mode`
> interaction. This block is the user-facing surface; the note is the
> mechanism.

### Pattern B: single image + large external artifact (OSWorld style)

[OSWorld's docker provider](https://github.com/xlang-ai/OSWorld/blob/main/desktop_env/providers/docker/manager.py)
runs *one* Docker image (`xlangai/...-host`), but every sandbox needs
a multi-GB qcow2 VM disk image downloaded from Hugging Face:

```python
UBUNTU_X86_URL = "https://huggingface.co/datasets/xlangai/ubuntu_osworld/.../Ubuntu.qcow2.zip"
```

The qcow2 is shared across sandboxes (read-only base) but each
sandbox needs a *writable* per-sandbox layer for VM state. Hard-coding
this download into the OSWorld manager works for one machine; at
cluster scale we want first-class platform support.

XRLEnv handles this with an `assets:` block on the template manifest:

```yaml
# xrlenv/templates/osworld/template.yaml
name: osworld
required_backends: [docker]

image:
  ref: "ghcr.io/xrlenv/osworld-host:0.1"   # the OSWorld controller image
  pre_pull: true

assets:
  - id: osworld-ubuntu-qcow2
    source: "https://huggingface.co/datasets/xlangai/ubuntu_osworld/resolve/main/Ubuntu.qcow2.zip"
    extract: zip                            # zip | tar | tar.gz | none
    extract_to: /var/cache/xrlenv/assets/osworld/
    sha256: "<hash>"                        # integrity check
    size_bytes: 16_000_000_000              # for cache budgeting
    mode: shared-readonly                   # see "asset modes" below

resources:
  cpu_request: 4.0
  mem_request: 8GB
  disk_request: 25GB                        # writable COW layer per sandbox
  mounts:
    # Read-only host-cached qcow2; backed by the asset above.
    - host_path: /var/cache/xrlenv/assets/osworld/Ubuntu.qcow2
      sandbox_path: /opt/osworld/base.qcow2
      readonly: true
    # Per-sandbox writable scratch dir — backend creates a fresh one
    # at create time, gives the sandbox an empty subdir of the node's
    # scratch root.
    - host_path: "{sandbox_scratch}/state/"
      sandbox_path: /opt/osworld/state/
      readonly: false

env_adapter:
  module: xrlenv.envs.osworld
  class:  DesktopEnvAdapter
  # Adapter init creates a per-sandbox COW overlay over the read-only
  # base.qcow2 using `qemu-img create -f qcow2 -b base.qcow2 state/disk.qcow2`,
  # then boots the VM from the overlay. Read traffic hits the shared
  # host page cache; writes go to the per-sandbox overlay.
  init_params:
    base_disk: /opt/osworld/base.qcow2
    overlay_dir: /opt/osworld/state/
```

#### Asset modes

| Mode | Semantics | Use case |
|---|---|---|
| `shared-readonly` | Downloaded once per node; bind-mounted read-only into every sandbox using it. | Backing qcow2, model checkpoints, large dataset shards. |
| `per-sandbox` | Downloaded once per node (cached), then *copied* fresh into each sandbox at create. | Templates that mutate the artifact and don't have a COW story. |
| `bake-into-image` | Build-time `ADD` / `COPY` into the image itself; no separate asset tracking. | Small artifacts (<100 MB) that change rarely. |

#### Asset cache (spec 15 extension)

The Image Cache Manager (spec 15) is extended in phase 0 to track
**assets alongside images**, with the same priority tiers (in-use,
pinned, soon-needed, recently-used, cold), the same eviction logic,
and the same `client.warmup(...)` path. From the cluster's
perspective, an asset and an image are both "blobs to keep ready
on the right nodes."

What changes for assets:
- The fetcher: HTTP/S3/GCS download with resume + sha256 check
  instead of `docker pull`.
- The eviction unit: the file (or extracted directory) at
  `extract_to`, not a Docker layer set.
- The mount-time hook: the backend turns asset-id references into
  concrete `host_path` values when assembling `MountSpec`s for
  `create`.

Operator surface mirrors images:
```
$ xrlenv assets
NODE     ASSETS  USED      FREE     PINNED  RECENT  COLD
gcp-1    3       58 GB     142 GB   1       1       1

$ xrlenv warmup --templates osworld
# pre-fetches both the host image and the qcow2 asset, in parallel,
# IO-budgeted per node.
```

Spec 15's data structures extend symmetrically; no separate cache
manager is introduced.

#### Per-sandbox writable scratch

For Pattern B (and any template that needs ephemeral writable space
the size of `disk_request`), the node agent provisions a per-sandbox
scratch directory at create time under
`/var/lib/xrlenv/scratch/<sandbox_id>/`. The `{sandbox_scratch}`
placeholder in `MountSpec.host_path` resolves to it. The backend
mounts the scratch as the sandbox's writable workspace; on
`destroy`, the scratch directory is removed (one of the GC layers
in spec 09).

### Choosing between Patterns A and B

| Upstream looks like | Pick | Why |
|---|---|---|
| One image per task, parameterized by which test/grader to run | **A** with `instances:` resolver | Per-task images cleanly separate; image cache manager handles 100s of refs. |
| One image, but a fat artifact has to live next to it | **B** with `assets:` block | Image stays small; artifact deduped at the node level. |
| One image + a small per-task asset (<100 MB) | A or B; if you can bake into per-task images, A is simpler | Below the asset-cache threshold, the cost difference doesn't matter. |
| Both per-task images *and* a shared fat asset | A + B together | Manifests support both blocks. Composition is mostly orthogonal: A's instance resolver chooses image; B's `assets:` block stages the shared blob. |

### Vendoring the upstream task tree

For both patterns, the upstream task definitions live under
`./tasks/<benchmark>/` in the user's repo (or a separate Python
package the user pip-installs). The resolver consumes that tree.
We don't try to wrap the upstream's task format with our own —
`task.toml`, OSWorld's task config jsons, SWE-bench's `dataset.json`
all stay in their native shape, and the adapter knows how to read
each one. **The adapter is the seam between upstream task format and
XRLEnv runtime.**

### Cross-task image consolidation (see spec 16)

Default behavior for every benchmark in phase 0 is "use upstream
per-task images as-is" — faithful, easy, requires zero analysis.

When scale matters — disk pressure on training nodes, or
training-vs-eval sandbox parity is a sanity-check requirement — an
**analysis tool** (`xrlenv analyze`, spec 16) examines the
benchmark's task suite and produces a `plan.yaml` recommending which
tasks can share a consolidated runtime, which need to stay on their
upstream per-task image, and what should be mounted vs installed.

Two things to note up front:

- **There is no single runtime image per benchmark.** Real
  benchmarks like terminal-bench-2 span domains too varied for one
  runtime; the analysis empirically clusters tasks (often into
  several runtimes plus a "unique" fallback that keeps upstream
  per-task images). One-runtime-per-benchmark would either bloat or
  break.
- **Different benchmarks define tasks in different formats.** The
  analysis tool is benchmark-agnostic *after* ingestion; the per-
  benchmark step is one method on the EnvAdapter
  (`enumerate_tasks`) that yields a normalized `TaskDescriptor`. See
  spec 14 for the adapter contract and spec 16 for the full
  workflow.

The `instances:` resolver described above is the single integration
point: when a `plan.yaml` is in effect, the resolver dispatches each
`task_id` according to the plan's grouping; otherwise it falls back
to the per-task image the upstream declared. Same template manifest,
same SDK call, smarter dispatch when consolidation has been done.

This is **phase 1**. Phase 0 ships only the resolver + per-task-
upstream-image dispatch, which is sufficient for smoke-test scale.

## Pattern: shared agent binary across many sandboxes

For installed-agent workloads (Claude Code, Aider, OpenHands, etc.)
where every sandbox needs the same multi-MB CLI, prefer a host
bind-mount (`resources.mounts`) over re-uploading the binary per
sandbox. The pattern:

1. Pre-stage the agent on each node at a known path (operator step
   during node bootstrap, or at template registration).
2. Declare it read-only-mounted in the template:
   ```yaml
   resources:
     mounts:
       - host_path: /opt/xrlenv-shared/agents/claude-code
         sandbox_path: /opt/agent
         readonly: true
   ```
3. The init script or `services:` block references `/opt/agent/...`
   inside the sandbox. Zero per-sandbox copy cost; one copy on the
   node serves all concurrent sandboxes.

Compare: the terminal-bench claude-code-agent installs a small
*setup script* per sandbox (uses `write_file` + `exec`, fine), then
the script `npm install`s the actual CLI from the internet — that's
two round-trips and a network dependency every rollout. The mount
approach replaces both with a constant-time setup.

## Tool services pattern

Long-lived processes inside a sandbox (browser, Python kernel,
language server) are first-class: declared under `services:`, started
by the stub during init, addressed by name at step time, and *not*
torn down between steps. They are torn down at sandbox destroy.

### A note on ports

Port numbers in `services[N].port` are **sandbox-internal**. Each
sandbox has its own network namespace (spec 01), so all copies of
this template can declare the same internal port numbers without
host-side collision. Services inside the sandbox reach each other
by name via the `XRLENV_SERVICE_PORTS_JSON` env var the stub
injects; no hard-coded port assumptions are needed in the
template's commands.

If a service should additionally be reachable from outside the
sandbox (debug VNC, HTTP dashboard for an operator), the adapter
or operator calls `session.port_forward(internal_port)` at runtime;
the host-side port is allocated dynamically from the node's
ephemeral range. Multiple sandboxes forwarding the same internal
port get distinct host ports.

Service entries in `template.yaml` are `ServiceSpec` shapes (spec 01):

```yaml
services:
  - name: browser
    cmd: ["chromium", "--headless=new", "--remote-debugging-port=9222"]
    port: 9222
    depends_on: [x_server]
    health_check: ["curl", "-fsS", "http://localhost:9222/json/version"]
    startup_timeout_s: 15
    restart: on_failure
  - name: pyrepl
    cmd: ["python", "/opt/xrlenv/pyrepl_server.py"]
    port: 9223
    health_check: ["curl", "-fsS", "http://localhost:9223/healthz"]
    restart: on_failure
```

The in-sandbox stub starts these in topological order, gates each on
its `health_check`, and injects `XRLENV_SERVICE_PORTS_JSON` into every
spawned process so co-located services discover one another's ports
without hard-coding.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: terminal-base, swebench-base, osworld-base.
- **Phase 1**: web-search-base, allowlist plumbing, warm pools, browser
  service.
- **Phase 2**: deep-research-base, persistent disks, snapshot/branch.
