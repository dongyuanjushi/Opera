# 07 — Networking

## Purpose

Define how sandboxes reach the network (or don't), how the control
plane talks to node agents, and how a trainer reaches an
inside-sandbox service when needed.

## Sandbox network policies

The policy is the composition of two orthogonal axes:

1. **External egress** — what the sandbox can reach off-node.
2. **Loopback** — what the sandbox can reach inside its own
   network namespace.

Templates declare a single `network_policy` value; the table
below shows what each value compiles to.

| Policy | Phase | External egress | Loopback | Cloud metadata |
|---|---|---|---|---|
| `none` | 0 | denied | **allowed** (in-sandbox services on `localhost:*` work normally) | denied |
| `open` | 0 | allowed | allowed | denied |
| `egress-allowlist` | 1 | allowed only to declared hosts/CIDRs | allowed | denied |

Loopback is **always allowed** regardless of policy, because every
template's in-sandbox services (browser ↔ X server, EnvAdapter ↔
controller, pyrepl, OSWorld VNC, etc.) talk to each other via
`localhost`. Earlier drafts of this spec said `none` "drops
loopback for non-essential ports" — that was wrong; it would
break every multi-service template.

### Enforcement

- **Docker, `none`**: container network = `none`. The container
  has lo only; no veth, no bridge. In-sandbox services bind to
  `127.0.0.1` and reach each other normally.
- **Docker, `open`**: default Docker bridge with NAT. The
  metadata-IP block (below) still applies.
- **Docker, `egress-allowlist`** (phase 1): per-sandbox network
  namespace + iptables OUTPUT chain that ACCEPTs only matching
  destinations, REJECTs the rest. DNS allowlist via a per-sandbox
  resolver that only answers for whitelisted names.
- **CubeSandbox**: equivalent semantics enforced via `CubeVS` (eBPF).
  More performant; same operator API.

### Cloud metadata block (mandatory, every policy)

Sandboxes can never reach `169.254.169.254` (AWS / Azure) or
`metadata.google.internal` regardless of `network_policy`. The
block is enforced at the node-agent's host-side iptables / eBPF
layer so a sandbox cannot bypass by hardcoding an IP. Spec 19's
boot self-test (`xrlenv selftest metadata-block`) verifies this
holds before the node accepts work.

### Egress allowlist format

```yaml
egress_allowlist:
  - host: "api.github.com"
  - host: "*.pypi.org"
  - cidr: "10.0.0.0/8"      # internal services (RFC1918 — see "Default-deny" below)
  - host: "registry.npmjs.org"
    ports: [443]
    allow_direct_ip: false  # default false; flipping it triggers a register-time warning
```

Wildcards via DNS resolver suffix-match. CIDRs via iptables/eBPF rule.

### DNS resolution semantics

Allowlist enforcement happens at two layers — DNS (the resolver
refuses to answer for non-allowlisted hostnames) and iptables
(packets to non-allowlisted IPs are dropped). The two cooperate
to handle CDN-fronted hosts and CNAME indirection cleanly:

- **Resolver TTL**: the per-sandbox resolver caches answers for
  `min(record_ttl, dns_cache_max_s)` (default 60 s). Short TTLs
  prevent stale IPs from outlasting the policy; the cap on long
  TTLs prevents one resolver answer pinning an IP after the
  remote service has rotated.
- **CNAME chains**: a CNAME to a non-allowlisted host fails
  resolution (the resolver follows the chain and refuses if any
  link is outside the allowlist). This stops the
  `vendor.example.com → cdn.notallowed.net` bypass.
- **IP pinning**: when the resolver answers an allowlisted name,
  the iptables ACCEPT rule pins the *answered IP* for the
  resolver TTL. This means a hostile DNS answer cannot redirect
  to an arbitrary IP — the sandbox can only reach IPs the
  sandbox-local resolver returned.
- **Direct-IP connections**: blocked by default. A template that
  needs to dial a literal IP (rare) sets
  `egress_allowlist[].allow_direct_ip: true` and the operator
  sees a registration warning. Even with the flag, the IP must
  match a declared CIDR.

### Default-deny ranges

Under `egress-allowlist`, the following ranges are blocked unless
explicitly listed in `egress_allowlist`:

