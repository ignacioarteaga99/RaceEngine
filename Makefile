.PHONY: help install run demo test lint santiago clean

PY := ./.venv/bin/python
export PYTHONPATH := $(PWD)/packages/contracts:$(PWD)/packages/routing:$(PWD)/packages/geodata:$(PWD)/packages/engine:$(PWD)/apps/api

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install:  ## Crea el entorno virtual e instala dependencias
	python3.12 -m venv .venv && ./.venv/bin/pip install -q -U pip -r requirements.txt

run:      ## Levanta la app en http://localhost:8000
	./run.sh

demo:     ## Construye la región sintética (sin red)
	$(PY) -m raceengine_geodata.cli build demo --force

santiago: ## Descarga Santiago Centro de OSM + elevación
	$(PY) -m raceengine_geodata.cli build santiago_centro --elevation opentopodata

regions:  ## Lista las regiones y su estado
	$(PY) -m raceengine_geodata.cli list

test:     ## Ejecuta la suite completa
	$(PY) -m pytest tests/ -q

lint:     ## ruff
	./.venv/bin/ruff check packages apps tests

clean:    ## Borra grafos y cachés generados
	rm -rf data/graphs data/cache .pytest_cache
