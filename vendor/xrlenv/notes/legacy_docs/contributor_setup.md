# Contributor setup

This page is for contributors working on XRLEnv itself. For runtime
installation and the first rollout, use {doc}`/installation` and
{doc}`/quickstart`.

## Development install

```bash
git clone https://github.com/<your-org>/XRLEnv.git
cd XRLEnv
uv sync --extra dev
```

Add the docs toolchain when editing the Sphinx site:

```bash
uv pip install -e '.[docs]'
```

## Test suite

Run the unit tests:

```bash
.venv/bin/python -m pytest -q
```

Run focused plug-in tests:

```bash
.venv/bin/python -m pytest xrlenv_plugins/harbor/ -q
.venv/bin/python -m pytest tests/unit/test_harbor_cluster.py -q
```

Static checks:

```bash
.venv/bin/python -m ruff check xrlenv/ xrlenv_plugins/ tests/
.venv/bin/python -m mypy xrlenv/ xrlenv_plugins/
```

## Build docs locally

```bash
.venv/bin/sphinx-build -W -b html docs docs/_build/html
```

Open `docs/_build/html/index.html` in a browser after a successful build.
Warnings are treated as errors in the strict build so broken cross-references
surface immediately.

## Documentation source layout

```text
docs/
├── index.rst              # Role-based landing page
├── getting_started/       # Install, first rollout, concepts
├── consumer/              # Client usage, run-config, timeouts
├── operations/            # Operator CLI and cluster-admin entry points
├── deployment/            # Local and multi-node rollout service deployment
├── observability/         # Admin panel, artifacts, metrics, logs
├── integration/           # Benchmark plug-in authoring
├── developer/             # Contributor setup and design rationale
└── api/                   # Autodoc reference
```
