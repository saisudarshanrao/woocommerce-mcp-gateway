#!/usr/bin/env python3
"""End-to-end check of MCP servers the platform hosts itself.

    an admin adds a server by container image instead of by URL
    it is deployed into a namespace of its own, with its own Secret, resource
      limits and NetworkPolicy, as a non-root container with no Kubernetes access
    its tools appear behind the gateway, which authenticates with a credential
      it made up itself and that no response ever contains
    nothing but the gateway can reach it, and it can reach nothing
    changing what it runs redeploys it
    an image that cannot start says why, and can be removed meanwhile
    removing the server takes its tools away at once, then its namespace

The image used is the demo MCP server already in the registry. Everything an
admin would do goes through the Ingress; the cluster is inspected with kubectl.
Standard library only.
"""

import argparse
import base64
import json
import subprocess
import sys
import time

from smoke_gateway import Dashboard, kubectl, secret, tool_names, wait_for
from smoke_mcp import McpClient
from smoke_store import Failure, ok
from smoke_stores import as_provisioner, can_connect

SLUG = "hosted-smoke"
PROBE_NS = "hosted-probe"


def server_named(admin, slug):
    return next((server for server in admin.call("GET", "/api/servers") if server["slug"] == slug), None)


def wait_until(admin, slug, done, seconds=180):
    """Waits for a hosted server to reach a state, printing each step of its deployment on the way."""
    seen, deadline = None, time.time() + seconds
    while time.time() < deadline:
        server = server_named(admin, slug)
        line = "gone" if server is None else f"{server['hosted']['status']}: {server['hosted']['detail'] or server['status']}"
        if line != seen:
            seen = line
            print(f"        {line}"[:150])
        if server and server["hosted"]["status"] == "failed" and not done(server):
            raise Failure(f"the deployment failed: {server['hosted']['detail']}")
        if done(server):
            return server
        time.sleep(1)
    raise Failure(f"{slug} did not get there within {seconds} seconds; last seen: {seen}")


def up(server):
    return server and server["hosted"]["status"] == "running" and server["status"] == "connected"


