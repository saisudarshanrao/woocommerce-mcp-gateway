# Local workflow. Every target names its kube context explicitly instead of
# trusting whichever one is current, so local values can never land on prod.
CLUSTER      := urumi
KUBE_CONTEXT ?= k3d-$(CLUSTER)
REGISTRY     ?= localhost:5001

# Which values file to layer on each chart: values-$(ENV).yaml.
ENV ?= local

KUBECTL := kubectl --context $(KUBE_CONTEXT)
HELM    := helm --kube-context $(KUBE_CONTEXT)

STORE_NAMESPACE ?= store-main
STORE_RELEASE   ?= store
WORDPRESS_IMAGE := $(REGISTRY)/store-wordpress
WOO_MCP_IMAGE   := $(REGISTRY)/woo-mcp
# An image's tag is a hash of its source directory: see scripts/image-tag.sh.
WORDPRESS_TAG    = $(shell ./scripts/image-tag.sh images/wordpress)
WOO_MCP_TAG      = $(shell ./scripts/image-tag.sh services/woo-mcp)

MCP_NAMESPACE   ?= mcp-servers
DEMO_MCP_IMAGE  := $(REGISTRY)/demo-mcp
DEMO_MCP_TAG     = $(shell ./scripts/image-tag.sh services/demo-mcp)

GATEWAY_NAMESPACE ?= gateway
GATEWAY_RELEASE   ?= gateway
GATEWAY_IMAGE     := $(REGISTRY)/gateway
DASHBOARD_IMAGE   := $(REGISTRY)/gateway-dashboard
GATEWAY_TAG        = $(shell ./scripts/image-tag.sh services/gateway)
DASHBOARD_TAG      = $(shell ./scripts/image-tag.sh services/dashboard)

.DEFAULT_GOAL := help
.PHONY: help cluster-up cluster-stop cluster-down cluster-status cluster-smoke \
        store-image store-up store-down store-status store-logs store-password store-smoke \
        mcp-test mcp-smoke isolation-smoke \
        mcp-servers-up mcp-servers-down tasks-token \
        gateway-image gateway-up gateway-down gateway-status gateway-logs gateway-password gateway-test \
        llm-key up down

help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

cluster-up: ## Create the local k3d cluster, or start it if it already exists
	@./scripts/cluster-up.sh

cluster-stop: ## Stop the cluster and keep its data
	k3d cluster stop $(CLUSTER)

cluster-down: ## Delete the cluster, its registry and all volumes
	k3d cluster delete $(CLUSTER)

cluster-status: ## Show nodes, workloads, ingresses and volumes
	@$(KUBECTL) get nodes -o wide
	@echo
	@$(KUBECTL) get pods -A
	@echo
	@$(KUBECTL) get ingress,pvc -A

cluster-smoke: ## Verify registry, storage, ingress and NetworkPolicy enforcement
	@KUBE_CONTEXT=$(KUBE_CONTEXT) REGISTRY=$(REGISTRY) ./scripts/smoke-cluster.sh

store-image: ## Build the store's images (WordPress, MCP server) and push them to the registry
	@./scripts/build-image.sh images/wordpress $(WORDPRESS_IMAGE)
	@./scripts/build-image.sh services/woo-mcp $(WOO_MCP_IMAGE)

store-up: store-image ## Install or upgrade the store, then wait until it is ready
	$(HELM) upgrade --install $(STORE_RELEASE) charts/store \
		--namespace $(STORE_NAMESPACE) --create-namespace \
		--values charts/store/values-$(ENV).yaml \
		--set wordpress.image.tag=$(WORDPRESS_TAG) \
		--set wooMcp.image.tag=$(WOO_MCP_TAG) \
		--wait --timeout 10m

store-down: ## Remove the store and delete all of its data
	-$(HELM) uninstall $(STORE_RELEASE) --namespace $(STORE_NAMESPACE)
	$(KUBECTL) delete namespace $(STORE_NAMESPACE) --ignore-not-found

store-status: ## Show the store's pods, volumes, ingress and network policies
	@$(KUBECTL) -n $(STORE_NAMESPACE) get pods,pvc,ingress,networkpolicy

store-logs: ## Show the setup log of the current WordPress pod
	@$(KUBECTL) -n $(STORE_NAMESPACE) logs deploy/$(STORE_RELEASE)-wordpress -c setup

store-password: ## Print the WordPress admin password
	@$(KUBECTL) -n $(STORE_NAMESPACE) get secret $(STORE_RELEASE)-secrets \
		-o jsonpath='{.data.wp-admin-password}' | base64 -d; echo

store-smoke: ## Place a guest order through the storefront and check the admin sees it
	@python3 scripts/smoke_store.py --context $(KUBE_CONTEXT) \
		--namespace $(STORE_NAMESPACE) --release $(STORE_RELEASE)

