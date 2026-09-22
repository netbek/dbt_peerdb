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
	@echo "$(YELLOW)Installing...$(RESET)"
	@./scripts/install-node.sh
	@./scripts/install-python.sh
	@./scripts/install-precommit.sh
	@./scripts/install-skills.sh
	@./scripts/install-dbt.sh
	@./scripts/install-vendor.sh

deps-scan:
	@echo "$(YELLOW)Scanning root lockfiles for vulnerabilities...$(RESET)"
	trivy fs pnpm-lock.yaml --table-mode detailed
	trivy fs uv.lock --table-mode detailed

repo-scan:
	@echo "$(YELLOW)Scanning entire repository for vulnerabilities...$(RESET)"
	trivy fs .

node-install:
	@echo "$(YELLOW)Installing Node dependencies...$(RESET)"
	@./scripts/install-node.sh

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

python-install:
	@echo "$(YELLOW)Installing Python dependencies...$(RESET)"
	@./scripts/install-python.sh

python-outdated:
	@echo "$(YELLOW)Listing outdated Python dependencies...$(RESET)"
	uv tree --outdated --depth 1

python-upgrade: PACKAGE := $(word 2,$(MAKECMDGOALS))
python-upgrade:
	@if [ -z "$(PACKAGE)" ]; then \
		echo "$(YELLOW)Upgrading all Python dependencies...$(RESET)"; \
		uv lock --upgrade; \
	else \
		echo "$(YELLOW)Upgrading '$(PACKAGE)'...$(RESET)"; \
		uv lock --upgrade-package $(PACKAGE); \
	fi

# Prevent make from treating arguments to python-upgrade as targets
ifeq (python-upgrade,$(firstword $(MAKECMDGOALS)))
%:
	@:
endif

python-why: PACKAGE := $(word 2,$(MAKECMDGOALS))
python-why:
	@if [ -z "$(PACKAGE)" ]; then \
		echo "$(RED)Error: Package name is required.$(RESET)"; \
		echo "Usage: make python-why <package>"; \
		exit 1; \
	fi
	@echo "$(YELLOW)Listing Python dependencies of '$(PACKAGE)'...$(RESET)"
	uv tree --invert --package $(PACKAGE)

# Prevent make from treating arguments to python-why as targets
ifeq (python-why,$(firstword $(MAKECMDGOALS)))
%:
	@:
endif

uv-sync:
	uv sync --all-extras --all-groups

skills-install:
	@echo "$(YELLOW)Installing agent skills...$(RESET)"
	pnpm exec skills-manager install --force

skills-uninstall:
	@echo "$(YELLOW)Uninstalling agent skills...$(RESET)"
	pnpm exec skills-manager uninstall

# ==============================================================================
# DEVELOPMENT
# ==============================================================================

autoflake:
	@echo "Removing unused imports..."
	pre-commit run autoflake --hook-stage manual --files $(filter-out $@,$(MAKECMDGOALS))

format:
	@echo "Formatting code..."
	pre-commit run yamlfmt --all-files
	pre-commit run pyupgrade --all-files
	pre-commit run isort --all-files
	pre-commit run ruff-format --all-files

lint:
	@echo "Linting code..."
	pre-commit run ruff-check --hook-stage manual --all-files

# ==============================================================================
# CLICKHOUSE
# ==============================================================================

clickhouse-start:
	clickhousectl local server start --version 26.3.33.24 --http-port 18123 --tcp-port 19000

clickhouse-stop:
	clickhousectl local server stop

clickhouse-remove:
	clickhousectl local server remove

# ==============================================================================
# RELEASE
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
