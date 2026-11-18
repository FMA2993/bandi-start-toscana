run := .venv/bin/python
BANDI := $(run) start_toscana.py

.PHONY: help install update list report test clean

help:
	@echo "make install   crea .venv e installa le dipendenze (uv se disponibile)"
	@echo "make update    scrape completo: JSON + CSV + report HTML + allegati"
	@echo "make list      solo elenco bandi aperti a video (veloce)"
	@echo "make report    rigenera output/report_bandi.html dal JSON esistente"
	@echo "make test      pytest (offline, sulle fixture)"
	@echo "make clean     rimuove .venv e le cache"

install:
	@if command -v uv >/dev/null 2>&1; then \
		uv venv .venv --quiet && uv pip install --python .venv/bin/python --quiet -e ".[dev]" && echo "ok (uv)"; \
	else \
		python3 -m venv .venv && .venv/bin/python -m pip install --quiet -e ".[dev]" && echo "ok (venv)"; \
	fi

update:
	$(BANDI) --json output/bandi_aperti.json --csv output/bandi_aperti.csv \
		--html output/report_bandi.html --download allegati --timeout 90 --workers 5

# solo dati (JSON/CSV/report), niente allegati: è quello che gira in CI
update-data:
	$(BANDI) --json output/bandi_aperti.json --csv output/bandi_aperti.csv \
		--html output/report_bandi.html --timeout 90 --workers 5

list:
	$(BANDI) --no-detail

# prendi i dati aggiornati dalla CI e apri il report (quello che serve a papà)
sync:
	git pull --rebase --autostash
	@open output/report_bandi.html 2>/dev/null || echo "→ apri output/report_bandi.html"

report:
	$(BANDI) --from-json output/bandi_aperti.json --html output/report_bandi.html --download allegati

test:
	$(run) -m pytest -q

clean:
	rm -rf .venv __pycache__ tests/__pycache__ .pytest_cache
