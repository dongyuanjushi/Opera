# P1.7.D — In-tree plug-in deletion + audit

**Status**: PLANNED 2026-05-07. Awaiting green light to execute.

**Parent**: slice `P1.7.D` in `notes/phase-1-to-do.md` line 689–699
("delete `xrlenv_plugins/benchmarks/{tb2,swebench_verified}/`. Their
roles are subsumed by the drop-in approach.").

## Why now

P1.7.C.1 closed gate-green on the VM topology with 8 phase-0
terminal-bench-2 tasks via `XrlenvHarborEnvironmentCluster`. The
case-2 path (`xrlenv.from_env()` docker-py drop-in) closed earlier
under P1.7.B. Both case-2/3 onboarding paths are validated by the
new audience-facing examples under
`examples/benchmarks-onboarding/`. The in-tree EnvAdapter-shaped
plug-ins at `xrlenv_plugins/benchmarks/{terminal_bench_2,swebench_verified}/`
are now redundant — every consumer of the slim pivot reaches case-2/3
benchmarks through the harness's own extension mechanism, not
through xrlenv's `EnvAdapter` Protocol.

Keeping the dead code in-tree is a maintenance cost (drift between
upstream harness contracts and the in-tree wrapper) and a UX
hazard (operators reading the docs find two ways to onboard one
benchmark and have to figure out which is canonical).

## Decisions locked (2026-05-07 design conversation)

- **Fork 1 — `byo_dataset_harbor` example**: **delete it.** B11.6's
  external-pip-package mechanism stays validated by `echo_bench`
  alone (case-1 EnvAdapter). The "bring your own harbor dataset"
  UX is documented as prose in
  `docs/integration/tutorials/own_dataset.md` post-rewrite.
- **Fork 2 — `tb2/scripts/build-task-images.sh` fate**: **inline**
  into `examples/benchmarks-onboarding/terminal-bench-2/scripts/build-task-images.sh`.
  Today the onboarding wrapper delegates to the in-tree script;
  post-deletion the onboarding script becomes the canonical one.
- **Fork 3 — `swebench_verified/data/verified_instances.jsonl`
  (8MB)**: **delete with the plug-in.** New drop-in smoke uses
  HF cache via `_load_swebench_dataset_cached`; the vendored
  copy is moot.
- **Fork 4 — Docs rewrite shape**: **skeleton-rewrite
  `docs/integration/`** + surgical edits everywhere else.
  Integration is where the slim pivot inverts the narrative
  (case-2/3 plug in via drop-in / harbor adapter, not via
  EnvAdapter); Operations / Deployment / Observability mostly
  didn't change.
- **Fork 5 — Slice into sub-commits**: three sub-commits, each
  independently green and reviewable.

## Sub-slices

### P1.7.D.1 — Code + tests delete (~half day)

Goal: main green after this commit. No docs work; all docstring +
spec drift goes to the next sub-slices.

Hard deletes (verbatim, no rewrites):

- `xrlenv_plugins/benchmarks/terminal_bench_2/` — entire dir,
  except `scripts/build-task-images.sh` which gets inlined into
  `examples/benchmarks-onboarding/terminal-bench-2/scripts/build-task-images.sh`
  before the dir delete.
- `xrlenv_plugins/benchmarks/swebench_verified/` — entire dir
  (including the 8MB `data/verified_instances.jsonl`).
- `xrlenv_plugins/benchmarks/__init__.py` — empty namespace pkg
  shell after the children are gone. Delete.
- `examples/pip_new_datasets_or_benchmark/byo_dataset_harbor/` —
  per Fork 1, the example is moot post-pivot. Delete.
- `tests/unit/test_image_builder.py` — entire file. Tested the
  two deleted in-tree builders end-to-end; nothing salvageable.

Edits (mechanical, no real rewrites):

- `tests/unit/test_import_cycles.py` — swap the tb2 cycle probe
  to an `xrlenv_plugins.harbor` import (same property covered;
  imports a non-stale package).
- `tests/smoke/test_terminal_bench_2_drop_in.py` — scrub the
  three stale comments at lines ~71 / ~101–102 referencing
  `examples/tb2_acceptance_smoke.py`. The PHASE_0_TASKS list is
  duplicated locally; no code break.
- `pyproject.toml` line ~50 — drop the
  "mirrored in xrlenv_plugins/benchmarks/swebench_verified/pyproject.toml"
  comment.

Onboarding script promotion — REVISED 2026-05-07 after a
quick check of the 8 phase-0 tasks' `task.toml`s:

**All 8 phase-0 tasks have `docker_image = "alexgshaw/<task>:20251031"`
pointing at Docker Hub.** The cluster gate didn't actually use
`Tb2ImageBuilder`-produced local images; the cluster's
`acquire_container(ensure_image_present=True)` pulls from Docker
Hub on first acquire. So the build script (which forwards to
`xrlenv build apply --benchmark terminal-bench-2` →
`Tb2ImageBuilder`) isn't load-bearing for the smoke and would
silently break post-deletion anyway (its dispatcher target
disappears with the in-tree plug-in).

Final script handling:

- `xrlenv_plugins/benchmarks/terminal_bench_2/scripts/populate-harbor-cache.sh`
  → inline into
  `examples/benchmarks-onboarding/terminal-bench-2/scripts/populate-harbor-cache.sh`.
  Still needed: harbor reads each task's `task.toml` /
  `solution/` / `tests/` from the cache regardless of where the
  image comes from.
- `xrlenv_plugins/benchmarks/terminal_bench_2/scripts/build-task-images.sh`
  + the wrapper at `examples/benchmarks-onboarding/terminal-bench-2/scripts/build-task-images.sh`
  → **drop both.** The smoke pulls from Docker Hub. For
  hypothetical future tasks without a prebuilt `docker_image`
  field, document the build-on-acquire deferral (P1.7.C.2) or
  let the operator build + push manually.
- `xrlenv_plugins/benchmarks/terminal_bench_2/scripts/verify-setup.sh`
  — operator pre-flight; nice-to-have but not load-bearing.
  Drop with the dir.

README + smoke.py edits to reflect the new reality:

- `examples/benchmarks-onboarding/terminal-bench-2/smoke.py`
  pre-build reminder log line currently says "images aren't on
  a public registry". Wrong now — the 8 phase-0 tasks ARE on
  Docker Hub. Replace with: "cluster pulls task images from
  Docker Hub via `ensure_image_present` on first acquire;
  populate-harbor-cache.sh seeds the task metadata cache."
- `examples/benchmarks-onboarding/terminal-bench-2/README.md` —
  re-frame the "Pre-requisites" section: only step is
  `populate-harbor-cache.sh`; drop the build-task-images
  callout. Add a note: tasks without prebuilt registry images
  (rare in tb2 today) need either operator-built+pushed
  images or P1.7.C.2's build-on-acquire.

Cross-tree pointer cleanup (in D.1, alongside the move):

- `examples/benchmarks-onboarding/terminal-bench-2/smoke.py` line ~130
  — comment pointing at the old path; update to the new inline path.
- `examples/benchmarks-onboarding/terminal-bench-2/README.md` line ~205
  — same.
- `examples/benchmarks-onboarding/terminal-bench-2/scripts/build-task-images.sh`
  lines 6, 23 — self-reference to the old path; the inline rewrite
  drops these naturally.

Historical references in `notes/` (gitignored audit/rebuttal scratch
+ phase-0 acceptance record) — **leave as-is**. The phase-0
acceptance record is a historical document; it should reflect the
state at that time. `notes/image_caching.md` gets a one-line
update if its claim is now factually wrong post-deletion.

Validation:

- `.venv/bin/python -m pytest -q` — full suite stays green
  minus the 5 in-package tests + `test_image_builder.py` (~6
  files, expected count drop ~30 tests).
- `.venv/bin/python -m mypy` — clean.
- `.venv/bin/python -m ruff check` — clean.
- Smoke imports unchanged: drop-in smoke + harbor cluster smoke
  both work.

### P1.7.D.2 — Sphinx docs rewrite (~1 day)

Skeleton-rewrite of `docs/integration/`, surgical edits
elsewhere. Ends with `sphinx-build -W -b html docs docs/_build/html`
clean (zero warnings).

Hard-delete dirs (per-benchmark operator runbooks for the in-tree
path; the audience-facing operator UX now lives in the onboarding
example READMEs):

- `docs/integration/benchmarks/swebench_verified/` — entire dir.
- `docs/integration/benchmarks/terminal_bench_2/` — entire dir.

Skeleton rewrite (locked 2026-05-07):

```
docs/integration/
├── index.md                                  # picker: eval vs training
├── evaluation/                               # case 2/3
│   ├── index.md                              # what is eval; pointer to the two patterns
│   └── supported_benchmarks_and_harnesses/   # per-benchmark / per-framework adapter pages
│       ├── index.md                          # pattern + listing
│       ├── harbor.md                         # XrlenvHarborEnvironmentCluster path
│       │                                     # (terminal-bench-2, harbor-format datasets)
│       └── swe-bench.md                      # docker-py drop-in path
│                                             # (SWE-bench Verified, SWE-bench Lite)
├── training/                                 # case 1 (placeholder)
│   └── index.md
└── reference/                                # shared, re-scoped
    ├── envadapter.md                         # case-1
    ├── manifest.md                           # case-1
    ├── plugin_layout.md                      # case-1
    ├── distribution_paths.md                 # case-1 + brief case-2/3
    ├── pitfalls.md                           # case-1 + case-2/3
    └── failure_isolation.md                  # case-1

docs/consumer/
└── build_gym_env_with_xrlenv.md              # NEW: docker-like dev via xrlenv
```

Why "docker-like dev" lives under `consumer/` not under
`integration/evaluation/`: the audience is "someone using
xrlenv's primitives directly to build their own thing" (custom
harness, ad-hoc remote-docker workflow, gym-env wrapper for a
new benchmark) — which is the consumer surface, not benchmark
integration. Integration is reserved for "wire an existing
benchmark / harness framework into xrlenv".

