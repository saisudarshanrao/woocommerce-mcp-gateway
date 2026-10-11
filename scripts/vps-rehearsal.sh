#!/usr/bin/env bash
# Stands in for a rented server when there is none to hand: a fresh Ubuntu VM
# on this Mac, set up exactly as docs/DEPLOY.md sets up a VPS.
#
#   1. creates the VM with Lima and gives it an address this Mac can reach
#   2. installs k3s on it with the official installer, as on any server
#   3. saves a kubeconfig for it, without touching your own ~/.kube/config
#   4. turns on the HTTP-to-HTTPS redirect, installs cert-manager, and gives it
#      a certificate authority to issue from
#   5. writes this deployment's settings to deploy/sites/rehearsal
#
# It differs from a real VPS in two ways, both because the address is private:
# the hostnames are <name>.<address>.sslip.io, a public DNS service that answers
# with the address written in the name, and certificates come from a private
# authority made here instead of from Let's Encrypt, which can only vouch for
# names it can reach from the internet. Everything else is the same charts, the
# same production values and the same commands.
#
#   scripts/vps-rehearsal.sh          create (or start) the VM and prepare it
#   scripts/vps-rehearsal.sh down     delete the VM and what this script wrote
set -euo pipefail

VM="${VPS_VM:-vps}"
K3S_VERSION="v1.36.5+k3s1"         # the same Kubernetes as the local cluster
CERT_MANAGER_VERSION="v1.21.2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SITE_DIR="${ROOT}/deploy/sites/rehearsal"
KUBECONFIG_FILE="${HOME}/.kube/urumi-${VM}.yaml"

step() { printf '\n== %s\n' "$*"; }
in_vm() { limactl shell "$VM" "$@"; }

if [ "${1:-}" = "down" ]; then
  limactl delete --force "$VM" 2>/dev/null || true
  rm -rf "$SITE_DIR" "$KUBECONFIG_FILE"
  echo "The VM, its kubeconfig and deploy/sites/rehearsal are gone."
  exit 0
fi

step "a fresh Ubuntu VM called '${VM}'"
if ! limactl list --quiet 2>/dev/null | grep -qx "$VM"; then
  # vzNAT gives the VM an address of its own on a network shared with this Mac,
  # which is what makes it look like a server on the network rather than a
  # process behind forwarded ports. Nothing of the Mac's is mounted into it.
  limactl start --name="$VM" --vm-type=vz --cpus=4 --memory=5 --disk=20 --network=vzNAT \
    --mount-none --containerd=none --tty=false template://ubuntu-lts
elif [ "$(limactl list --format '{{.Status}}' "$VM")" != "Running" ]; then
  limactl start "$VM"
fi
ADDRESS="$(in_vm ip -4 -o addr show lima0 | awk '{print $4}' | cut -d/ -f1)"
[ -n "$ADDRESS" ] || { echo "the VM has no address on the shared network" >&2; exit 1; }
echo "its address: ${ADDRESS}"

step "k3s ${K3S_VERSION}, installed the way docs/DEPLOY.md says"
if ! in_vm sudo test -x /usr/local/bin/k3s; then
  # --node-ip and --tls-san: the address other machines know this server by,
  # so that the node and the API's certificate both use it.
  in_vm sh -c "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='${K3S_VERSION}' sh -s - --node-ip '${ADDRESS}' --tls-san '${ADDRESS}'" \
    | grep -E "Installing|Starting|systemd: Enabling" || true
fi
mkdir -p "$(dirname "$KUBECONFIG_FILE")"
# The server's own kubeconfig points at 127.0.0.1 and calls everything
# "default". Point it at the server and give it a name of its own.
in_vm sudo cat /etc/rancher/k3s/k3s.yaml | sed -e "s/127.0.0.1/${ADDRESS}/" -e "s/: default$/: ${VM}/" > "$KUBECONFIG_FILE"
chmod 600 "$KUBECONFIG_FILE"
export KUBECONFIG="$KUBECONFIG_FILE"
k() { kubectl --context "$VM" "$@"; }
for _ in $(seq 1 60); do k get nodes 2>/dev/null | grep -q " Ready " && break; sleep 2; done
k get nodes -o wide | awk '{print $1, $2, $5, $6}'
# k3s installs Traefik itself, a minute or so after the node is ready.
for _ in $(seq 1 90); do k -n kube-system get deploy traefik >/dev/null 2>&1 && break; sleep 2; done
k -n kube-system rollout status deploy/traefik --timeout=300s >/dev/null
echo "the ingress controller is up"

