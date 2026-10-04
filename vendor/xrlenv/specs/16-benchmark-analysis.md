# 16 — Benchmark Analysis & Image Consolidation

## Purpose

A discovery tool — `xrlenv analyze` — that examines a benchmark's
task suite (or a user-defined task set) and produces an empirical
**consolidation plan**: which tasks can share a runtime image, which
need to stay on their upstream per-task image, what should be
mounted vs installed, and how much disk we save by acting on it.

This is a **discovery tool, not a mandate.** The default for a
freshly onboarded benchmark remains "use upstream images as-is"
(faithful, easy). The analysis runs when you care about scale —
training across hundreds of tasks where total image bytes per node
matter, or eval-vs-training sandbox parity is a sanity-check
requirement.

## Why this is needed

A benchmark like terminal-bench-2 spans wildly different domains
(Cython debugging, kernel tasks, web tasks, Linux configuration).
There is **no single runtime image** that fits all of them
efficiently — pretending otherwise either bloats the runtime to
absurd size or breaks a third of the tasks. Empirical grouping is
the only honest answer:

- Some clusters of tasks really do share 90%+ of their image content
  → consolidate them onto one runtime + small per-task delta.
- Some tasks are genuinely unique → leave them on their upstream
  per-task image.
- The split is per-benchmark-and-per-cluster; you can't decide it
  without looking.

## Different benchmarks, different protocols

Each benchmark defines its tasks in its own format:

| Benchmark | Source of truth | Where the image is named |
|---|---|---|
| terminal-bench-2 | `task.toml` per task dir | `[environment].docker_image` |
| OSWorld | per-task JSON configs + a global VM image | one host image; per-task config in JSON |
| SWE-bench | `dataset.json` rows | per-instance image refs in a registry |
| user-defined | whatever the user picks | wherever the user puts it |

The analysis tool **does not** try to unify these formats. The
benchmark-specific work is isolated to one method on the EnvAdapter
— `enumerate_tasks()` — that yields a normalized `TaskDescriptor`.
Everything downstream of that (image introspection, clustering,
consolidation, equivalence checks) operates on the normalized
intermediate representation and is benchmark-agnostic.

## Adapter contract for analysis ingestion

```python
@dataclass
class TaskAsset:
    """A per-task data directory or file the analysis can mount or bake."""
    role: str                    # "tests" | "solution" | "instruction" | ...
    host_path: Path              # absolute path on the analyzing machine
    size_bytes: int

@dataclass
class TaskDescriptor:
    task_id: str
    declared_image: ImageRef | None         # upstream's image, if any
    declared_image_build: BuildSpec | None  # upstream's Dockerfile, if any
    declared_resources: ResourceSpec
    task_assets: list[TaskAsset]
    upstream_format: dict                   # adapter's native fields, opaque to core

class BenchmarkAdapter(Protocol):
    """The EnvAdapter (spec 14) implements this when it integrates a
    benchmark whose task suite the operator may want to analyze."""

    def enumerate_tasks(self) -> Iterator[TaskDescriptor]:
        """Yield every task in the benchmark, in its native ordering."""

    def resolve_instance(self,
                         task_id: str,
                         plan: Plan | None = None) -> ResolvedInstance:
        """Resolve a single task to a concrete sandbox spec.

        - plan is None  -> faithful upstream behavior (per-task image as declared).
        - plan provided -> dispatch by the plan's grouping (consolidated runtime
          + mounts + init hooks for tasks in a group; upstream image for tasks
          in the 'unique' fallback group).
        """
```

`enumerate_tasks` is the only benchmark-specific step in the
pipeline. Adding a new benchmark = implementing this method on its
EnvAdapter. The rest of the analysis is shared across benchmarks.

## The four analysis passes

### Pass 1 — image introspection

For every distinct image referenced across all `TaskDescriptor`s:

- Pull the manifest (cheap; layer digests + sizes; no full image
  pull).
- Optionally run a one-shot **introspection container** that emits:
  - `FROM` chain
  - `apt list --installed`, `pip freeze`, `npm ls -g --depth=0`
  - File-tree summary at `/usr`, `/opt`, `/home`, `/root`
  - Total image size, per-layer sizes, layer digests

Output: an `ImageFingerprint` per distinct image. Cached on disk so
re-running the analysis on the same set is fast.

### Pass 2 — overlap / clustering

Compute pairwise similarity over fingerprints — Jaccard over
installed-package sets, identical-layer counts, common-base
detection — and cluster.

The clustering algorithm itself is agglomerative with a similarity
threshold (default 0.85; tunable per benchmark). Output: a candidate
grouping where every group's members share enough that consolidating
them is plausible.

For terminal-bench-2 a realistic clustering might land at:

