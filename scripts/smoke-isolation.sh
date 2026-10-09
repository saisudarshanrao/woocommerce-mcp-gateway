#!/usr/bin/env bash
# Checks who can reach what around a store, by actually trying each connection.
#
#   blocked   a pod in an unrelated namespace -> WordPress, MariaDB, the MCP server
#   blocked   a pod in the gateway's namespace without the gateway's labels -> the MCP server
#   allowed   a pod with the gateway's namespace and labels -> the MCP server
#   allowed   the MCP server -> its own WordPress
#   blocked   the MCP server -> MariaDB, the Kubernetes API, the internet
#   blocked   MariaDB -> anything
#
# Probe pods are created for the test and removed afterwards.
set -euo pipefail

CTX="${KUBE_CONTEXT:-k3d-urumi}"
STORE_NS="${STORE_NAMESPACE:-store-main}"
RELEASE="${STORE_RELEASE:-store}"
GATEWAY_NS="${GATEWAY_NAMESPACE:-gateway}"
OUTSIDE_NS="isolation-probe"

k() { kubectl --context "$CTX" "$@"; }
step() { printf '\n== %s\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*" >&2; exit 1; }

IMAGE="$(k -n "$STORE_NS" get deploy "${RELEASE}-woo-mcp" -o jsonpath='{.spec.template.spec.containers[0].image}')"
WORDPRESS="${RELEASE}-wordpress.${STORE_NS}.svc.cluster.local"
MARIADB="${RELEASE}-mariadb.${STORE_NS}.svc.cluster.local"
WOO_MCP="${RELEASE}-woo-mcp.${STORE_NS}.svc.cluster.local"

created_gateway_ns=""
cleanup() {
  k delete namespace "$OUTSIDE_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [ -n "$created_gateway_ns" ]; then
    k delete namespace "$GATEWAY_NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# probe_pod <namespace> <name> [labels]: a pod that just sleeps, to connect from.
probe_pod() {
  k -n "$1" run "$2" --image="$IMAGE" --restart=Never ${3:+--labels="$3"} \
    --overrides='{"spec":{"terminationGracePeriodSeconds":1}}' --command -- sleep 600 >/dev/null
  k -n "$1" wait "pod/$2" --for=condition=Ready --timeout=90s >/dev/null
}

# connects <namespace> <pod-or-deploy> <host> <port>: succeeds if a TCP connection opens.
connects() {
  k -n "$1" exec "$2" -- python -c '
import socket, sys
socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4).close()' "$3" "$4" >/dev/null 2>&1
}

# A rule that allows a pod learns its address a moment after the pod starts, so
# an allowed connection is retried for a few seconds before it counts as failed.
must_connect() {
  local label="$1"; shift
  for _ in $(seq 1 15); do
    if connects "$@"; then pass "allowed  $label"; return; fi
    sleep 1
  done
  fail "$label should be allowed but is not"
}
must_not_connect() {
  local label="$1"; shift
  if connects "$@"; then fail "$label should be blocked but is open"; fi
  pass "blocked  $label"
}

step "a pod in an unrelated namespace"
k create namespace "$OUTSIDE_NS" >/dev/null
probe_pod "$OUTSIDE_NS" outsider
must_connect     "outsider -> Kubernetes API (control: the probe itself works)" "$OUTSIDE_NS" outsider kubernetes.default.svc.cluster.local 443
must_not_connect "outsider -> WordPress"       "$OUTSIDE_NS" outsider "$WORDPRESS" 80
must_not_connect "outsider -> MariaDB"         "$OUTSIDE_NS" outsider "$MARIADB" 3306
must_not_connect "outsider -> the MCP server"  "$OUTSIDE_NS" outsider "$WOO_MCP" 80

step "pods in the gateway's namespace"
if k get namespace "$GATEWAY_NS" >/dev/null 2>&1; then
  # A probe carrying the gateway's labels would be picked up by the gateway's
  # own Service, so this part only runs before the gateway is installed.
  echo "  skip  namespace '$GATEWAY_NS' already exists; not adding look-alike pods to it"
else
  k create namespace "$GATEWAY_NS" >/dev/null
  created_gateway_ns=1
  probe_pod "$GATEWAY_NS" lookalike "app.kubernetes.io/name=gateway,app.kubernetes.io/component=api"
  probe_pod "$GATEWAY_NS" bystander "app.kubernetes.io/name=something-else"
  must_connect     "gateway-labelled pod -> the MCP server"            "$GATEWAY_NS" lookalike "$WOO_MCP" 80
  must_connect     "other pod -> Kubernetes API (control)"             "$GATEWAY_NS" bystander kubernetes.default.svc.cluster.local 443
  must_not_connect "same namespace, other labels -> the MCP server"    "$GATEWAY_NS" bystander "$WOO_MCP" 80
  must_not_connect "gateway-labelled pod -> WordPress (not its business)" "$GATEWAY_NS" lookalike "$WORDPRESS" 80
  must_not_connect "gateway-labelled pod -> MariaDB"                   "$GATEWAY_NS" lookalike "$MARIADB" 3306
  status="$(k -n "$GATEWAY_NS" exec lookalike -- python -c '
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen(urllib.request.Request(sys.argv[1], data=b"{}", headers={"Content-Type": "application/json"}), timeout=5)
except urllib.error.HTTPError as error:
    print(error.code)' "http://${WOO_MCP}/mcp")"
  [ "$status" = "401" ] || fail "reaching the MCP server without its token returned HTTP ${status:-nothing}, expected 401"
  pass "reaching it is not enough: without the bearer token it answers HTTP 401"
fi

step "what the MCP server itself can reach"
must_connect     "MCP server -> its WordPress"      "$STORE_NS" "deploy/${RELEASE}-woo-mcp" "${RELEASE}-wordpress" 80
must_not_connect "MCP server -> MariaDB"            "$STORE_NS" "deploy/${RELEASE}-woo-mcp" "${RELEASE}-mariadb" 3306
must_not_connect "MCP server -> Kubernetes API"     "$STORE_NS" "deploy/${RELEASE}-woo-mcp" 10.43.0.1 443
must_not_connect "MCP server -> the internet"       "$STORE_NS" "deploy/${RELEASE}-woo-mcp" 1.1.1.1 443

step "what MariaDB can reach"
db_connects() { k -n "$STORE_NS" exec "${RELEASE}-mariadb-0" -- bash -c "timeout 4 bash -c '</dev/tcp/$1/$2'" >/dev/null 2>&1; }
db_connects 127.0.0.1 3306 || fail "the probe from the MariaDB pod does not work, so its results mean nothing"
pass "allowed  MariaDB -> itself (control: the probe itself works)"
for target in "10.43.0.1 443 Kubernetes-API" "1.1.1.1 443 the-internet"; do
  set -- $target
  if db_connects "$1" "$2"; then fail "MariaDB -> ${3//-/ } should be blocked but is open"; fi
  pass "blocked  MariaDB -> ${3//-/ }"
done

printf '\nIsolation checks passed.\n'
