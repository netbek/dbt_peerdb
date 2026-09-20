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

define fetch-or-sync
@if ls -d "$1/$2-$3.dist-info" >/dev/null 2>&1; then \
	echo "$(YELLOW)$1 already at $2==$3, skipping...$(RESET)"; \
else \
	echo "$(YELLOW)Fetching $2==$3 into $1...$(RESET)"; \
	rm -rf "$1"; \
	mise exec --fresh-env uv -- pip install --target "$1" --no-deps --quiet "$2==$3"; \
fi
endef

install:
	$(call fetch-or-sync,vendor/dbt,dbt-core,1.11.14)
	$(call fetch-or-sync,vendor/dbt-adapters,dbt-adapters,1.22.10)
	$(call fetch-or-sync,vendor/dbt-clickhouse,dbt-clickhouse,1.10.2)
