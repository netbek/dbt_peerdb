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