- `integration/index.md` (top picker) — replaces the existing
  `overview.md`. Two-section page:
  - **Are you running an evaluation harness?** (case 2/3) →
    point at `evaluation/`.
  - **Are you training an RL agent?** (case 1, step-driven
    `act → obs` loops) → point at `training/`.
  Existing `overview.md` content gets folded in; the file
  itself is renamed.

- `integration/evaluation/index.md` — landing page for case 2/3:
  - "What evaluation looks like under xrlenv": you have an
    existing benchmark or harness framework (SWE-bench, harbor,
    eventually OSWorld); you want it running on a remote,
    xrlenv-scheduled cluster instead of one local docker daemon.
    xrlenv exposes a cluster-routed sandbox primitive that the
    benchmark / framework consumes via a per-benchmark or
    per-framework adapter — pre-wired adapters listed in
    `supported_benchmarks_and_harnesses/`.
  - Pointer at `supported_benchmarks_and_harnesses/index.md`
    for the listing.
  - Side pointer: "writing your own custom harness or just want
    a remote-docker workflow without a benchmark framework? →
    `consumer/build_gym_env_with_xrlenv.md`."

- `integration/evaluation/supported_benchmarks_and_harnesses/index.md`
  — pattern + listing:
  - Two adapter shapes the platform offers, by audience:
    - **Per-benchmark drop-in adapter** — for benchmarks whose
      upstream harness uses `docker.from_env()` directly (e.g.
      SWE-bench's `swebench.harness`). The adapter is a
      one-line swap on the consumer side
      (`docker.from_env()` → `xrlenv.from_env()`); the
      benchmark's grading + report shape land unchanged.
      Page: `swe-bench.md`.
    - **Per-framework adapter** — for frameworks with their
      own `BaseEnvironment` / `Provider` Protocol that
      consumers extend (e.g. harbor's `import_path`). The
      adapter subclasses the framework's Protocol and
      overrides container-touching seams to route through
      xrlenv. Page: `harbor.md`.
  - Listing table: framework / benchmark | adapter file |
    audience.
  - "How to write your own adapter" — link to
    `xrlenv_plugins/harbor/README.md` as the canonical
    walkthrough; same pattern applies to new frameworks.

- `integration/evaluation/supported_benchmarks_and_harnesses/harbor.md`
  — the harbor-shape adapter:
  - When this page is for you: you're running terminal-bench-2,
    a harbor-format dataset, or any benchmark whose tasks
    follow harbor's task layout (Dockerfile + `task.toml` +
    `solution/` + `tests/`).
  - Setup: `import_path:
    xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster` in
    `job.yaml`; env vars (`XRLENV_GRPC_HOST` / `_PORT` /
    `_CONSUMER_TOKEN` / `_GRPC_SECURE`); pre-build images on
    each node.
  - What changes for the user: nothing in their `job.yaml`
    beyond the `import_path` line; harbor's trial driver runs
    against the cluster identically to local docker.
  - Known limitations (single-service-only, no
    build-on-acquire yet — both P1.7.C.2).
  - Worked example pointer:
    `examples/benchmarks-onboarding/terminal-bench-2/`.

- `integration/evaluation/supported_benchmarks_and_harnesses/swe-bench.md`
  — the docker-py drop-in path for SWE-bench:
  - When this page is for you: you're running SWE-bench
    Verified, SWE-bench Lite, or any benchmark using
    `swebench.harness.run_evaluation` (or any harness whose
    sandbox driver is docker-py).
  - The drop-in promise: the harness's
    `client = docker.from_env()` becomes
    `client = xrlenv.from_env()`; nothing else in the harness
    changes.
  - Setup: env vars (`XRLENV_GRPC_HOST` / `_PORT` /
    `_CONSUMER_TOKEN` / `_GRPC_SECURE`); image distribution
    (cluster pulls from Docker Hub on first acquire via
    `ImageCacheManager.ensure_present`).
  - What's wired: containers.create / exec_run / put_archive /
    get_archive / streaming exec / images.* — full coverage
    of the surface SWE-bench uses.
  - Worked example pointer:
    `examples/benchmarks-onboarding/swebench-verified/`.
  - Side pointer: "want the same drop-in for your own
    docker-py harness? → `consumer/build_gym_env_with_xrlenv.md`."

- `consumer/build_gym_env_with_xrlenv.md` (NEW, **lives under
  `docs/consumer/`, not `docs/integration/`**) — the
  "docker-like dev experience" page. Audience: anyone who wants
  to write code that says "give me a remote container, run a
  command, copy bytes in/out, throw it away" without learning a
  new harness framework. Two sub-sections:
  1. **Drop-in for docker-py code.**
     - `docker.from_env()` → `xrlenv.from_env()` one-line
       swap.
     - Env-var protocol (`XRLENV_GRPC_HOST` etc).
     - What's wired (containers.create / exec_run /
       put_archive / get_archive / streaming exec / images.*).
     - What's not wired (raises `NotImplementedError` with a
       clear message).
     - Cross-link: same primitive that powers
       `integration/evaluation/supported_benchmarks_and_harnesses/swe-bench.md`.
  2. **Direct API for custom workflows.**
     - `Client.grpc(host=..., port=..., token=...)`.
     - `async with await client.acquire_container(image=...,
       command=[...], labels={...}) as session: ...`.
     - Recipes:
       - Run a command:
         `await session.exec(["ls", "/"], timeout_s=30)`
       - Stream a long-running command:
         `async for chunk in session.exec_stream([...], timeout_s=1800):`
       - Copy bytes in:
         `await session.put_archive(target_dir="/work", tarball=...)`
       - Copy bytes out:
         `tarball = await session.get_archive("/logs/output.txt")`
       - Destroy explicitly:
         `await session.destroy()` (or rely on `async with`).
     - `xrlenv.rollout_metadata(artifact_path=..., displayed_name=...)`
       for admin UX hooks.
     - Cross-link: same primitive that powers the harbor
       adapter under
       `integration/evaluation/supported_benchmarks_and_harnesses/harbor.md`
       (subclass-override pattern there; direct-call pattern
       here — both ride on the same `ClusterContainerSession`).

