#!/usr/bin/env bash
# Smoke-tests the four cluster capabilities every chart in this repo relies on.
# Creates two throwaway namespaces and removes them on exit.
#
#   registry       an image pushed from the host is pullable from inside the cluster
#   storage        the default StorageClass provisions a PVC whose data outlives the pod
#   ingress        Traefik routes http://smoke.localhost to a Service
#   networkpolicy  policies are enforced: default-deny blocks, a targeted allow permits
set -euo pipefail

CTX="${KUBE_CONTEXT:-k3d-urumi}"
REGISTRY="${REGISTRY:-localhost:5001}"
HOST="smoke.localhost"
NS="smoke-test"
NS_CLIENT="smoke-client"
IMAGE="${REGISTRY}/smoke:$(date +%s)"
URL="http://web.${NS}.svc.cluster.local/"

k() { kubectl --context "$CTX" "$@"; }
step() { printf '\n== %s\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*" >&2; exit 1; }

cleanup() {
  k delete namespace "$NS" "$NS_CLIENT" --ignore-not-found --timeout=90s >/dev/null 2>&1 || true
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# retry <attempts> <delay-seconds> <command...>: succeed as soon as the command does.
retry() {
  local attempts="$1" delay="$2" i=0
  shift 2
  while [ "$i" -lt "$attempts" ]; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    i=$((i + 1))
    sleep "$delay"
  done
  return 1
}

# Fetch the web Service from a client pod in the other namespace.
reaches() { k -n "$NS_CLIENT" exec "$1" -- timeout 5 wget -q -T 3 -O- "$URL" 2>/dev/null | grep -q smoke-ok; }
blocked() { ! reaches "$1"; }
ingress_ok() { curl -fsS --max-time 3 "http://${HOST}/" | grep -q smoke-ok; }
ingress_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${HOST}/" || true; }

step "registry: build on the host, push, pull from inside the cluster"
docker build -q -t "$IMAGE" - >/dev/null <<'EOF'
FROM busybox:1.37
RUN mkdir /www && echo smoke-ok > /www/index.html
USER 65534
CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]
EOF
docker push -q "$IMAGE" >/dev/null
pass "pushed ${IMAGE}"

k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata: {name: ${NS}}
---
apiVersion: v1
kind: Namespace
metadata: {name: ${NS_CLIENT}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: data, namespace: ${NS}}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 64Mi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: ${NS}}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      terminationGracePeriodSeconds: 1
      containers:
        - name: web
          image: ${IMAGE}
          ports: [{containerPort: 8080}]
          volumeMounts: [{name: data, mountPath: /data}]
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: data}
---
apiVersion: v1
kind: Service
metadata: {name: web, namespace: ${NS}}
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: 8080}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: web, namespace: ${NS}}
spec:
  ingressClassName: traefik
  rules:
    - host: ${HOST}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: {service: {name: web, port: {number: 80}}}
EOF
for role in allowed denied; do
  k -n "$NS_CLIENT" run "client-${role}" --image="$IMAGE" --labels="role=${role}" \
    --restart=Never --overrides='{"spec":{"terminationGracePeriodSeconds":1}}' \
    --command -- sleep 3600 >/dev/null
done
k -n "$NS" rollout status deploy/web --timeout=120s >/dev/null
k -n "$NS_CLIENT" wait pod --all --for=condition=Ready --timeout=120s >/dev/null
pass "cluster pulled the image and started 3 pods from it"

step "storage: PVC is provisioned and data survives pod replacement"
TOKEN="token-${RANDOM}${RANDOM}"
k -n "$NS" exec deploy/web -- sh -c "echo ${TOKEN} > /data/token"
k -n "$NS" delete pod -l app=web --wait=true >/dev/null
got=""
for _ in $(seq 1 30); do
  got="$(k -n "$NS" exec deploy/web -- cat /data/token 2>/dev/null || true)"
  [ "$got" = "$TOKEN" ] && break
  sleep 2
done
[ "$got" = "$TOKEN" ] || fail "token written before the pod was deleted is gone (got '${got}')"
pass "PVC $(k -n "$NS" get pvc data -o jsonpath='{.status.phase} via StorageClass {.spec.storageClassName}'); token survived a new pod"

step "ingress: http://${HOST} -> Traefik -> Service"
retry 30 2 ingress_ok || fail "no response through the ingress (HTTP $(ingress_code))"
pass "HTTP $(ingress_code) from http://${HOST}/"

step "networkpolicy: policies are enforced"
retry 10 1 reaches client-denied || fail "baseline broken: cross-namespace traffic should work before any policy"
pass "baseline: no policy, cross-namespace traffic flows"

k apply -f - >/dev/null <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny-ingress, namespace: ${NS}}
spec:
  podSelector: {}
  policyTypes: [Ingress]
EOF
retry 15 1 blocked client-denied || fail "default-deny is NOT enforced: unlabelled client still gets through"
retry 15 1 blocked client-allowed || fail "default-deny is NOT enforced: labelled client still gets through"
code="$(ingress_code)"
[ "$code" != "200" ] || fail "default-deny is NOT enforced for Traefik"
pass "default-deny: both clients blocked, and Traefik too (HTTP ${code})"

k apply -f - >/dev/null <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: allow-labelled-client-and-traefik, namespace: ${NS}}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ${NS_CLIENT}}}
          podSelector: {matchLabels: {role: allowed}}
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: kube-system}}
          podSelector: {matchLabels: {app.kubernetes.io/name: traefik}}
      ports:
        - {protocol: TCP, port: 8080}
EOF
retry 15 1 reaches client-allowed || fail "allow rule not honoured: labelled client is still blocked"
blocked client-denied || fail "allow rule too wide: unlabelled client got through"
retry 15 1 ingress_ok || fail "allow rule not honoured: Traefik is still blocked (HTTP $(ingress_code))"
pass "targeted allow: labelled client and Traefik pass, unlabelled client still blocked"

printf '\nAll cluster smoke tests passed.\n'
