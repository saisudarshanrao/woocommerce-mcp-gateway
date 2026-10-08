# Local workflow. Every target names its kube context explicitly instead of
# trusting whichever one is current, so local values can never land on prod.
CLUSTER      := urumi
KUBE_CONTEXT ?= k3d-$(CLUSTER)
REGISTRY     ?= localhost:5001

KUBECTL := kubectl --context $(KUBE_CONTEXT)

.DEFAULT_GOAL := help
.PHONY: help cluster-up cluster-stop cluster-down cluster-status cluster-smoke

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
