# 09 — Deployment and Garbage Collection

## Purpose

How to actually run XRLEnv in each of its three deployment modes, and
how the system stays clean (no leaked sandboxes, no orphan files, no
runaway disk).

**No CI/CD or admin-required tooling in phase 0.** The user has only
VM-level access on their cloud accounts. We ship shell scripts and a
systemd unit. No Terraform, no managed instance groups, no Kubernetes,
no Helm charts.

## Local mode (laptop)

```
docker compose up                      # control plane + one node agent
xrlenv template build terminal-base    # build local image
xrlenv up                              # ready
```

`docker-compose.yaml` runs:
- `xrlenv-control` (gRPC + admin panel + sqlite volume)
- `xrlenv-node` mounted with `/var/run/docker.sock` so it drives the
  user's Docker Desktop

macOS: Docker only (CubeSandbox is not available; see plan).

## Reserved cloud VMs (GCP and AWS)

Both clouds use the same flow:

1. User provisions a VM through the cloud console (or `gcloud compute
   instances create` / `aws ec2 run-instances` if they prefer the
   CLI). Any Linux VM with Docker-installable kernel works.
2. User SSHes in and runs the appropriate bootstrap entry point.
   Phase 1.x slice 5 (B8.1, shipped 2026-05-11) ships the
   `xrlenv bootstrap` subcommand at `xrlenv/cli/bootstrap.py`
   — stdlib-only so it runs under any system Python 3.10+ on a
   fresh VM. The existing `deploy/bootstrap-{gcp,aws}.sh` scripts
   are now thin 3-line wrappers around the Python entry point so
   existing cloud-init snippets keep working. Recommended flow:
   ```
   # 1. Preview the plan first (touches nothing on the host):
   sudo -E bash deploy/bootstrap-gcp.sh <cp:port> --dry-run
   # or, equivalent direct:
   sudo -E python3 xrlenv/cli/bootstrap.py --target gcp \
       --xrlenv-repo "$(pwd)" --dry-run
   # 2. Run for real:
   sudo -E bash deploy/bootstrap-gcp.sh <cp:port>
   ```
   The classic curl form is still documented for bootstrap-of-
   bootstrap scenarios; the operator must SHA-256 the script before
   running it (spec 19 supply-chain rules).
3. Bootstrap entry point installs Docker, the `xrlenv` package
   (wheel / checkout / PyPI), and a systemd unit; writes
   `/etc/xrlenv/node.env` with the control-plane address + node id
   (the EnvironmentFile the systemd unit reads at startup). If
   `XRLENV_NODE_TOKEN` is set in the bootstrap environment, an
   additional mode-0600 systemd drop-in carries it to the daemon.
4. User appends the new node's address to `nodes.yaml` on the control
   plane, runs `xrlenv reload`. Node appears in `xrlenv nodes` and
   the admin panel within seconds.

### Bootstrap entry point (phase 1.x slice 5 onward)

The Python subcommand `xrlenv bootstrap` at
`xrlenv/cli/bootstrap.py` is the implementation. It runs stdlib-only
so it executes on a fresh VM before `xrlenv` itself is installed,
and the existing `deploy/bootstrap-{gcp,aws}.sh` shell entry points
remain as 3-line wrappers that `exec` the Python module as a flat
script with `--target {gcp,aws}` and `--xrlenv-repo <checkout>`.

```bash
# Recommended flow on a freshly-provisioned VM:
sudo -E bash deploy/bootstrap-gcp.sh <cp:port> --dry-run    # preview
sudo -E bash deploy/bootstrap-gcp.sh <cp:port>              # live
```

Step sequence (each step is idempotent via a `skip_if` predicate):

1. Docker install — `dnf install docker` on AL2023/RHEL/Fedora, the
   upstream Docker apt repo on GCP, `apt install docker.io` on AWS
   Ubuntu / Debian / linux-generic.
2. `systemctl enable --now docker`.
3. Ensure the `xrlenv` system user + docker-group membership.
4. Ensure `/opt/xrlenv`, `/etc/xrlenv`, `/var/lib/xrlenv`,
   `/var/cache/xrlenv` (+ harbor and build-context cache subdirs)
   exist with the right owner / mode.