mcp-test: ## Run the WooCommerce MCP server's unit tests
	cd services/woo-mcp && uv run pytest -q

mcp-smoke: ## Create a product, take an order and update it through the MCP tools
	@python3 scripts/smoke_mcp.py --context $(KUBE_CONTEXT) \
		--namespace $(STORE_NAMESPACE) --release $(STORE_RELEASE)

isolation-smoke: ## Try every connection around the store and check which are blocked
	@KUBE_CONTEXT=$(KUBE_CONTEXT) STORE_NAMESPACE=$(STORE_NAMESPACE) STORE_RELEASE=$(STORE_RELEASE) \
		./scripts/smoke-isolation.sh

mcp-servers-up: ## Deploy the two extra MCP servers (tasks and sandbox)
	@./scripts/build-image.sh services/demo-mcp $(DEMO_MCP_IMAGE)
	@for server in tasks sandbox; do \
		$(HELM) upgrade --install $$server charts/mcp-server \
			--namespace $(MCP_NAMESPACE) --create-namespace \
			--values charts/mcp-server/values-$$server.yaml \
			--values charts/mcp-server/values-$(ENV).yaml \
			--set image.tag=$(DEMO_MCP_TAG) \
			--wait --timeout 5m | grep -E "^(STATUS|REVISION)" | tr '\n' ' '; echo "($$server)"; \
	done

mcp-servers-down: ## Remove the two extra MCP servers
	$(KUBECTL) delete namespace $(MCP_NAMESPACE) --ignore-not-found

tasks-token: ## Print the credential the tasks server expects (header X-API-Key)
	@$(KUBECTL) -n $(MCP_NAMESPACE) get secret tasks-mcp-auth -o jsonpath='{.data.token}' | base64 -d; echo

gateway-image: ## Build the gateway's images (API, dashboard) and push them to the registry
	@./scripts/build-image.sh services/gateway $(GATEWAY_IMAGE)
	@./scripts/build-image.sh services/dashboard $(DASHBOARD_IMAGE)

gateway-up: gateway-image ## Install or upgrade the gateway, then wait until it is ready
	$(HELM) upgrade --install $(GATEWAY_RELEASE) charts/gateway \
		--namespace $(GATEWAY_NAMESPACE) --create-namespace \
		--values charts/gateway/values-$(ENV).yaml \
		--set api.image.tag=$(GATEWAY_TAG) \
		--set dashboard.image.tag=$(DASHBOARD_TAG) \
		--wait --timeout 10m

gateway-down: ## Remove the gateway and delete all of its data
	-$(HELM) uninstall $(GATEWAY_RELEASE) --namespace $(GATEWAY_NAMESPACE)
	$(KUBECTL) delete namespace $(GATEWAY_NAMESPACE) --ignore-not-found

gateway-status: ## Show the gateway's pods, volumes, ingress and network policies
	@$(KUBECTL) -n $(GATEWAY_NAMESPACE) get pods,pvc,ingress,networkpolicy

gateway-logs: ## Follow the gateway API's log
	@$(KUBECTL) -n $(GATEWAY_NAMESPACE) logs deploy/$(GATEWAY_RELEASE)-api --tail=100 -f

gateway-password: ## Print the gateway admin's password
	@$(KUBECTL) -n $(GATEWAY_NAMESPACE) get secret $(GATEWAY_RELEASE)-secrets \
		-o jsonpath='{.data.admin-password}' | base64 -d; echo

gateway-test: ## Run the gateway's unit tests (starts a throwaway Postgres in Docker)
	cd services/gateway && uv run pytest -q

llm-key: ## Store the chat model's API key (from the LLM_API_KEY environment variable) and restart the API
	@test -n "$$LLM_API_KEY" || { echo "Set LLM_API_KEY in the environment first: LLM_API_KEY=... make llm-key"; exit 1; }
	@$(KUBECTL) create namespace $(GATEWAY_NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f - >/dev/null
	@$(KUBECTL) -n $(GATEWAY_NAMESPACE) create secret generic gateway-llm \
		--from-literal=api-key="$$LLM_API_KEY" --dry-run=client -o yaml | $(KUBECTL) apply -f - >/dev/null
	@$(KUBECTL) -n $(GATEWAY_NAMESPACE) rollout restart deploy/$(GATEWAY_RELEASE)-api >/dev/null 2>&1 || true
	@echo "stored in Secret gateway-llm; the API is restarting to pick it up"

up: store-up mcp-servers-up gateway-up ## Deploy everything: the store, the extra MCP servers and the gateway

down: gateway-down mcp-servers-down store-down ## Remove everything and delete all data
