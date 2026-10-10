.PHONY: lint test record export bench-dev tune up down smoke
lint:
	uv run ruff check . && uv run ruff format --check .
test:
	uv run pytest -q
record:
	uv run python -m always_200 record --seeds $(SEEDS) --run $(RUN) $(if $(filter 1,$(QUICK)),--quick,)
export:
	uv run python -m always_200 export --seeds $(SEEDS) --run $(RUN) $(if $(filter 1,$(QUICK)),--quick,)
bench-dev:
	uv run python -m always_200 bench --recordings recordings/$(RUN) --detectors never,always $(if $(filter 1,$(TICKETS)),--tickets,)
tune:
	uv run python -m always_200 tune --session recordings/A1 --out $(or $(OUT),runs/tune)
up:
	docker compose up -d --build
down:
	docker compose down
smoke:
	uv run pytest -m docker -q tests/patient/test_compose_smoke.py