5. Install Python 3.12+: probe `$XRLENV_PYTHON` → `python3.{14,13,12}`
   on `PATH` → distro package manager → uv-managed
   python-build-standalone fallback under `$install_root/python`.
   The distro-install step is `allow_failure=True` so the uv
   fallback picks up the slack on distros (Ubuntu 22.04, Debian 12)
   without python3.12 in their repos.
6. Create `/opt/xrlenv/.venv` using the resolved interpreter, then
   pip install xrlenv from `--xrlenv-wheel` / `--xrlenv-repo` / PyPI
   (priority order).
7. Verify `xrlenv.node` imports cleanly + the `xrlenv-node` console
   script lands on the venv PATH.
8. Write `/etc/xrlenv/node.env` (XRLENV_CONTROL_PLANE,
   XRLENV_NODE_ID, harbor + build-context cache paths) and install
   the systemd unit + token drop-in (mode 0600) when
   `XRLENV_NODE_TOKEN` is set.
9. `systemctl daemon-reload && enable && restart`.
10. Add `$SUDO_USER` to the docker group (opt-out via
    `--skip-operator-docker-group`).

Cloud-metadata auto-detect for the node id:
- **GCP**: HTTP GET against `metadata.google.internal` with the
  `Metadata-Flavor: Google` header.
- **AWS**: IMDSv2 two-step (PUT for a token, GET for the
  instance-id).
- **linux-generic**: no metadata; operator must pass `--node-id`.

`deploy/bootstrap-common.sh` and `deploy/_preflight.sh` are
retained as helper-function libraries for `deploy/refresh.sh`'s
"fast-path I just `git pull`-ed" flow. A future slice can fold
refresh into `xrlenv bootstrap --refresh-only` if the value
emerges.

### `nodes.yaml`

```yaml
nodes:
  - id: gcp-1
    cloud: gcp
    expected_address: 10.142.0.7        # informational; control plane never dials this
    expected_fingerprint: "sha256:7a4f..."   # spec 19 fingerprint binding (mandatory phase 0)
    bearer_token: ${XRLENV_NODE_TOKEN}
  - id: aws-1
    cloud: aws
    expected_address: 10.0.1.42         # informational only
    expected_fingerprint: "sha256:b2e1..."
    bearer_token: ${XRLENV_NODE_TOKEN}
```

This file is the operator's **roster of expected nodes** — it lets
the control plane pre-authorize bearer tokens, render placeholder
rows in the admin panel before nodes connect, and detect "node X
hasn't checked in" alerts. It is *not* a list of inbound endpoints
the control plane dials. All control-plane → node communication
rides the bidi gRPC stream the node agent opens **outbound** at
boot (spec 04, spec 07): create-sandbox, destroy, fetch-trajectory,
image directives, drain, and stats requests are framed as command
messages on that reverse stream and correlated by request id.
`expected_address` is purely a label.

Cloud VMs therefore need **no inbound firewall rules**; only egress
to the control plane's `grpc_server` port. This is critical given
the user's VM-only access (no admin to open ports). Spec 11's
`/healthz` and `/readyz` probes are exposed *on the control plane*
proxying to the node-agent stream — Slime polls the control plane,
not the node directly.

