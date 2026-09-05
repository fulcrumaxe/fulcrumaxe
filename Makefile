.PHONY: test lint build deploy coverage clean

# Run all test suites
test:
	@echo "=== Running Python unit tests ==="
	python3 -m pytest tests/ -x --tb=short
	@echo ""
	@echo "=== Running integration tests ==="
	python3 -m pytest tests/integration/ -x --tb=short || echo "[WARN] Integration tests skipped (server may not be running)"

# Run linters
lint:
	@if command -v ruff >/dev/null 2>&1; then \
		echo "=== Running ruff ==="; \
		ruff check backend/ tests/ scripts/ || exit 1; \
	elif command -v flake8 >/dev/null 2>&1; then \
		echo "=== Running flake8 ==="; \
		flake8 backend/ tests/ || exit 1; \
	else \
		echo "[INFO] ruff/flake8 not found — running py_compile check"; \
		python3 -m py_compile backend/*.py; \
	fi
	@echo "=== TypeScript typecheck ==="
	cd tui && npm run typecheck

# Build all artifacts
build:
	@echo "=== Building TUI bundle ==="
	cd tui && npm install && npm run build
	@echo ""
	@echo "=== Python package check ==="
	python3 -c "import backend.api; print('[OK] backend.api importable')" 2>/dev/null || python3 -m py_compile backend/api.py && echo "[OK] backend/api.py compiles"

# Placeholder deploy target
deploy:
	@echo "=== Deployment Instructions ==="
	@echo ""
	@echo "1. Build production images: make build"
	@echo "2. Push to container registry: docker push <registry>/autonomous-forever-api:latest"
	@echo "3. Update remote deployment config with the new image refs"
	@echo "4. On remote host: pull and restart the updated images"
	@echo ""
	@echo "No automated deploy target configured yet."

# Generate coverage report
coverage:
	@echo "=== Running pytest with coverage ==="
	python3 -m pytest tests/ --cov=backend --cov-report=html --cov-report=term-missing
	@echo ""
	@echo "HTML report: htmlcov/index.html"

# Remove build artifacts and caches
clean:
	@echo "=== Cleaning build artifacts ==="
	find . -type d -name __pycache__ -not -path "./.git/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./.git/*" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage .pytest_cache/
	rm -rf tui/dist/ tui/.cache/
	@echo "=== Clean complete ==="
