#!/usr/bin/env bash
# Creates the local k3d cluster, or starts it if it already exists, then waits
# until Traefik is ready so Ingress objects applied right afterwards are served.
set -euo pipefail

CLUSTER="urumi" # must match metadata.name in deploy/local/k3d.yaml
CTX="k3d-${CLUSTER}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

for tool in docker k3d kubectl helm; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing required tool: ${tool} (see README, Prerequisites)" >&2
    exit 1
  }
done
docker info >/dev/null 2>&1 || {
  echo "Docker is not running. Start it first (with Colima: 'colima start')." >&2
  exit 1
}

if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  echo "cluster '${CLUSTER}' already exists, starting it"
  k3d cluster start "$CLUSTER"
else
  k3d cluster create --config "${ROOT}/deploy/local/k3d.yaml"
fi

# k3s installs Traefik from a Helm job shortly after the API server comes up,
# so the Deployment may not exist yet on a fresh cluster.
echo "waiting for Traefik"
for _ in $(seq 1 60); do
  kubectl --context "$CTX" -n kube-system get deploy traefik >/dev/null 2>&1 && break
  sleep 2
done
kubectl --context "$CTX" -n kube-system rollout status deploy/traefik --timeout=180s
kubectl --context "$CTX" get nodes
