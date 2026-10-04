# Docs full rewrite plan (post-P1.7) — locked IA

**Status**: LOCKED 2026-05-08. Executing in 5 sub-commits (D2A–D2E).

## Why rewrite

The pre-rewrite `docs/` had three structural problems:

1. **Mixes user manual with design rationale.** `specs/` is the
   canonical home for design (00–21); docs/ should be how-to /
   what-it-does, with a thin technical-details section for
   pictures/algorithms readers need on the operational path.
2. **Organized by role, not by task.** Roles overlap heavily in
   real life (the eval operator is also the consumer; the
   custom-workflow author runs their own laptop control plane).
   Task-oriented IA cuts cleaner.
3. **Over-indexed on case-1 RL training which isn't shipped.**
   EnvAdapter Protocol works (hello_shell + echo_bench), but
   trainer integration (Slime / verl) is P1.3 / P1.4, post-P1.7.
   Docs should be honest about the gap.

## Locked IA

```
docs/
├── index.rst                                   # landing: what XRLEnv is + 5 paths
│
├── getting_started/
│   ├── installation.md                         # uv pip install + .venv setup
│   ├── quickstart.md                           # 5-min walkthrough: laptop + 1 acquire
│   └── architecture.md                         # overview of the architecture
│                                                # (update outdated sections)
│
├── deploy/
│   ├── index.md                                # picker: single / multi
│   ├── single_node_deployment.md               # `xrlenv up` single host (all planes co-located)
│   └── multi_node_deployment/
│       ├── index.md                            # how to set up multi-node on cloud VMs
│       ├── inventory.md                        # nodes.yaml schema
│       ├── cloud_VM_providers/
│       │   ├── index.md                        # picker: GCP / AWS
│       │   ├── gcp.md
│       │   └── aws.md
│       └── runbook.md                          # six-step deployment (existing, scrub "Scenario 1 —")
│
├── supported_benchmarks_and_harnesses/
│   ├── index.md                                # mechanisms overview + how to write your own adapter
│   ├── swe_bench.md                            # docker-py drop-in
│   └── terminal_bench_2.md                     # harbor adapter
│
├── build_with_xrlenv/
│   ├── index.md                                # picker
│   ├── work_with_xrlenv_managed_containers/
│   │   ├── index.md                            # picker: drop-in / direct API
│   │   ├── docker_py_dropin.md                 # `xrlenv.from_env()` for existing docker-py code
│   │   └── direct_api.md                       # `Client.acquire_container` recipes
│   └── RL/
│       └── index.md                            # placeholder; trainer integration P1.3/P1.4
│
├── observability/
│   ├── index.md                                # what's traced + where to find it
│   ├── admin_panel.md                          # all /admin views
│   ├── metrics.md                              # Prometheus /metrics (REAL: counters/gauges/histograms exist)
│   ├── logs.md                                 # structured logs schema + reading them
│   └── capacity.md                             # `xrlenv images plan`, warm pools, eviction
│
├── technical_details/
│   ├── index.md                                # what this section is for
│   ├── images/
│   │   ├── index.md                            # overview
│   │   └── build_plan.md                       # CONSOLIDATES build_plans.md + images.md:
│   │                                            #   1. Known-in-advance: assign pull/build to nodes by constraints
│   │                                            #   2. On-demand pull/build (benchmark-driven; xrlenv assigns)
│   │                                            #   3. Constraints handled by scheduler
│   │                                            #   4. Idempotence layer
│   └── scheduling.md                           # scheduler: image routing + rollout routing
│                                                # (cluster workload, node capacity, image affinity)
│
├── developer_guide/
│   ├── index.md                                # what this section is for
│   ├── security.md                             # security model
│   ├── tokens.md                               # `xrlenv tokens issue consumer` + audit
│   ├── timeouts.md                             # consumer/timeouts.md + developer/timeouts_internals.md merged
│   ├── run_config.md                           # run-config schema (was consumer/run_config.md)
│   ├── cli_reference.md                        # full `xrlenv` CLI (was operations/cli.md)
│   └── api_reference.md                        # `xrlenv.Client` autodoc + selective hand-written
│
└── reference/
    ├── glossary.md
    └── cheatsheets/
        ├── index.md
        └── docker.md                            # docker commands referenced across the docs
```

8 top-level sections (+ landing). 25 unique pages. Down from
53 pages / 16 sub-dirs.

