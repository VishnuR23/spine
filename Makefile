.PHONY: help install demo up down logs clean test test-backend test-hook smoke dashboard-check verify openapi

help:
	@echo "Spine — common commands"
	@echo ""
	@echo "  make demo      Start everything and seed a working demo (start here)"
	@echo "  make up        Start the stack without seeding"
	@echo "  make down      Stop the stack, keep data"
	@echo "  make clean     Stop the stack and DESTROY data"
	@echo "  make logs      Tail API and worker logs"
	@echo ""
	@echo "  make install   Install the package and dev tools into the active venv"
	@echo "  make verify    Run every check (backend, hook, C++, smoke, dashboard)"
	@echo "  make test      Backend + hook tests"
	@echo "  make test-cpp  Build and test the C++ SDK"
	@echo "  make smoke     End-to-end smoke test (~30s, no API key needed)"
	@echo ""

install:
	pip install -e ".[dev]"

# docker compose needs a .env to exist. Generate one with real secrets —
# the dashboard refuses to start on the example's placeholder values.
.env:
	@python3 tools/init_env.py

demo: .env
	docker compose up -d --build
	docker compose exec -T api python tools/quickstart.py

up: .env
	docker compose up -d --build

down:
	docker compose stop

clean:
	docker compose down -v

logs:
	docker compose logs -f api worker

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

TEST_ENV = DATABASE_URL="sqlite+aiosqlite:///:memory:" ADMIN_API_KEY=test-key JWT_SECRET=test-secret

test-backend:
	$(TEST_ENV) pytest -q

test-hook:
	cd integrations/claude-code-spine && python3 -m pytest tests/ -q

smoke:
	bash tools/smoke_plan_bound.sh

dashboard-check:
	cd spine-dashboard && npx tsc --noEmit && npx vite build --outDir /tmp/spine-dashboard-build

test-cpp:
	cmake -S sdks/cpp -B sdks/cpp/build -DCMAKE_BUILD_TYPE=Release
	cmake --build sdks/cpp/build -j4
	./sdks/cpp/build/spine_tests

test: test-backend test-hook

verify: test-backend test-hook test-cpp smoke dashboard-check
	@echo ""
	@echo "All checks passed."

openapi:
	PYTHONPATH=. $(TEST_ENV) python3 tools/export_openapi.py
