# 19 — Security Model

## Purpose

XRLEnv runs **agent-generated commands inside sandboxes**. Even when
the operator trusts the consumer code, the policy itself is a model
that emits arbitrary shell, code, and tool calls. The platform's
security posture must assume those commands are adversarial-by-
default — not because the user is malicious, but because the model
will eventually emit something destructive by mistake.

This spec is the cross-cutting security contract every other spec
defers to. It is **load-bearing for phase 0**, not a phase-1
afterthought, because the auth and isolation defaults baked into
phase 0 are the ones future phases inherit.

## Threat model

Adversaries we design against, ranked from most likely to least:

1. **Buggy or jailbroken model output** — agent emits `rm -rf /`,
   leaks env vars to a paste site, fork-bombs, exfiltrates host
   secrets via a request to a control URL. *Most common; assume
   every long rollout produces something like this.*
2. **Compromised template image** (supply chain) — operator pulls
   `someorg/swebench-base` and the image runs a miner / backdoor
   on first command. *Probable for popular benchmarks; mitigation
   is image pinning + integrity verification.*
3. **Compromised asset / dataset** — qcow2, model checkpoint, or
   pip wheel cache fed into the sandbox carries a payload.
4. **Compromised consumer process** — credential theft on the GPU
   host. Consumer's bearer token is reused against the control
   plane to enumerate other tenants' rollouts.
5. **Operator-side mistake** — `--admin-bind 0.0.0.0:8080` exposes
   the read-only admin panel to the public internet because the
   operator forgot to SSH-tunnel.
6. **Insider abuse** — multi-tenant cluster, one consumer steals
   another's trajectory data. *Phase 2; out of scope for phase 0
   (single-tenant by assumption).*

Out of scope for *any* phase: hardware-level side channels,
microarchitectural attacks, GPU-side leakage, kernel zero-days.
We rely on the cloud provider's hypervisor and a stock kernel.

## Identities

Every actor has a typed identity and a scope:

| Identity | Issued at | Talks to | Default scope |
|---|---|---|---|
| `node-agent` | bootstrap script (spec 09) | control plane (outbound stream) | report heartbeats, accept commands for own node, write trajectory files for own rollouts |
| `consumer` | operator-issued via `xrlenv tokens issue consumer` | control plane gRPC | start / step / cancel rollouts the consumer owns; read trajectories the consumer owns |
| `operator` (CLI) | operator login on control-plane VM | control plane gRPC + admin HTTP | full read; gated write actions (`drain`, `kill-sandbox`, `evict-image`) |
| `admin-ui` | shares operator scope, but bound to localhost by default | admin HTTP | read-only in phase 0; gated write in phase 1 |
| `template-publisher` | repository-level (signing key) | image registry / asset hosts | sign template manifests + image refs (phase 1) |
| `external-reward-service` | per-template config | inbound HTTP from control plane | called per rollout; receives sealed trajectory |

Phase 0 ships a single shared bearer token per cluster (the
`XRLENV_NODE_TOKEN` already in spec 09) and a separate
`XRLENV_CONSUMER_TOKEN` for consumers. Phase 1 splits these into
per-identity tokens with explicit scopes; phase 2 adds OIDC and
project / tenant scoping.

### Phase-0 node token: fingerprint binding (mandatory)

A shared cluster-wide bearer is a real operational weakness if it
ever leaks: anyone holding the bytes could impersonate any node.
Phase 0 mitigates this with **mandatory fingerprint binding** at
the control plane:

- Each node entry in `nodes.yaml` (spec 09) declares an
  `expected_fingerprint` — the SHA-256 of the node's
  cloud-instance metadata (`(cloud, instance_id, project,
  zone)`) for cloud VMs, or `(hostname, machine-id)` for
  local/laptop nodes.