## Naming normalizations applied

Sphinx URLs hate spaces and dislike mixed case. Applied to the
proposed IA:

- `Technical Details/` → `technical_details/` (mandatory: space
  would break URLs).
- `Supported_benchmarks_and_harnesses/` → kept (already snake).
- `Build_with_xrlenv/` → `build_with_xrlenv/`.
- `Developer_Guide/` → `developer_guide/`.
- `Reference/` → `reference/`.
- File names: `Glossary.md` → `glossary.md`; `Cheatsheets/` →
  `cheatsheets/`.

## What gets deleted from the old tree

Hard-deletes (not represented in the new IA):

- `docs/architecture.md` (root-level) — content moves to
  `getting_started/architecture.md` after refresh.
- `docs/security.md` — moves to `developer_guide/security.md`.
- `docs/current_status.md` → `notes/` (internal, not public docs).
- `docs/roadmaps.md` → `notes/` (internal).
- `docs/developer/contributor_setup.md` → `CONTRIBUTING.md` at
  repo root (out of Sphinx).
- `docs/developer/design_principles.md` → already in
  `specs/00-overview.md` + `CLAUDE.md`; remove from Sphinx.
- `docs/integration/` (entire tree, 15 pages from previous IA) →
  replaced by `supported_benchmarks_and_harnesses/` (3) +
  `build_with_xrlenv/` (4) + folded reference content into
  `developer_guide/` and `build_with_xrlenv/RL/index.md`.
- `docs/api/*.rst` (autogenerated stubs) → folded into
  `developer_guide/api_reference.md`.
- `docs/getting_started/` (existing dir) — content rewritten.
- `docs/observability/per_run.md` → folded into
  `observability/admin_panel.md` (per-rollout admin views).
- `docs/cheatsheets/` → `reference/cheatsheets/`.
- `docs/glossary.md` → `reference/glossary.md`.
- `docs/consumer/index.md` → folded into
  `developer_guide/api_reference.md`.
- `docs/consumer/build_gym_env_with_xrlenv.md` → split into
  `build_with_xrlenv/work_with_xrlenv_managed_containers/{docker_py_dropin,direct_api}.md`.

## Sub-commits

Each ends with `sphinx-build -W -b html docs docs/_build/html`
clean and pytest green.

### D2A — scaffold + getting_started

- Create the new top-level dir tree (empty index.md placeholders).
- Write `docs/index.rst` (landing page: what XRLEnv is + 5 paths).
- Write `getting_started/installation.md` (rewrite for clarity;
  drop role talk; uv pip install + .venv setup).
- Write `getting_started/quickstart.md` (5-min walkthrough:
  `xrlenv up` on laptop + one direct-API acquire; replaces the
  pre-rewrite `single_rollout.py` literalinclude that's been
  stale since slim pivot).
- Write `getting_started/architecture.md` (move + update
  `docs/architecture.md`; refresh outdated parts: case-1/2/3
  picker framing, slim pivot, current admin panel routes,
  current Client surface).
- Update `docs/index.rst` toctree to include only the 8 new
  top-level sections; old sections still exist but are
  unreferenced (cleaned up in D2E).

### D2B — deploy/

- `deploy/index.md` (picker: single vs multi).
- `deploy/single_node_deployment.md` (rewrite from
  `docs/deployment/local.md`).
- `deploy/multi_node_deployment/index.md` (overview).
- `deploy/multi_node_deployment/inventory.md` (move
  `docs/deployment/inventory.md`).
- `deploy/multi_node_deployment/cloud_VM_providers/index.md`
  (picker).
- `deploy/multi_node_deployment/cloud_VM_providers/gcp.md`
  (move `docs/deployment/gcp.md`).
- `deploy/multi_node_deployment/cloud_VM_providers/aws.md`
  (move `docs/deployment/aws.md`).
