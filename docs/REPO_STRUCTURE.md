# Organized Repository Structure

The compile's destination structure (kernel + packages + selective
satellites). Repos map in via the Convergence Matrix; blockers live in
the Gap Directory.

```
crow-os/                       # kernel root (this repository)
  src/crow_os/                 # ledger, capabilities, workflow, runtime,
                               # agents, federation, connectors, plugin
  docs/                        # vision, roadmap, governance, security gates
  tests/                       # kernel suite + security gate regressions
  probes/                      # adversarial probe harnesses
  scripts/                     # local verification (Actions-free)
  packages/                    # extracted libraries, one source of truth
    avrone/                    #   runtime (canonical trunk, post-W0)
    cyclekernal/               #   fusion math
    great_scale/               #   residual coordinate math + parity tests
    capability/                #   from capability-authority
    fusion_bridge/             #   GreatScale-Avrone-Grok-Fusion-Bridge
    attested_history/          #   receipt bus (kernel-side)
    simulation_governance/     #   GAP-018 contracts
  plugins/
    control12/                 #   lattice workers (host/K8s scope decided
                               #   by GAP-008)
  apps/
    crow_os_workspace/         #   transplanted 38-file Avrone workspace
    control_plane_ui/          #   living-intermediate-control-plane merge
    cyclekernel_showcase/
  infra/
    swarm_vm_runner/
  tools/
    github_api_automation/

satellites (stay separate repos, bound by the same contracts):
  product surfaces with independent deploy/compliance boundaries
```

## Rules of the structure

- packages/ are extracted with their tests and a declared public API;
  no package imports another package's internals.
- apps/ are users of packages/, never holders of shared logic.
- plugins/ deploy with their own lifecycle and speak to the kernel
  only through the capability broker.
- Archived repos point here with a redirect README and stay read-only.
