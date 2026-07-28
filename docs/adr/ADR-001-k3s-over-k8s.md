# ADR-001 — k3s instead of full Kubernetes

**Status:** Accepted · **Date:** 2026-07-28

## Context

The project needs container orchestration for Phase 2: scheduled and
event-triggered pipeline runs, a webhook receiver, stateful Neo4j, and an
observability stack. The original design specified "Kubernetes (kind/minikube)".

Constraints that actually bind:

- It must run on a laptop or one small VM, alongside an IDE and a browser.
- It must be demoable live in an interview — no 10-minute cluster bootstrap.
- The Kubernetes *skills* need to be real and transferable. A toy abstraction
  that isn't Kubernetes would defeat the purpose.

## Decision

Use **k3s** as the target orchestrator, single-node, with `k3d` for throwaway
dev clusters.

## Rationale

| | k3s | kubeadm / kind / minikube |
|---|---|---|
| Control plane RAM | ~512 MB | ~1.5–2 GB |
| Binary | one, ~70 MB | multiple components |
| Time to ready | ~30 s | 2–5 min |
| Ingress | Traefik bundled | install separately |
| Storage | local-path bundled | install a provisioner |
| API | **identical** | identical |
| CNCF conformant | **yes** | yes |

The decisive point is the last two rows: k3s is a *packaging* difference, not
an API difference. Every manifest, Helm chart, RBAC rule and probe written
here applies unchanged to EKS/GKE/AKS. Nothing is learned in a k3s-shaped way.

The ~1.5 GB of control-plane RAM saved is roughly the entire Neo4j budget.
On a 4 GB target that's the difference between the stack fitting and not.

k3s also removes etcd by default (SQLite-backed), which is a real reduction in
moving parts for single-node — and a real limitation for HA, noted below.

## Consequences

**Positive**
- Whole stack fits in ~2.8 GB peak. Demoable anywhere.
- Bundled Traefik and local-path cut two install steps and two failure modes.
- The `HelmChart` CRD lets the cluster bootstrap itself from a manifest
  dropped in `/var/lib/rancher/k3s/server/manifests/` — a clean demo.
- Faster iteration: cluster teardown/rebuild is seconds.

**Negative**
- Single-node means no real scheduling, affinity, PDB or multi-node failure
  behaviour is exercised. Be honest about this — those are things read about,
  not done.
- SQLite datastore isn't HA. Irrelevant here; would matter in production.
- Some ecosystem charts assume a full cluster (StorageClass names, LB types)
  and need value overrides.

**Mitigation for the negative**
Document in the README what would change for multi-node/managed: swap
local-path for a CSI driver or Longhorn, add PDBs and anti-affinity, move
Neo4j to a managed instance or a clustered deployment, enable the k3s embedded
etcd or an external datastore. Being able to describe the delta demonstrates
the understanding that running it multi-node would have demonstrated.

## Alternatives rejected

- **kind** — good for CI, awkward for a persistent demo environment; storage
  and ingress need extra setup, and the Docker-in-Docker layer confuses live
  demos.
- **minikube** — heavier than k3s for the same result; the VM driver
  compounds the memory problem.
- **Docker Compose only** — considered seriously. Rejected because Jobs,
  CronJobs, probes, RBAC and Helm are a meaningful part of what this project
  is meant to demonstrate, and Compose has no equivalent.
- **Managed cloud cluster** — costs money monthly and can't be demoed on a
  plane. May be worth one weekend later to show it deploys unchanged, which is
  itself the point of choosing conformant k3s.
