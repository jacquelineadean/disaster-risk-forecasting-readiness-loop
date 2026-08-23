.PHONY: help install snapshot panel loop canary verify test report serve ledger clean-derived

PY ?= python3

help:            ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

install:         ## install the package (editable). No runtime dependencies.
	$(PY) -m pip install -e .

snapshot:        ## pull and pin the public data (~300 MB, one time)
	$(PY) -m readiness.cli snapshot

panel:           ## build the labelled county-quarter panel, show split coverage
	$(PY) -m readiness.cli panel

loop:            ## run the Phase 0 experimental loop end to end
	$(PY) -m readiness.cli loop

canary:          ## demonstrate the harness rejecting a leaked model
	$(PY) -m readiness.cli canary

verify:          ## check the Phase 0 exit criteria
	$(PY) -m readiness.cli verify

ledger:          ## show the experiment ledger and verify its hash chain
	$(PY) -m readiness.cli ledger

test:            ## run the test suite (no network required)
	$(PY) -m unittest discover -s tests -t . -v

report:          ## rebuild report/index.html from the design source
	$(PY) tools/build_report.py

serve:           ## serve the report at http://localhost:8137
	$(PY) -m http.server 8137 --directory report

clean-derived:   ## drop derived artefacts; keeps snapshots and the ledger
	rm -rf report/index.html
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