- On `RegisterNode`, the node sends its live fingerprint
  alongside the bearer token. The control plane:
  - Looks up the `nodes.yaml` row by `node_id`.
  - Verifies `bearer == XRLENV_NODE_TOKEN`.
  - Verifies the live fingerprint matches `expected_fingerprint`.
  - Rejects with `RegisterDenied("fingerprint_mismatch")` and an
    audit event `auth.fingerprint_mismatch` if either fails.
- Audit and structured logs always carry **both** identity hints:
  `token_digest_hint` (first 6 chars of the token's SHA-256) and
  `node_fingerprint_hint` (first 6 chars of the fingerprint
  digest). A leaked token used from an unexpected fingerprint is
  visible at the audit layer immediately, and rejected before it
  reaches `RegisterNode` body handling.
- The fingerprint is derived from cloud metadata the node fetches
  from the metadata service it does *not* otherwise expose to
  sandboxes (spec 07 cloud-metadata block applies to sandboxes,
  not the node agent itself).

This does not eliminate the shared-token risk — a leaker who also
controls the legitimate node still passes — but it bounds the
blast radius to the leaked node's identity only. Operators who
need stronger guarantees can opt into per-node tokens early via
`xrlenv tokens issue --node <id>`; the bootstrap script in spec
09 supports per-node tokens whenever the operator generates them.

### Phase 1: per-node tokens default

Phase 1 makes per-node tokens the default. The bootstrap path
becomes:

1. Operator runs `xrlenv tokens issue --node <id>` on the
   control plane.
2. The CLI prints a one-shot token tied to the node's expected
   fingerprint.
3. Operator pastes the token into the bootstrap form on the
   target VM.
4. mTLS replaces bearer at the same time; the bearer is
   transitional during the rollout window.

Until phase 1 lands, fingerprint binding above is the floor.

## Token lifecycle

- **Bootstrap**: tokens generated on the control-plane VM (`xrlenv
  init` writes `~/.xrlenv/secrets/` with mode `0600`); the
  bootstrap scripts in spec 09 read from there. Never embedded
  in a public URL or curl-piped from the network without a
  detached signature check (see "Image and asset supply chain"
  below).
- **Storage**: tokens live in `~/.xrlenv/secrets/<role>.token` on
  the control plane and the systemd `EnvironmentFile` on each
  node. Mode `0600`. Never logged at INFO; INFO logs include the
  *first 6 chars* of the token's SHA-256 digest as an identity
  hint, never the token bytes.
- **Rotation**: `xrlenv tokens rotate <role>` writes a fresh
  secret to `<role>.token`, prints it once, and refuses to print
  it again. Two cutover modes:

  - **Immediate cutover (default).** The prior token is dropped
    from `TokenStore` on the next `maybe_reload`; calls carrying
    it 401 starting with the next RPC. This is the
    security-default; it matches "old token invalid the moment
    new one is issued."
  - **`--grace <duration>` overlap.** Writes a sibling
    `<role>.token.previous.json` (mode 0600) holding the prior
    token + an ISO-8601 `grace_until` timestamp. `TokenStore`
    accepts both old and new tokens until wall-clock crosses the
    expiry; entries past their expiry are evicted lazily by the
    verify path. Reserve this for deployment-rollover cases
    where the new token isn't yet propagated everywhere.

  The P1.x slice 2 shape revises the phase-0 placeholder "fixed
  60 s overlap" — operators now choose the window explicitly.
- **Revocation**: `xrlenv tokens revoke <token-id>` appends a
  record to `~/.xrlenv/secrets/revoked.json`
  (`{"token_id", "revoked_at"}` list). `token_id` is the 12-char
  SHA-256 prefix of the bearer (its first 6 chars match the log
  `digest_hint`, so an operator quoting an audit-log fingerprint
  resolves to a unique row via revoke-by-prefix). The control
  plane picks revocations up on its next mtime-driven
  `maybe_reload`; `verify()` then returns `None` for any matching
  identity. Phase 2 swaps the on-disk list for a Redis-backed
  index so the check stays O(1) at scale.

## API authz scopes

The gRPC API (spec 03) tags every method with a coarse scope.
Phase 0 implements three coarse scopes plus a small set of
explicit grant-only sub-scopes that a coarse scope does *not*
imply:

| Coarse scope | Methods |
|---|---|
| `node.report` | `RegisterNode`, `Heartbeat`, `SandboxEvent`, `TrajectoryChunk`, `Stats`, `LogTail`, `CommandReply` (the entire reverse stream) |
| `consumer.rollout` | `Rollout`, `Heartbeat` (consumer), `CancelRollout`, `CancelGroup`, `Replay`, `Healthz`, `Readyz`, `Invoke` (function-call mode, gated additionally by template `trust_level`) |
| `operator.admin` | `ListNodes`, `ListSandboxes`, `ListRollouts`, `RegisterTemplate`, `Capacity`, `PortForward.*`, plus the destructive admin actions added in phase 1 |

| Sub-scope | Default holder | What it grants |
|---|---|---|
| `port_forward.allow` | `operator.admin` only | issue `PortForward` against a sandbox; consumers do **not** receive this by default and operators may grant it explicitly via `xrlenv tokens grant <token> port_forward.allow` (spec 07) |
| `audit.read` | `operator.admin` only | read the `audit` table via the `/audit` admin view; phase 1+ |
| `template.publish` | `operator.admin` only | sign / register manifests as a trusted publisher (phase 1+) |

Consumer tokens cannot call `RegisterTemplate`; node tokens cannot
call `Rollout`; admin tokens can do everything except impersonate
a node (the node identity is checked against the `RegisterNode`
node fingerprint). Sub-scopes are additive: a consumer with
`port_forward.allow` granted still cannot call `RegisterTemplate`
because that requires `operator.admin`.

`Healthz` and `Readyz` are both `consumer.rollout` so Slime/verl
adapters polling node health from the consumer side work without
extra grants.

## Admin panel exposure

- Default bind: `127.0.0.1:8080`. The CLI prints the SSH-tunnel
  command on startup so the operator's first interaction does not
  involve a public bind.
- `--admin-bind 0.0.0.0:8080` is allowed only when
  `--admin-allow-public` is also passed; otherwise the control
  plane refuses to start with a clear error. This guard cannot be
  defaulted-on by config file: it must be set on the command
  line so that CI / autostart configurations cannot silently
  expose the panel.
- Phase 0: the panel is read-only and unauthenticated when bound
  to localhost. Public binds were rejected outright in phase 0.
- Phase 1.x slice 3 (B7.3, shipped 2026-05-11): HTTP basic auth
  with two roles —
  - **viewer** (token shape `read_<urlsafe32>`) — read-only access
    to every GET route (`/`, `/nodes`, `/builds`, `/images`,
    `/rollouts`, `/api/build/plans/<id>`, etc.).
  - **operator** (token shape `write_<urlsafe32>`) — full access
    including the three POST write routes (`/api/build/apply`,
    `/api/build/cancel`, `/api/build/calibrate`) plus any future
    destructive admin action.
  A single middleware classifier gates every request: `GET` →
  `consumer | viewer | operator`, anything else → `operator`.
  `/healthz` and `/static/*` stay open (load balancers + the
  pre-auth challenge page). Identity (role + `owner_id`) is resolved
  **from the token alone**, so the basic-auth `username` is cosmetic
  — a user just pastes their token. (Dropping the prior
  username-must-match-role check grants no privilege: the role is the
  verified token's role, never the typed username.) The existing
  `Authorization: Bearer <token>` CLI shape remains accepted in
  parallel — same middleware, same role gate. The bind guard now
  permits `--admin-allow-public` when a `TokenStore` is wired with at
  least one credential; a public bind with no auth still raises
  `AdminBindError`.
- Multi-user (per-owner scoping): a per-user `consumer` token — the
  one a user keeps in `.env` to submit jobs — also opens the admin
  panel **read-only and scoped to its own `owner_id`** (its rollouts /
  sessions / sandboxes; another owner's id → 404). So a user needs one
  token. `viewer` is the watch-only read role for people who don't
  submit; `operator` is the un-scoped admin (sees all owners) and the
  only role allowed writes. Owner scoping is applied from the verified
  token's `owner_id`, so it can't be spoofed by a crafted request. The
  public-bind guard still requires a shared `viewer`/`operator`
  role-token (a management-capable identity) before exposure.
- Phase 2: OIDC + RBAC + per-page audit log.

## Sandbox runtime hardening

For the Docker backend (spec 01):

- `--cap-drop=ALL --cap-add=` only the capabilities the template
  declares it needs (default empty list).
- Default seccomp profile `xrlenv-default.json` (a tightened
  variant of Docker's default, blocking `keyctl`, `bpf`,
  `add_key`, `mount`, `umount2`, `userfaultfd`).
- `--security-opt no-new-privileges`.
- Rootless or `userns-remap` where the kernel supports it. Phase
  0 documents the userns option but doesn't require it (cloud VM
  kernel coverage is uneven). **Phase 1.x revision (B5.4,
  shipped 2026-05-10)**: userns-remap is per-acquire opt-in on
  the raw-container path (`Client.acquire_container(
  userns_mode="remap")`), default `"host"`. The original "phase
  1 makes userns the default" plan would have silently broken
  benchmark images that need in-container root for installing
  packages, chmod-ing bind-mounted paths, etc. Operator must
  configure daemon-level `userns-remap` in
  `/etc/docker/daemon.json` separately; without daemon config,
  `userns_mode="remap"` is a silent no-op.
- Never `--privileged`. Never `-v /var/run/docker.sock` mounted
  into a sandbox. The catalog refuses to register a template that
  requests either.
- `cgroupv2` resource limits matching `ResourceSpec`; OOM killer
  scoped to the container.
- `--read-only` root filesystem with `tmpfs` for `/tmp` and
  declared writable mounts. Templates that need a writable root
  set `rootfs_writable: true` and the operator sees a warning at
  registration.

For CubeSandbox (phase 1): hypervisor-enforced isolation
supersedes most of the above; the spec still applies seccomp /
caps inside the microVM's guest userspace.

For Function-Call mode (spec 01): `trust_level` declared in the
manifest is enforced — `trusted-only` pools refuse invocations
sourced from agent-generated commands unless the consumer
explicitly opts in.

## Bind mounts and shared caches

The node agent's mount allowlist (spec 04) is the gate for
**template-declared** mounts (anything in a manifest's
`resources.mounts`). Defaults:

- Allowed prefixes: `/opt/xrlenv-shared/`, `/var/cache/xrlenv/`,
  the per-sandbox scratch placeholder `{sandbox_scratch}`, and
  any path the operator explicitly adds.
- Denied prefixes (always, even with operator override):
  `/etc`, `/root`, `/home`, `/var/run/docker.sock`, `/proc`,
  `/sys`, `/dev` (except the controlled `/dev/null`, `/dev/zero`,
  `/dev/random` set the runtime injects).
- Read-write mounts must be either operator-pinned read-only
  caches or per-sandbox scratch dirs. A template's request for a
  read-write mount of a shared path is rejected with
  `MountDenied("rw_shared")`.

### Platform-injected plug-in roots (D22)

External plug-in roots discovered via `XRLENV_TEMPLATE_DIRS` /
`xrlenv.benchmarks` entry-points are **platform-injected** mounts:
the operator chose them at node boot via env-var or pip-install,
not a benchmark author writing arbitrary mount paths into a
manifest. They are mounted read-only at indexed
`/opt/xrlenv-extras/<idx>` prefixes (see spec 06 "Plug-in
resolution and the in-sandbox import path") and bypass the
template-mount allowlist above — re-running it would force an
exception list for `/home/...` paths the dev workflow relies on.

A narrower **system-path guard** still applies in
`xrlenv.control.template_discovery._resolve_plugin_root`: roots
resolving to `/etc`, `/proc`, `/sys`, `/dev*`,
`/var/run/docker.sock`, or `/` (exact match) are dropped with a
warning regardless of who configured them. The guard runs at
discovery time; rejected roots fall back to manifest-only
registration (the manifest still registers, but the adapter must
reach the sandbox via image-bundled code).

Operator-visible signal at node startup: a structured
`plugin_root.mounted host_path=<...> container_target=/opt/xrlenv-extras/<idx> ro=true`
INFO line is logged per mounted root by `xrlenv-node serve` and
`xrlenv.control.runtime.build_local_runtime` (one line per entry in
`extra_plugin_roots`). Per-row state-store audit events for the
same data, plus `plugin_root.collision` detection when two roots
ship the same `<category>/<name>`, are a phase-1.5 follow-on —
they need a node→control audit-event RPC that today only the
control plane has wired.

Shared package caches (`/var/cache/xrlenv/{pip,uv,npm}` from spec
06) are gated by a per-template `cache_trust_mode`:

| Mode | Behavior |
|---|---|
| `readonly-prebaked` (default) | Cache is mounted read-only; never written by sandboxes; populated only by `xrlenv cache prefetch <template>` running on the operator side |
| `rw-trusted-template` | Read-write; allowed only when the manifest is signed (phase 1) by a publisher in the operator's trust list |
| `rw-untrusted-disabled` | Sandbox sees no shared cache; package manager downloads run inside the sandbox each time. Default for any template marked `network_policy: open` and any template loaded from a public registry without a signed manifest |

## Network egress

Spec 07 already defines `none / open / egress-allowlist`. Security
additions:

- **Cloud metadata blocking is mandatory in every policy.**
  Sandboxes cannot reach `169.254.169.254`, `metadata.google.
  internal`, or the Azure equivalent regardless of policy. The
  block is enforced at the node-agent's iptables / eBPF layer,
  not just by the per-sandbox resolver, so a sandbox cannot bypass
  it by hardcoding the IP. Phase 0 ships a self-test
  (`xrlenv selftest metadata-block`) that runs on each node at
  boot and refuses to register the node if the block does not
  hold.
- **RFC1918 / link-local default-deny under `egress-allowlist`.**
  Private network ranges are blocked unless explicitly listed.
  Public CDNs are allowed by hostname only; the per-sandbox
  resolver rewrites CNAME resolutions to a single IP and the
  iptables rule pins to that IP for the resolver TTL.
- **Direct-IP connections are denied by default** under
  `egress-allowlist`; only hostname-form destinations resolve.
  Templates that genuinely need direct-IP (rare) declare
  `egress_allowlist[].allow_direct_ip: true` and the operator
  sees a registration warning.
- **Audit log**: phase 1 ships per-sandbox connection logs
  (sni, dest host, dest port, bytes) to the per-rollout
  directory under `network.log` so that "what did this rollout
  call out to?" is answerable post-hoc.

## Image and asset supply chain

- **Image refs are pinned by digest, not tag.** The catalog
  rewrites `image.ref` to `image@sha256:...` at registration.
  Tags are convenience labels; the digest is the contract. A
  template that uses a mutable tag without a digest is registered
  with a warning and re-pinned to whatever digest the operator's
  registry returned at register time.
- **Asset integrity**: every `assets:` entry carries a `sha256`
  (already in spec 06). The cache manager refuses to mount an
  asset whose checksum diverges from the manifest. Partial
  downloads (e.g. interrupted curl) are retried; corrupted
  finals are quarantined under `<extract_to>.bad-<ts>/` and the
  warmup directive reports `asset_fetch_failed:checksum`.
- **HTTP fetchers refuse plaintext.** `http://` asset URLs are
  rejected; `https://` is required. The `s3://` and `gs://`
  fetchers use SDK-default TLS.
- **Bootstrap script integrity** (spec 09): the `bootstrap-*.sh`
  scripts are served from the project's GitHub release with a
  detached signature; the docs show the SHA-256 the operator
  should verify *before* piping to bash. Phase 1 ships a
  `xrlenv bootstrap` subcommand that fetches + verifies in one
  step.
- **Trusted publisher list** (phase 1): operators declare
  trusted Docker registries / signing keys; templates whose
  image came from outside the list are loaded with
  `cache_trust_mode: rw-untrusted-disabled` regardless of
  manifest.

## Audit logging

Every action that mutates state or could be sensitive emits a
structured event:

- `auth.token_used` (identity hint, scope, method, source ip) — **OFF by
  default.** Written one-per-authenticated-RPC, it dominates the `audit` table
  and churns the state-store write path at scale (a 13.2M-row / 2.6 GB prod
  incident, 2026-07-30), so the interceptor suppresses it unless
  `XRLENV_AUDIT_AUTH_SUCCESS=1`. Successful access is reconstructable from node
  registration + `raw_rollouts`; enable this only when a full per-RPC success
  trail is required.
- `auth.denied` (reason: `bad_token`, `revoked`, `wrong_scope`) — **always
  recorded** (the high-value, low-volume security signal).
- `admin.action` (operator, action, target id, before/after)
- `admin.public_bind_warned` and `admin.public_bind_started`
- `template.registered` (digest, signed_by, mount_violations)
- `mount.denied` (template, host_path, sandbox_path, reason)
- `egress.metadata_block_test` (node, result)
- `network.egress.denied` (sandbox_id, dest, reason) — phase 1
- `function.invoke` (already in spec 01) — flagged when
  `trust_level=untrusted-allowed`
- `secret.read` (which secret, by which identity)

These are written to the StateStore's `audit` table (spec 20
schema; separate from the generic `events` table) and mirrored
into the per-rollout directory when applicable. Audit events are
also exported via the `/metrics`-adjacent counter
`xrlenv_audit_events_total{event,result}` so an operator can alert
on bursts of `auth.denied` without needing the log pipeline.

**Retention** (single source of truth — spec 20 retention matrix
is canonical): audit rows retain for **90 days by default**,
operator-configurable via `audit_retention_days` in
`~/.xrlenv/config.yaml`. The generic events-log retention (14
days) does **not** apply to audit; the two tables roll on
independent schedules. Phase 2 ships an optional signed archive
to object storage at `<cold_tier>/audit/` for compliance-grade
retention beyond the configured window.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: shared bearer tokens with three scopes; localhost
  admin bind by default with explicit `--admin-allow-public`
  guard; mount allowlist with default-deny system paths;
  metadata-IP block + boot self-test; image digest pinning;
  asset SHA-256; default seccomp + cap-drop; no userns required;
  audit events for token use, mount denial, admin actions, public
  binds.
- **Phase 1**: mTLS with per-node certs; per-consumer tokens with
  rotation/revocation; egress allowlist; signed template
  manifests; userns-remap as per-acquire opt-in on the raw-
  container path (default `"host"` — benchmark-compat); trusted
  publisher list;
  per-sandbox network audit logs; admin basic auth on public bind.
- **Phase 2**: OIDC, RBAC, multi-tenant project scoping; signed
  audit log (append-only with hash chain); per-template policy
  bundles distributed via the control plane; egress through a
  per-template proxy with structured logs.

## Cross-references

- **Spec 04**: outbound-only transport; mount allowlist; node
  identity.
- **Spec 06**: `cache_trust_mode`, signed publisher list,
  `network_policy`, asset checksums.
- **Spec 07**: network policies, metadata block enforcement.
- **Spec 09**: bootstrap script integrity, `nodes.yaml`
  expected-roster role.
- **Spec 13**: admin bind defaults, public-bind guard, audit log
  surfacing.
- **Spec 15**: image and asset integrity at the cache layer.
- **Spec 16**: benchmark analysis runs untrusted images — see
  M10 in the audit; an analysis-mode "isolation profile" lives
  here.