- `deploy/multi_node_deployment/runbook.md` (move
  `docs/deployment/runbook.md`; scrub all "Scenario 1 —" /
  "Scenario-1" framing → unified "control plane on one host;
  data-plane on cloud VMs" narrative).

### D2C — supported_benchmarks_and_harnesses/ + build_with_xrlenv/

- `supported_benchmarks_and_harnesses/index.md` (mechanisms
  overview + how-to-write-your-own-adapter; expanded from the
  current `integration/evaluation/supported_benchmarks_and_harnesses/index.md`).
- `supported_benchmarks_and_harnesses/swe_bench.md` (move +
  rename from `swe-bench.md`).
- `supported_benchmarks_and_harnesses/terminal_bench_2.md`
  (move + rename from `harbor.md`).
- `build_with_xrlenv/index.md` (picker).
- `build_with_xrlenv/work_with_xrlenv_managed_containers/index.md`
  (picker: drop-in / direct API).
- `build_with_xrlenv/work_with_xrlenv_managed_containers/docker_py_dropin.md`
  (extract from `consumer/build_gym_env_with_xrlenv.md`).
- `build_with_xrlenv/work_with_xrlenv_managed_containers/direct_api.md`
  (extract from same).
- `build_with_xrlenv/RL/index.md` (placeholder; honest about
  trainer integration deferral; points at hello_shell +
  echo_bench worked examples for the EnvAdapter mechanism that
  IS shipped).

### D2D — observability/ + technical_details/

- `observability/index.md` (what's traced + where).
- `observability/admin_panel.md` (move + fold per_run.md).
- `observability/metrics.md` (real metrics doc — `MetricsRegistry`
  exposes shipped counters/gauges/histograms; not a placeholder).
- `observability/logs.md` (move).
- `observability/capacity.md` (extract from
  `deployment/build_plans.md`).
- `technical_details/index.md` (what this section is for —
  algorithmic / architectural details a reader needs operationally,
  not full design rationale which lives in specs/).
- `technical_details/images/index.md` (overview).
- `technical_details/images/build_plan.md` (CONSOLIDATES
  `deployment/build_plans.md` + `deployment/images.md` +
  `technical/images.md`; covers the four topics specified by user:
  known-in-advance assignment, on-demand pull/build, scheduler
  constraints, idempotence layer).
- `technical_details/scheduling.md` (update `technical/scheduling.md`
  for the post-slim-pivot scheduler shape: image routing +
  rollout routing under workload / node capacity / image
  affinity).

### D2E — developer_guide/ + reference/ + delete old tree + audit close

- `developer_guide/index.md` (catch-all kitchen sink; explain
  what's here).
- `developer_guide/security.md` (move `docs/security.md`).
- `developer_guide/tokens.md` (extract `xrlenv tokens issue` +
  audit log content from current operator runbook).
- `developer_guide/timeouts.md` (merge `consumer/timeouts.md` +
  `developer/timeouts_internals.md`).
- `developer_guide/run_config.md` (move
  `consumer/run_config.md`).
- `developer_guide/cli_reference.md` (move `operations/cli.md`).
- `developer_guide/api_reference.md` (autodoc-driven; merge
  `consumer/index.md` + `api/*.rst`).
- `reference/glossary.md` (move).
- `reference/cheatsheets/index.md` (move).
- `reference/cheatsheets/docker.md` (write — common docker
  commands referenced across the docs).
- **Delete the old tree** (`docs/{architecture.md, security.md,
  current_status.md, roadmaps.md, integration/, consumer/,
  observability/{per_run.md}, operations/, deployment/{old},
  technical/, developer/, getting_started/{old},
  cheatsheets/, glossary.md, api/}`).
- Repoint all cross-refs.
- Final `sphinx-build -W` clean.
- Suite green.

## What I won't do without explicit approval

- Touch `specs/` (already canonical, lots of careful history).
- Delete `notes/` content that isn't explicitly listed above.
- Change Sphinx theme / extensions / `conf.py` core config.
- Rename or delete `xrlenv_plugins/harbor/README.md` or any
  in-repo READMEs.
- Add new MyST / Sphinx extensions beyond what's already in
  `conf.py`.

## Validation per sub-commit

- `sphinx-build -W -b html docs docs/_build/html` — clean.
- `pytest -q --ignore=tests/smoke` — 1229 passing.
- `mypy` — clean.
- `ruff check` — only the 2 pre-existing errors in `docs/conf.py`
  + `tests/unit/test_raw_container.py` (predate this work).
- `grep -rn 'integration/overview\|integration/tutorials\|integration/benchmarks' docs/`
  — no matches at the end.
- Manual eyeball pass on `_build/html/index.html` sidebar to
  confirm rendered nav matches the locked IA (top-level: Getting
  started / Deploy / Supported benchmarks / Build with xrlenv /
  Observability / Technical details / Developer guide / Reference).
