.PHONY: lint test record export bench-dev
lint:
	uv run ruff check . && uv run ruff format --check .
test:
	uv run pytest -q
record:
	uv run python -m always_200 record --seeds $(SEEDS) --run $(RUN) $(if $(filter 1,$(QUICK)),--quick,)
export:
	uv run python -m always_200 export --seeds $(SEEDS) --run $(RUN) $(if $(filter 1,$(QUICK)),--quick,)
bench-dev:
	uv run python -m always_200 bench --recordings recordings/$(RUN) --detectors never,always