def run(args):
    admin = Dashboard(args.url)
    print(f"\n== the admin signs in at {args.url}")
    admin.call("POST", "/api/auth/login", {
        "email": args.admin_email,
        "password": secret(args, args.namespace, f"{args.release}-secrets", "admin-password"),
    })
    if not admin.call("GET", "/api/config")["hosted_enabled"]:
        raise Failure("this gateway cannot host servers (provisioner.enabled in the gateway chart)")
    for leftover in (SLUG, "hosted-bad"):
        if server_named(admin, leftover):
            raise Failure(f"'{leftover}' is left over from an earlier run. Remove it on the Servers page first.")
    image = args.image or kubectl(args, "-n", "mcp-servers", "get", "deploy", "tasks-mcp", "-o",
                                  "jsonpath={.spec.template.spec.containers[0].image}")
    ok(f"servers can be hosted here; using the image {image}")

    member_id, added = None, []
    namespace = f"mcp-{SLUG}"
    try:
        print("\n== the admin adds a server by image, with no URL and no credential")
        created = admin.call("POST", "/api/servers", {
            "slug": SLUG, "name": "Hosted tasks", "hosted": {"image": image, "args": ["tasks"]},
        })
        added.append(created["id"])
        ok(f"accepted: it will be at {created['url']}, with a credential the gateway made up")
        wait_until(admin, SLUG, up)
        server = server_named(admin, SLUG)
        ok(f"deployed and connected: {len(server['tools'])} tools, such as {server['tools'][0]['name']}")

        print("\n== what it got in the cluster")
        labels = json.loads(kubectl(args, "get", "namespace", namespace, "-o", "json"))["metadata"]["labels"]
        if labels.get("mcp-gateway/hosted") != SLUG or labels.get("pod-security.kubernetes.io/enforce") != "restricted":
            raise Failure(f"namespace {namespace} has the wrong labels: {labels}")
        ok(f"its own namespace, {namespace}, held to the 'restricted' pod security standard")
        pod = json.loads(kubectl(args, "-n", namespace, "get", "pods", "-o", "json"))["items"][0]["spec"]
        container = pod["containers"][0]
        limits, context = container["resources"]["limits"], container["securityContext"]
        if pod.get("automountServiceAccountToken") is not False or not context["readOnlyRootFilesystem"] \
                or context["allowPrivilegeEscalation"] or not pod["securityContext"]["runAsNonRoot"]:
            raise Failure("the hosted pod is not locked down as expected")
        ok(f"limits of {limits['cpu']} CPU and {limits['memory']} memory; non-root, read-only filesystem, "
           "no privilege escalation, no Kubernetes credentials")
        policies = kubectl(args, "-n", namespace, "get", "networkpolicy", "-o", "name").split()
        token = base64.b64decode(kubectl(
            args, "-n", namespace, "get", "secret", "server-mcp-auth", "-o", "jsonpath={.data.token}")).decode()
        if not policies or len(token) < 40:
            raise Failure("the hosted server has no NetworkPolicy or no credential in its Secret")
        if token in json.dumps(admin.call("GET", "/api/servers")):
            raise Failure("the API returned the hosted server's credential")
        ok(f"its own Secret and NetworkPolicy ({policies[0].split('/')[1]}); the credential in the Secret is in no API response")

        print("\n== its tools, through the gateway, for a member")
        email = f"hosted-{int(time.time())}@example.com"
        invite = admin.call("POST", "/api/invites", {"email": email, "role": "member"})
        member = Dashboard(args.url)
        member_id = member.call("POST", f"/api/invites/{invite['link'].rsplit('/', 1)[1]}/accept",
                                {"name": "Hosted Member", "password": "a-password-only-for-this-run"})["id"]
        mcp = McpClient(f"{args.url}/mcp", member.call("POST", "/api/keys", {"name": "hosted smoke"})["key"])
        if f"{SLUG}__add_task" not in tool_names(mcp):
            raise Failure("the hosted server's tools are not in the gateway's list")
        task = mcp.call_tool(f"{SLUG}__add_task", {"title": "Hosted by the platform"})
        ok(f"{SLUG}__add_task worked: task #{task['id']} '{task['title']}'")
        if "hosted" in (seen := server_named(member, SLUG)) and "image" in seen["hosted"]:
            raise Failure("a member can see what image a hosted server runs")
        ok("the member uses it without seeing its image, address or credential")

        print("\n== who can reach it, and what it can reach")
        host = f"server-mcp.{namespace}.svc.cluster.local"
        without = ("import sys, urllib.request, urllib.error\n"
                   "try:\n"
                   "    urllib.request.urlopen(urllib.request.Request(sys.argv[1], data=b'{}', "
                   "headers={'Content-Type': 'application/json'}), timeout=5)\n"
                   "except urllib.error.HTTPError as error:\n"
                   "    print(error.code)")
        status = kubectl(args, "-n", args.namespace, "exec", f"deploy/{args.release}-api", "--",
                         "python", "-c", without, f"http://{host}/mcp").strip()
        if status != "401":
            raise Failure(f"without the credential the hosted server answered HTTP {status or 'nothing'}, expected 401")
        ok("allowed  the gateway's API pod -> the hosted server; without the credential it answers HTTP 401")
        kubectl(args, "create", "namespace", PROBE_NS)
        kubectl(args, "-n", PROBE_NS, "run", "outsider", f"--image={image}", "--restart=Never",
                "--overrides={\"spec\":{\"terminationGracePeriodSeconds\":1}}", "--command", "--", "sleep", "300")
        kubectl(args, "-n", PROBE_NS, "wait", "pod/outsider", "--for=condition=Ready", "--timeout=90s")
        wait_for("the probe pod can connect to something",
                 lambda: can_connect(args, PROBE_NS, "outsider", "kubernetes.default.svc.cluster.local", 443))
        if can_connect(args, PROBE_NS, "outsider", host, 80):
            raise Failure("a pod in an unrelated namespace can connect to the hosted server")
        ok("blocked  a pod in an unrelated namespace -> the hosted server (control: that pod reaches the Kubernetes API)")
        for label, target, port in (("the internet", "1.1.1.1", 443), ("the Kubernetes API", "10.43.0.1", 443),
                                    ("the first store's WordPress", "store-wordpress.store-main.svc.cluster.local", 80)):
            if can_connect(args, namespace, "deploy/server-mcp", target, port):
                raise Failure(f"the hosted server can open a connection to {label}")
            ok(f"blocked  the hosted server -> {label}")

        print("\n== the admin changes what it runs")
        changed = admin.call("PATCH", f"/api/servers/{created['id']}", {"hosted": {"image": image, "args": ["sandbox"]}})
        if changed["hosted"]["status"] != "deploying":
            raise Failure(f"changing the arguments did not start a deployment: {changed['hosted']}")
        wait_until(admin, SLUG, lambda s: up(s) and any(tool["name"] == f"{SLUG}__echo" for tool in s["tools"]))
        names = tool_names(mcp)
        if f"{SLUG}__add_task" in names or f"{SLUG}__echo" not in names:
            raise Failure("after the change the gateway still lists the old tools")
        ok(f"redeployed with other arguments: its tools are now {SLUG}__echo and friends, and the task tools are gone")

        print("\n== an image that cannot start")
        bad = admin.call("POST", "/api/servers", {
            "slug": "hosted-bad", "name": "Broken", "hosted": {"image": "localhost:5001/there-is-no-such-image:0"},
        })
        added.append(bad["id"])
        stuck = wait_until(admin, "hosted-bad", lambda s: s and "Image" in (s["hosted"]["detail"] or ""), seconds=120)
        ok(f"it stays 'deploying' and says why: {stuck['hosted']['detail']}")
        admin.call("DELETE", f"/api/servers/{bad['id']}")
        wait_until(admin, "hosted-bad", lambda s: s is None, seconds=120)
        if subprocess.run(["kubectl", "--context", args.context, "get", "namespace", "mcp-hosted-bad"], capture_output=True).returncode == 0:
            raise Failure("namespace mcp-hosted-bad still exists")
        ok("removed while still deploying: the row and its namespace are gone")

        print("\n== what the provisioner may do with mcp- namespaces")
        for expected, what, command in (
            (False, "create a namespace mcp-rogue without its label", ("create", "namespace", "mcp-rogue", "--dry-run=server")),
            (False, "delete mcp-servers, which an operator installed", ("delete", "namespace", "mcp-servers", "--dry-run=server")),
            (False, "read Secrets in mcp-servers", ("-n", "mcp-servers", "get", "secrets")),
            (True, "read the pods of the server it deployed", ("-n", namespace, "get", "pods", "-o", "name")),
        ):
            allowed, said = as_provisioner(args, *command)
            if allowed != expected:
                raise Failure(f"the provisioner {'can' if allowed else 'cannot'} {what}: {said}")
            ok(f"{'allowed ' if allowed else 'refused '} {what}" + ("" if allowed else f": {said}"[:140]))

        print("\n== the admin removes the hosted server")
        admin.call("DELETE", f"/api/servers/{created['id']}")
        if any(name.startswith(f"{SLUG}__") for name in tool_names(mcp)):
            raise Failure("the removed server's tools are still listed")
        refused = mcp.call_tool(f"{SLUG}__echo", {"text": "anyone there?"}, expect_error=True)
        ok(f"its tools left the gateway at once; calling one: {refused}")
        wait_until(admin, SLUG, lambda s: s is None, seconds=120)
        if subprocess.run(["kubectl", "--context", args.context, "get", "namespace", namespace], capture_output=True).returncode == 0:
            raise Failure(f"namespace {namespace} still exists")
        ok(f"namespace {namespace} is gone, and with it the pod, the Secret and the NetworkPolicy")
    finally:
        subprocess.run(["kubectl", "--context", args.context, "delete", "namespace", PROBE_NS, "--ignore-not-found", "--wait=false"],
                       capture_output=True)
        if not args.keep:
            for server_id in added:
                admin.call("DELETE", f"/api/servers/{server_id}", expect=(204, 404, 409))
            if member_id:
                admin.call("DELETE", f"/api/members/{member_id}", expect=(204, 404))
            wait_for("this run's servers are gone",
                     lambda: not any(s["id"] in added for s in admin.call("GET", "/api/servers")), seconds=180)
            ok("the servers and the member this run created have been removed")

    print("\nHosted servers smoke test passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://gateway.localhost", help="public URL of the gateway")
    parser.add_argument("--context", default="k3d-urumi", help="kube context")
    parser.add_argument("--namespace", default="gateway")
    parser.add_argument("--release", default="gateway", help="Helm release name")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--image", default="", help="the MCP server image to host (default: the demo server's)")
    parser.add_argument("--keep", action="store_true", help="leave the servers and the member this run creates")
    run(parser.parse_args())


if __name__ == "__main__":
    try:
        main()
    except Failure as failure:
        print(f"  FAIL  {failure}", file=sys.stderr)
        sys.exit(1)
