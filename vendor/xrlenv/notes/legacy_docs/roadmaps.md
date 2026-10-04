# Roadmaps

Items below are designed and specced but not yet implemented. The rough
ordering matches the planned delivery sequence. For what is shipping today,
see {doc}`current_status`.

**Trainer adapters (phase 1):**
Slime (`xrlenv/adapters/slime.py`) and verl (`xrlenv/adapters/verl.py`)
thin-shim adapters. These are optional imports — the core SDK is already
designed so the adapters are minimal. Slime is the primary target.

**CubeSandbox backend (phase 1):**
Linux/KVM microVM backend for stronger isolation. Requires KVM-capable host.
The backend seam in `xrlenv/backends/` is already defined; the implementation
follows after Docker is fully validated.

**Warm pools and lazy image loading (phase 1):**
Pre-started sandboxes reduce first-step latency. Lazy image loading
(`lazy_load: preferred | required` in `manifest.yaml`) streams layers from
a registry as the sandbox starts.

**Redis state store (phase 1):**
Replaces SQLite for multi-host deployments with higher write concurrency.

**Egress allowlist (phase 1):**
`network: egress-allowlist` in templates; enforced at the node-agent's
iptables/eBPF layer.

**Real benchmark adapters (follow-on benchmark slice):**
Wire `TerminalBench2EnvAdapter` to the harbor-framework package; populate
`tasks/` with the real upstream task catalog. Same for SWE-bench and OSWorld.

**mTLS and per-node certs (phase 1):**
Node identity upgrades from fingerprint binding + shared bearer token to
per-node TLS certificates and mTLS on the gRPC stream.

**Kubernetes / autoscale (phase 2):**
`k8s`/MIG/ASG autoscaling. Phase 0 uses manually provisioned reserved VMs.

**Sandbox sessions with preemption-safe resume (phase 3):**
Spec 18 sessions decouple sandbox identity from rollout identity, enabling
checkpoint-and-resume across preemptions.
