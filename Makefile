.PHONY: help install snapshot panel loop canary verify test report serve ledger contracts dashboard docs-capture site serve-site clean-derived

PY ?= python3
# Which registered contract to run against. Leave empty to let the CLI resolve
# it ($READINESS_CONTRACT, or the sole registered contract).
CONTRACT ?=
CFLAG = $(if $(CONTRACT),-c $(CONTRACT),)

help:            ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  pass CONTRACT=<name> to pick a registered contract, e.g. make loop CONTRACT=flood-xx"

install:         ## install the package (editable). No runtime dependencies.
	$(PY) -m pip install -e .

contracts:       ## list the registered contracts
	$(PY) -m readiness.cli contracts

snapshot:        ## pull and pin the public data for the contract (~300 MB, one time)
	$(PY) -m readiness.cli snapshot $(CFLAG)

panel:           ## build the labelled region-period panel, show split and event coverage
	$(PY) -m readiness.cli panel $(CFLAG)

loop:            ## run the experimental loop end to end
	$(PY) -m readiness.cli loop $(CFLAG)

canary:          ## demonstrate the harness rejecting a leaked model
	$(PY) -m readiness.cli canary $(CFLAG)

verify:          ## check the Phase 0 exit criteria
	$(PY) -m readiness.cli verify $(CFLAG)

ledger:          ## show the experiment ledger and verify its hash chain
	$(PY) -m readiness.cli ledger $(CFLAG)

dashboard:       ## render every contract's ledger to experiments/<name>/dashboard.html
	$(PY) -m readiness.cli dashboard --all

docs-capture:    ## regenerate docs/media with Playwright (needs `pip install -e '.[docs]'`)
	$(PY) tools/demo/capture.py

test:            ## run the test suite (no network required)
	$(PY) -m unittest discover -s tests -t . -v

report:          ## rebuild report/index.html from the design source
	$(PY) tools/build_report.py

serve:           ## serve the report at http://localhost:8137
	$(PY) -m http.server 8137 --directory report

site:            ## build the overview website's data into site/generated (packs pinned data if present)
	$(PY) tools/build_site.py

serve-site:      ## serve the overview website at http://localhost:8138 (run `make site` first)
	$(PY) -m http.server 8138 --directory site

clean-derived:   ## drop derived artefacts; keeps snapshots, the ledgers and the committed report
	rm -rf experiments/*/dashboard.html experiments/index.html site/generated
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