Bearer tokens are shared at bootstrap (one token per cluster, written
into the systemd unit's environment file). The shared-token risk is
bounded by spec 19's mandatory **fingerprint binding** — the
control plane rejects any `RegisterNode` whose live cloud-instance
fingerprint doesn't match the `expected_fingerprint` declared in
this file. Operators can opt into per-node tokens before phase 1
via `xrlenv tokens issue --node <id>`; phase 1 makes per-node
tokens the default and adds mTLS.

To remove a node cleanly: run `xrlenv drain <node>`, wait for the
drain to report "0 sandboxes remaining" or for the drain timeout to
elapse, then delete its row, `xrlenv reload`, then SSH in and
`systemctl stop xrlenv-node`. The drain protocol (spec 04) marks the
node unschedulable, lets running rollouts finish up to
`drain_timeout_s` (default 300 s), then force-destroys remainders
through the bounded-concurrency executor before exit. Without the
drain step, restarting a node leaves orphans the control plane has
to GC.

## Autoscale (phase 2)

When the user has admin access, a `NodeProvider` interface lets the
scheduler request capacity:

```python
class NodeProvider(Protocol):
    async def list_nodes(self) -> list[NodeRef]: ...
    async def request_capacity(self, n: int, hint: NodeHint) -> list[NodeRef]: ...
    async def release(self, node: NodeRef) -> None: ...
```

Implementations:
- `StaticNodeProvider` — phase 0; reads `nodes.yaml`.
- `GcpMigProvider` — phase 2; targets a managed instance group.
- `AwsAsgProvider` — phase 2; targets an autoscaling group.
- `KubernetesProvider` — phase 2; one pod per node-agent.

## Optional: cluster-wide registry mirror (phase 1)

When the user has a single VM they can dedicate to it, running an
in-cluster registry mirror (`distribution/distribution`, the same
binary that powers Docker Hub) eliminates redundant upstream pulls and
keeps cross-node image sharing fast over the LAN.

```
+------------+       +-----------------+         +-----------------+
|  upstream  | <---  | cluster mirror  |  <-->   |  node 1..N      |
|  registry  |       | (one VM)        |         |  docker daemon  |
+------------+       +-----------------+         +-----------------+
```

- One `distribution` instance, configured as a pull-through cache
  pointing at the upstream registry (Docker Hub, GHCR, ECR, GCR).
- Each node's `/etc/docker/daemon.json` adds the mirror as a
  `registry-mirrors` entry.
- First pull of an image cluster-wide hits upstream; all later pulls
  hit the mirror.

This is **documented as an optional pattern, not a feature**. The
control plane and node agent don't need to know whether a mirror is
in use; from their perspective it's still `docker pull`. Documented
here because it's the cheapest way to scale image distribution past
~5 nodes without admin access to a managed registry service.

The concrete implementation of this pattern lives in
`deploy/registry/` — `run-registry-mirror.sh` (the server, a
`registry:3` pull-through cache on a shared store) and
`deploy/registry/configure_docker_registry.sh` (the per-worker
`registry-mirrors` daemon.json merge), with operator instructions in
`deploy/registry/README.md` and the Sphinx "Registry mirror" page.

When combined with the per-node image cache manager (spec 15), the
mirror absorbs the cluster-wide cold-pull cost; the manager handles
per-node hot-set churn.

When **lazy image loading** (spec 15, phase 1) is enabled, the
mirror also serves eStargz-formatted blobs to stargz-snapshotter;
no extra deployment effort beyond using a registry that can return
range-requested chunks (`distribution/distribution` does this out
of the box).

## Garbage collection

Multiple, layered. Each layer assumes the others can fail.

1. **Per-sandbox TTL** — every sandbox carries
   `(template_default_ttl_s, optional_override)`. Coordinator emits
   destroy at expiry. Default 1 h.
2. **Node-agent startup GC** — enumerate live sandboxes via the
   backend; destroy any not in the local sandbox table.
3. **Control-plane reconcile GC** — on reconnect after outage, the
   control plane sends the canonical sandbox list per node; node
   destroys anything missing from it after a 60 s grace.
4. **Per-rollout run-dir rotation** — `~/.xrlenv/runs/<date>/`
   (canonical layout from spec 08 / spec 20: `meta.json` always,
   `trajectory.jsonl` only when `platform-jsonl` is in the sink
   chain, `coordinator.log`, `node.log`, `stub.log`,
   `env_adapter.log`, `network.log` (phase 1), `blobs/`) is
   rotated nightly; directories older than `retention_days`
   (default 14) are deleted. Configurable.
5. **Image cache prune** — node agent runs `docker image prune
   --filter until=24h` daily; per-instance SWE-bench images get a
   keep-list so we don't redownload them every day.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: shell-script bootstrap for GCP+AWS, static `nodes.yaml`,
  Docker-only, GC layers 1–5.
- **Phase 1**: cube install added to bootstrap (Linux+KVM nodes only),
  per-node mTLS certs, log/trajectory rotation tighter,
  `xrlenv bootstrap` subcommand replaces the curl-pipe path.
- **Phase 2**: NodeProviders for MIG/ASG/k8s, autoscale on queue depth.

## Cross-references

- Bootstrap-script integrity, token storage, secret handling →
  [spec 19](19-security-model.md)
- Canonical paths and retention matrix consumed by every GC layer
  → [spec 20](20-state-and-storage.md)
- Reverse-stream transport that supersedes any inbound port →
  [spec 21](21-node-control-protocol.md)
