ifneq ($(shell which tput),)
	ifneq ($(TERM),)
		RED    := $(shell tput setaf 1)
		GREEN  := $(shell tput setaf 2)
		YELLOW := $(shell tput setaf 3)
		CYAN   := $(shell tput setaf 6)
		RESET  := $(shell tput sgr0)
	endif
endif

# ==============================================================================
# DEPENDENCY MANAGEMENT
# ==============================================================================

install:
	@./scripts/install.sh

deps-scan:
	@echo "$(YELLOW)Scanning root lockfiles for vulnerabilities...$(RESET)"
	trivy fs pnpm-lock.yaml --table-mode detailed

repo-scan:
	@echo "$(YELLOW)Scanning entire repository for vulnerabilities...$(RESET)"
	trivy fs .

node-outdated:
	@echo "$(YELLOW)Listing outdated Node dependencies...$(RESET)"
	@pnpm outdated || true

node-upgrade: PACKAGE := $(word 2,$(MAKECMDGOALS))
node-upgrade:
	@if [ -z "$(PACKAGE)" ]; then \
		echo "$(YELLOW)Upgrading all Node dependencies...$(RESET)"; \
		pnpm exec ncu --packageManager pnpm --install always; \
	else \
		echo "$(YELLOW)Upgrading '$(PACKAGE)'...$(RESET)"; \
		pnpm exec ncu $(PACKAGE) --packageManager pnpm --install always; \
	fi

# Prevent make from treating arguments to node-upgrade as targets
ifeq (node-upgrade,$(firstword $(MAKECMDGOALS)))
%:
	@:
endif

node-why: PACKAGE := $(word 2,$(MAKECMDGOALS))
node-why:
	@if [ -z "$(PACKAGE)" ]; then \
		echo "$(RED)Error: Package name is required.$(RESET)"; \
		echo "Usage: make node-why <package>"; \
		exit 1; \
	fi
	@echo "$(YELLOW)Listing Node dependencies of '$(PACKAGE)'...$(RESET)"
	pnpm why $(PACKAGE)

# Prevent make from treating arguments to node-why as targets
ifeq (node-why,$(firstword $(MAKECMDGOALS)))
%:
	@:
endif

# ==============================================================================
# PUBLISH
# ==============================================================================

bump-version:
	@./scripts/bump-version.sh $(word 2,$(MAKECMDGOALS))

# Prevent make from treating the bump type as a target (e.g. `make bump-version patch`)
ifeq (bump-version,$(firstword $(MAKECMDGOALS)))
%:
	@:
endif

create-release:
	@./scripts/create-release.sh
