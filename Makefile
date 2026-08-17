PYTHON ?= python3
PORT ?= 8080
MOCK_DIR ?= .gopro-mock
MOCK_URL = http://127.0.0.1:$(PORT)/videos/DCIM/100GOPRO/
ARGS ?=

.PHONY: server run clean help

server: ## Start the mock GoPro server (foreground; Ctrl-C to stop)
	$(PYTHON) mock_gopro_server.py --dir $(MOCK_DIR) --port $(PORT) $(ARGS)

run: ## Download from the mock server into $(MOCK_DIR)/lib (server must be running)
	$(PYTHON) download_gopro_tui.py --url $(MOCK_URL) --base-dir $(MOCK_DIR)/lib --yes

clean: ## Remove generated test files and the test download library
	rm -rf $(MOCK_DIR)

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'