```
group: python-data-science     (~70 tasks; FROM python:3.13-slim + numpy/pandas/scipy)
group: nodejs                   (~15 tasks; FROM node:20-slim)
group: systems                  (~10 tasks; FROM ubuntu:22.04 + build-essential)
group: unique                   (~5 tasks; no good cluster — keep upstream image)
```

### Pass 3 — consolidation candidate

For each group with high overlap, the tool emits:

- A candidate consolidated runtime **Dockerfile** (the union of
  common layers, with version-divergent deps moved to a shared
  pip / npm cache or a per-task install hook).
- An **`init_install` hook** template — the per-sandbox commands the
  resolver injects when launching a task in this group.
- An estimated **disk-savings number** (current N images × avg
  size → K runtimes + per-task delta).
- An **init-time penalty** estimate per task (e.g. "+1.2 s for
  `pip install` from shared cache").
- **Risks**: tasks whose declared environment hints at incompatibility
  with the consolidated runtime (e.g. divergent `LD_LIBRARY_PATH`,
  conflicting system packages). Flagged for manual review.

### Pass 4 — equivalence smoke

For each task in a candidate consolidated group, run a fast
"agent-observable equivalence" check:

1. Spin one sandbox from the **consolidated runtime + mounts +
   init_install**.
2. Spin one sandbox from the **upstream per-task image**.
3. Run the task's reference grader (or a vendored test harness) on
   the same reference solution in both.
4. Compare grader outcomes + a file-tree diff at conventional paths
   (`/opt/task`, `/workspace`, `/root`).

Tasks that **fail equivalence** are dropped automatically from the
consolidated group and demoted to the `unique` fallback (they keep
their upstream per-task image). The analysis report lists each
demotion with the reason.

## The output: `plan.yaml`

The artifact the resolver consumes:

```yaml
benchmark: terminal-bench-2
generated_by: "xrlenv analyze"
generated_at: "2026-04-25T14:00:00Z"

groups:
  - id: python-data-science
    runtime:
      image_build:
        context: "groups/python-data-science/"
        tag: "xrlenv-tb2-py-ds:0.1"
    init_install:
      - "pip install --cache-dir /var/cache/xrlenv/pip -r {task_dir}/reqs.txt"
    task_mounts:
      - role: tests
        sandbox_path: /opt/task/tests
        readonly: true
      - role: solution
        sandbox_path: /opt/task/solution
        readonly: true
      - role: instruction
        sandbox_path: /opt/task/instruction.md
        readonly: true
    member_tasks:
      - build-cython-ext
      - ... (69 more)

  - id: nodejs
    runtime: { image_build: { context: "groups/nodejs/", tag: "xrlenv-tb2-node:0.1" } }
    init_install:
      - "cd {task_dir} && npm ci --cache /var/cache/xrlenv/npm"
    task_mounts: [...]
    member_tasks: [...]

  - id: unique
    fallback: per-task-upstream-image       # use TaskDescriptor.declared_image
    member_tasks: [task-x, task-y, task-z, task-w, task-v]

summary:
  estimated_disk_savings_per_node: "118 GB -> 26 GB"
  equivalence_passed: 89
  equivalence_failed: 1                    # auto-demoted to 'unique'
  equivalence_skipped: 5                   # tasks in 'unique', no consolidation attempted
```

The resolver (`adapter.resolve_instance(task_id, plan=plan)`) reads
the plan and dispatches:

- Task in a consolidated group → return `ResolvedInstance` with the
  group's runtime image, the group's `init_install` script, and
  task-specific mounts derived from `TaskDescriptor.task_assets`
  according to `task_mounts`.
- Task in `unique` → return upstream per-task image (the original
  Pattern A behavior, faithful to upstream).

Same SDK call, same template manifest, just smarter dispatch.

## Operator override

`plan.yaml` is **a recommendation, not a contract**. It is a plain
YAML file the operator hand-edits to:

- Promote a task between groups (`unique` → `python-data-science`,
  or vice versa).
- Force a task to stay on upstream per-task image even if the
  analysis recommended consolidation.
- Add a manual group with a custom Dockerfile.
- Adjust `init_install` commands or task-mount roles.

After editing, `xrlenv analyze --check ./plan.yaml` re-runs only the
equivalence pass against the operator's choices and flags any
demotions implied by the edits.

## CLI

```
xrlenv analyze tasks/<your-bench>/                                \
    --adapter xrlenv_plugins.benchmarks.<your_plugin>.adapter     \
    --output plan.yaml                                            \
    --equivalence-budget 30m                                      \
    --threshold 0.85

xrlenv analyze --report plan.yaml              # render plan.yaml -> human report
xrlenv analyze --check  plan.yaml              # re-run equivalence on edited plan
```

The analyzing machine needs Docker (to run introspection containers
and equivalence smokes). It does **not** need to be a control-plane
or node-agent host — running on the operator's laptop is fine, as
long as it can pull the benchmark's images.

### Analysis-mode isolation profile

`xrlenv analyze` runs **arbitrary benchmark images** (the whole
point is to introspect them). Some of those images come from
public registries with unknown supply chains. The analyzer must
not give them the operator's host. The CLI enforces a fixed
isolation profile for every container it spawns:

| Setting | Value | Why |
|---|---|---|
| Network | `--network none` for introspection; per-host bridge with metadata block for equivalence smokes | introspection is filesystem-only; equivalence may need network for some benchmarks but never the cloud metadata IP |
| Mounts | only the benchmark's task asset dirs, read-only | no host mounts; no `/var/run/docker.sock`; no `~/.docker`, `~/.aws`, `~/.config/gcloud`, `~/.ssh` |
| User | rootless (`--userns-remap` or `--user $(id -u):$(id -g)`) when supported | analyzed image cannot escalate to host root |
| Capabilities | `--cap-drop=ALL` | no host system access |
| Seccomp | spec 19's `xrlenv-default.json` profile | block dangerous syscalls |
| Privileged | never | also blocks `--device`, `--security-opt apparmor=unconfined`, etc. |
| Resource caps | `--cpus 2 --memory 4G --pids-limit 256` | a runaway introspection cannot fork-bomb the host |
| Time budget | `--introspection-timeout 60s` per container; `--equivalence-budget 30m` total per task | hard kill; emits `analysis.timeout` event |
| Registry creds | a separate ephemeral Docker config dir scoped to read-only registry pulls; no push creds visible | a malicious image cannot use the operator's push token to publish |

The analyzer surfaces these in `--dry-run` output so the operator
can audit before running. Running outside this profile (e.g.
`xrlenv analyze --unsafe-host-mounts`) requires the
`--i-know-this-is-dangerous` flag and writes a prominent
`audit:analysis.unsafe_run` event.

For very-untrusted images (analyzing a third-party fork, an
unsigned community contribution), operators can point the
analyzer at a **disposable analysis VM**:

```
xrlenv analyze tasks/foo/ --remote analyzer-vm.example.com
```

The remote node runs the same isolation profile but on hardware
the operator considers expendable. Phase 1.

### Why this matters

Without the isolation profile, the operator's laptop or builder
VM is the weakest link in the supply chain — analyzing a
compromised benchmark image once would expose registry
credentials, SSH keys, and any host file the user could read.
Spec 19's threat model #2 (compromised template image) extends
to analysis-time, not just runtime.

## How runtimes from the plan participate in the rest of the system

- **Image cache manager (spec 15)**: runtimes named in `plan.yaml`
  are added to the operator pin list automatically — they're the
  hottest images on the cluster once a consolidated training run is
  underway, and we don't want them evicted.
- **Centralized build (spec 09)**: runtimes declared with
  `image_build:` in the plan are built by `xrlenv build-images` on
  the designated builder VM and pushed to the cluster registry
  mirror. Nodes pull, never build.
- **Image-set lockfile (spec 09 phase 1)**: `xrlenv freeze` resolves
  every runtime in `plan.yaml` to its content digest before a
  training run, so teammates rebuilding mid-run don't perturb the
  in-flight cluster.
- **Admin panel (spec 13)**: a `/plans` view shows currently active
  plans per benchmark, group membership, and equivalence-check
  status.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: not implemented. Phase 0 ships the
  `enumerate_tasks` / `resolve_instance` adapter contract and the
  `instances:` resolver in spec 06; consolidation is unavailable.
  Default for every benchmark in phase 0 is "upstream per-task image
  as-is" — perfectly fine for the smoke-test scale of a phase-0
  cluster.
- **Phase 1**: this spec lands. `xrlenv analyze` ships, equivalence
  smoke runs, `plan.yaml` flows into resolvers and the image cache
  manager. The first benchmarks consolidated end-to-end are
  SWE-bench and terminal-bench-2 (the largest hot-set offenders).
- **Phase 2**: re-analysis on a schedule (re-cluster as the suite
  evolves), automated cross-benchmark consolidation (when two
  benchmarks share a runtime, share the runtime), historical
  equivalence dashboards (long-running drift detection between a
  consolidated runtime and its upstream baseline).

## Non-goals

- Not a generic Docker layer-dedup tool. Docker's automatic layer
  sharing already handles the easy case (multiple tags from the same
  Dockerfile chain). This tool finds the *cross-image* opportunities
  Docker's per-image FROM-chain logic can't see.
- Not a benchmark format unifier. Each benchmark's native format
  (`task.toml`, OSWorld JSONs, SWE-bench `dataset.json`, custom)
  stays in its native shape; the adapter's `enumerate_tasks` is the
  only seam.
- Not an equivalence prover. Pass 4's smoke check verifies
  agent-observable behavior on a reference solution; it does not
  certify that every possible agent trajectory will see identical
  state. Operators who need stronger guarantees stay on upstream
  per-task images.