step "plain HTTP is redirected to HTTPS"
k apply -f "${ROOT}/deploy/sites/example/traefik-https-redirect.yaml" >/dev/null
# k3s re-renders Traefik with the new setting; wait for that to have happened.
for _ in $(seq 1 60); do
  k -n kube-system get deploy traefik -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null | grep -q "redirections" && break
  sleep 2
done
k -n kube-system rollout status deploy/traefik --timeout=300s >/dev/null
echo "Traefik now answers http:// with a redirect"

step "cert-manager ${CERT_MANAGER_VERSION} and an issuer"
k apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml" >/dev/null
for name in cert-manager cert-manager-webhook cert-manager-cainjector; do
  k -n cert-manager rollout status "deploy/${name}" --timeout=180s >/dev/null
done
# A real server would use Let's Encrypt (deploy/sites/example/cluster-issuer.yaml).
# Here cert-manager is given an authority of its own: a self-signed root, and an
# issuer that signs with it. The charts only name the issuer, so they cannot
# tell the difference.
issuer() {
  k apply -f - >/dev/null <<YAML
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: rehearsal-selfsigned
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: rehearsal-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: Rehearsal certificate authority
  secretName: rehearsal-ca
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: rehearsal-selfsigned
    kind: ClusterIssuer
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: rehearsal-ca
spec:
  ca:
    secretName: rehearsal-ca
YAML
}
# cert-manager's admission webhook takes a moment to start answering.
for _ in $(seq 1 30); do issuer 2>/dev/null && break; sleep 3; done
k -n cert-manager wait certificate/rehearsal-ca --for=condition=Ready --timeout=120s >/dev/null
echo "issuer 'rehearsal-ca' is ready"

step "this deployment's settings: deploy/sites/rehearsal"
DOMAIN="$(echo "$ADDRESS" | tr . -).sslip.io"
mkdir -p "$SITE_DIR"
cat > "${SITE_DIR}/store.yaml" <<YAML
# Written by scripts/vps-rehearsal.sh for the VM at ${ADDRESS}.
store:
  host: store.${DOMAIN}
  title: Urumi Store
ingress:
  annotations:
    cert-manager.io/cluster-issuer: rehearsal-ca
YAML
cat > "${SITE_DIR}/gateway.yaml" <<YAML
# Written by scripts/vps-rehearsal.sh for the VM at ${ADDRESS}.
host: gateway.${DOMAIN}
ingress:
  annotations:
    cert-manager.io/cluster-issuer: rehearsal-ca
provisioner:
  hostTemplate: "{slug}.stores.${DOMAIN}"
  storeValues:
    ingress:
      annotations:
        cert-manager.io/cluster-issuer: rehearsal-ca
YAML
# What a browser or a script has to trust to accept the certificates.
k -n cert-manager get secret rehearsal-ca -o jsonpath='{.data.tls\.crt}' | base64 -d > "${SITE_DIR}/ca.pem"
ls "$SITE_DIR" | tr '\n' ' '; echo

cat <<TEXT

The stand-in server is ready. To deploy to it and check it, in this shell:

  export KUBECONFIG="\$HOME/.kube/config:${KUBECONFIG_FILE}"
  export SSL_CERT_FILE="${SITE_DIR}/ca.pem"
  make up    ENV=prod SITE=rehearsal KUBE_CONTEXT=${VM} REGISTRY=images.local SHIP_TO=lima-${VM} SSH_OPTS="-F \$HOME/.lima/${VM}/ssh.config"
  make smoke KUBE_CONTEXT=${VM} STORE_URL=https://store.${DOMAIN} GATEWAY_URL=https://gateway.${DOMAIN}

  Storefront  https://store.${DOMAIN}
  Dashboard   https://gateway.${DOMAIN}
TEXT