- `training/index.md` — placeholder for case 1:
  - One-paragraph framing: "Training-side onboarding is the
    EnvAdapter Protocol; the trainer drives an `act → obs`
    loop and xrlenv runs the sandbox under the hood."
  - Pointer to `xrlenv/templates/hello_shell/` (in-repo
    worked template) and
    `examples/pip_new_datasets_or_benchmark/echo_bench/` (pip
    package).
  - Pointer to `reference/envadapter.md`,
    `reference/manifest.md`, etc.
  - **"Coming soon" callout**: Slime / verl trainer integration
    rides on P1.3 / P1.4 (post-P1.7); when those land, this
    section grows tutorials + per-trainer pages.

- `reference/envadapter.md` — re-scope: "EnvAdapter is
  **case-1 only** under the slim pivot. Case-2/3 plug in via
  the drop-in / harbor adapter; see `../evaluation/` for the
  picker."
- `reference/manifest.md` — re-scope to case-1.
- `reference/plugin_layout.md` — case-1 layout.
- `reference/distribution_paths.md` — case-1 layout primary;
  brief case-2/3 section pointing at the harbor adapter +
  drop-in patterns.
- `reference/pitfalls.md` — case-1 pitfalls + case-2/3
  pitfalls (image-distribution staging, mkdir-before-put_archive,
  /logs/* dir creation, etc).
- `reference/failure_isolation.md` — re-scope to case-1.

Files getting deleted under `docs/integration/`:

- `overview.md` — content folded into the new top-level
  `integration/index.md`.
- `tutorials/` — entire dir. Case-2/3 tutorials get replaced by
  the per-benchmark pages under
  `evaluation/supported_benchmarks_and_harnesses/` plus the
  consumer-facing dev workflow page; case-1 tutorials live
  under `training/` or as worked examples.
- `benchmarks/` — entire dir (including the
  `swebench_verified/` and `terminal_bench_2/` operator-runbook
  subdirs already flagged for hard-delete in D.2's docs sweep).

Surgical edits — everywhere else (drop tb2/swebench example
references; replace with hello_shell / echo_bench / harbor where
relevant):

- `architecture.md` — light: replace tb2 example mentions
- `quickstart.md`, `installation.md`, `current_status.md`,
  `glossary.md`, `roadmaps.md`
- `api/envs.rst` — re-scope EnvAdapter Protocol docs to case-1
- `consumer/{run_config,index,timeouts}.md`
- `observability/{per_run,logs}.md`
- `operations/cli.md`
- `deployment/{images,build_plans,runbook}.md` — drop
  build-plans references that pointed at tb2/swebench
  build-plans dirs; case-2/3 builds via the harness's own flow
- `developer/{contributor_setup,timeouts_internals}.md`
- `technical/images.md`

Source-code docstring sweep (NOT user-facing, but the audit picks
these up):

- `xrlenv/backends/docker.py` — comment example
- `xrlenv/templates/__init__.py` — docstring example
- `xrlenv/envs/__init__.py` — docstring example
- `xrlenv/control/image_builder.py` — docstring + YAML example
- `xrlenv/control/instance_resolver.py` — docstring (multiple
  example references)
- `xrlenv/control/template_discovery.py` — docstring example

Every reference to `xrlenv_plugins.benchmarks.terminal_bench_2.*`
or `xrlenv_plugins.benchmarks.swebench_verified.*` in a docstring
gets swapped to either `xrlenv.templates.hello_shell` (in-repo
case-1 worked template) or `xrlenv_plugins.harbor` (case-3 plug-in
shape) depending on which is more illustrative for the docstring's
mechanism.

Validation:

- `sphinx-build -W -b html docs docs/_build/html` — zero
  warnings.
- `grep -rE 'terminal_bench_2|swebench_verified|byo_dataset_harbor|in-tree' docs xrlenv`
  — only intentional historical references remain (e.g.
  `current_status.md`'s past-tense entries).

### P1.7.D.3 — Specs (~half day)

- `specs/14-envadapter.md` — re-scope to case-1. Add an intro
  paragraph: "EnvAdapter is the case-1 mechanism (RL training,
  step-driven `act → obs` loops, e.g. hello_shell). Case-2/3
  benchmarks plug in via their harness's own extension mechanism
  (docker-py drop-in for SWE-bench-style harnesses, harbor's
  `import_path` for harbor-shape benchmarks); see specs 01 + the
  Integration docs."
- `specs/06-templates.md` — passing edit if it uses tb2/swebench
  as canonical examples; swap to hello_shell.
- `specs/16-benchmark-analysis.md` — passing edit; check if
  `xrlenv analyze` was tb2/swebench-specific; if so, re-scope or
  flag as deferred.
- Any other spec that mentions the deleted benchmark names —
  grep + edit. The Phase Matrix in `specs/00-overview.md` is
  already up to date per the slim-pivot work.

Validation:

- `grep -rE 'terminal_bench_2|swebench_verified' specs/` — only
  intentional historical mentions remain.

## Out of scope for P1.7.D (deferred)

- New case-2/3 worked examples beyond what `examples/benchmarks-onboarding/`
  already ships (tb2 + swebench).
- Trainer integration (Slime / verl) — P1.3 follows P1.7.
- P1.7.C.2 (multi-service compose + real build-on-acquire).
- Documentation for the harbor adapter's *non-Docker* future
  backends (E2B-style, Modal-style) — defer until they exist.

## Cross-tree pointer audit (resolved 2026-05-07)

Pre-D.1 grep for cross-tree links to the soon-deleted scripts:

```bash
grep -rE 'xrlenv_plugins/benchmarks/(terminal_bench_2|swebench_verified)/scripts' \
    --include='*.md' --include='*.rst' --include='*.sh' --include='*.py'
```

Inventory of hits:

- **D.1-fixable** (referenced in files that stay):
  - `examples/benchmarks-onboarding/terminal-bench-2/smoke.py` (~L130)
  - `examples/benchmarks-onboarding/terminal-bench-2/README.md` (~L205)
  - `examples/benchmarks-onboarding/terminal-bench-2/scripts/build-task-images.sh`
    (L6, L23 — self-reference; inline rewrite drops these)
- **D.2-fixable** (referenced in docs that get re-scoped):
  - `docs/integration/benchmarks/{swebench_verified,terminal_bench_2}/operator.md`
    — entire dirs hard-deleted per the docs section above
  - `docs/deployment/images.md` (L111)
  - `docs/deployment/runbook.md` (L409)
- **Leave alone** (historical / internal scratch):
  - `notes/phase-0-acceptance-results.md` — historical record
  - `notes/image_caching.md` — internal note; one-line edit if
    its claim is now factually wrong post-deletion
  - `xrlenv_plugins/benchmarks/terminal_bench_2/{examples,README.md}`
    — gets deleted with the dir

No surprises; D.1 + D.2 cover everything.