- RFC1918: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`
- Link-local: `169.254.0.0/16`
- Loopback (off-sandbox): `127.0.0.0/8` outside the sandbox's own ns
- IPv6 ULA: `fc00::/7`
- IPv6 link-local: `fe80::/10`

Templates that legitimately need to reach an internal service
declare its CIDR explicitly, as in the example above.

### SNI proxy option (phase 2)

For templates whose allowlist is fundamentally hostname-shaped
(deep-research agents that visit hundreds of arxiv / GitHub /
search hostnames), phase 2 ships an optional per-template SNI/HTTP
proxy: the sandbox routes egress through a node-local proxy that
enforces the allowlist on TLS SNI rather than IP. This trades a
hop for hostname-precise enforcement; templates opt in with
`network_policy: egress-allowlist-sni`. Phase 2; not phase 1.

### Audit (phase 1+)

Every denied egress emits `network.egress.denied` (spec 19) into
the per-rollout `network.log` and the audit table. Allowed flows
are summarized (host, port, bytes, count) for forensics without
per-packet logging.

## Port forwarding

For debugging, sometimes a developer wants to reach an HTTP service
inside a sandbox (e.g. open the OSWorld VNC view). The SDK exposes:

```python
forward = await session.port_forward(
    internal_port=5901,
    ttl_s=600,                # forward auto-revokes after this
)
# forward.url -> "https://control-plane.example.com:8443/forward/<token>/" (proxied; default)
# forward.expires_at, forward.revoke()
```

### Default: control-plane proxied, not host-bind

The default port-forward implementation is a control-plane proxy:
the URL points at the control plane, which holds an authenticated
HTTP/WS reverse-proxy session over the spec-21 reverse stream into
the sandbox. The sandbox never gets a host-bound port. This means:

- The forward URL inherits the control plane's TLS and auth
  (operator identity required).
- Removing a node, draining, or losing connectivity revokes the
  forward automatically (the reverse stream is the carrier).
- The forward is auditable: every request lands in the audit log
  with `network.port_forward.request`.

Operators who specifically need a host-bind port (legacy
debugging, performance-sensitive flows) opt in:

```python
forward = await session.port_forward(
    internal_port=5901,
    bind="node-host",          # node-host | control-plane (default)
    bind_host="127.0.0.1",     # only "127.0.0.1" allowed by default
    ttl_s=600,
)
# forward.url -> "http://127.0.0.1:34123"  (port-forward over SSH tunnel)
```

`bind_host: "0.0.0.0"` requires the operator-side flag
`--allow-public-port-forward` on the node-agent (mirroring the
admin-panel public-bind guard) — refused without it, even if the
operator token authorizes the forward.

### Authorization

- `session.port_forward(...)` is part of the `operator.admin`
  scope (spec 19). Trainer tokens cannot forward by default; an
  operator can explicitly grant a trainer the
  `port_forward.allow` scope via `xrlenv tokens grant ...`.
- A forward inherits the rollout's `owner_id` / `project_id`;
  phase 2 admin panel filters expose forwards per-tenant.

### Lifecycle

- `ttl_s` (default 600 = 10 min) caps the forward's lifetime; the
  control plane revokes when expired.
- Forward count is capped per-node (`max_concurrent_port_forwards`,
  default 1024) and per-token (default 16). Excess raises
  `PortRangeExhausted` (host-bind path) or `ForwardQuotaExceeded`
  (proxied path).
- `xrlenv forwards list` enumerates active forwards (operator
  scope); `xrlenv forwards revoke <id>` cancels one.
- Every create / revoke emits `network.port_forward.created` /
  `network.port_forward.revoked` audit events with operator
  identity, target sandbox, internal port, bind, TTL.

### Implementation

In Docker:
- `bind: control-plane` — node agent multiplexes a TCP/HTTP
  proxy over the spec-21 reverse stream; no host port allocated.
- `bind: node-host` — `--publish <bind_host>:0:5901`.

In Cube:
- `bind: control-plane` — CubeProxy route; same multiplexing
  semantics.
- `bind: node-host` — host bind via Cube's port-publish with the
  same `bind_host` constraint.

Phase 0 ships the proxied default and `127.0.0.1` host-bind;
public host-bind requires phase-1 operator-side flag flipping.

## Cross-sandbox traffic

Disallowed by default. Phase 0 places each sandbox on its own
isolated network (Docker bridge per sandbox, microVM tap interface
per sandbox in Cube). Phase 2 leaves a hook for multi-agent rollouts:
a `RolloutGroup` can request a shared private subnet across its
sandboxes.

## Control-plane / node-agent traffic

- **Direction**: outbound from node agent to control plane only.
  Cloud VMs only need *egress* allowed; no inbound firewall edits.
  Critical given the user has VM-only access (no admin to open ports).
- **Transport**: gRPC over TLS. Phase 0 uses a shared bearer token
  set during bootstrap; phase 1 adds mTLS with cert rotation.
- **Trainer ↔ control plane**: same gRPC endpoint, separate auth
  scope. Trainer connects from the GPU host; the orchestrator's
  endpoint must be reachable.

## DNS

- Sandboxes get a node-local resolver (the node agent itself for
  allowlist enforcement; a stock systemd-resolved for `open`).
- The resolver does NOT leak `metadata.google.internal` /
  `169.254.169.254` to the sandbox — cloud metadata service is firewall-
  blocked from inside any sandbox to prevent SSRF-style escalations.

## Phase ladder

> Authoritative phase ownership lives in spec 00's phase matrix. The ladder below only lists this spec's local deliverables — when in doubt, the matrix wins.


- **Phase 0**: `none` and `open` policies; per-sandbox isolation
  network; port-forward; metadata-service block.
- **Phase 1**: `egress-allowlist` with DNS allowlist; mTLS for control
  plane; cube networking via eBPF.
- **Phase 2**: shared subnets for multi-agent groups; egress through a
  per-template proxy with audit log.
