.PHONY: help install snapshot panel features score loop promote backtest phase1 canary verify test report serve ledger contracts dashboard docs-capture site serve-site clean-derived

PY ?= python3
# Which registered contract to run against. Leave empty to let the CLI resolve
# it ($READINESS_CONTRACT, or the sole registered contract).
CONTRACT ?=
CFLAG = $(if $(CONTRACT),-c $(CONTRACT),)
# Which exit criteria `make verify` checks: 0 (baselines reproduce, canary
# rejected) or 1 (ledger-only: the promoted test card and its record).
PHASE ?= 0
# Feature connectors for the Phase 1 targets, comma-separated. Leave empty to
# load every connector whose pinned data is present.
FEATURES ?=
FFLAG = $(if $(FEATURES),--features $(FEATURES),)
# The model `make score` and `make promote` run: the one `make promote` spends
# the test touch on.
MODEL ?= logistic
# Constructor arguments for that model, space-separated `key=value` pairs, each
# expanded into a --param flag: PARAMS='iters=800 feature_sets=era5-antecedent,terrain'.
# A value may hold commas, which is why the separator here is a space. Left
# empty, `promote` adopts the arguments of the validate card that passed.
PARAMS ?=
PFLAG = $(foreach p,$(PARAMS),--param $(p))
# Which split `make score` fits against. Never test: `promote` is the only way
# onto the test split, because the touch and the card are one step.
SPLIT ?= validate

help:            ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  pass CONTRACT=<name> to pick a registered contract, e.g. make loop CONTRACT=flood-xx"
	@echo "  pass PARAMS='key=value key=value' to score or promote with model arguments"

install:         ## install the package (editable). No runtime dependencies.
	$(PY) -m pip install -e .

contracts:       ## list the registered contracts
	$(PY) -m readiness.cli contracts

snapshot:        ## pull and pin the public data for the contract (~300 MB, one time; FEATURES=era5,terrain adds sources)
	$(PY) -m readiness.cli snapshot $(CFLAG) $(FFLAG)

panel:           ## build the labelled region-period panel, show split and event coverage
	$(PY) -m readiness.cli panel $(CFLAG)

features:        ## load the feature sources; print admission verdicts, columns and the audit
	$(PY) -m readiness.cli features $(CFLAG) $(FFLAG)

score:           ## fit and score MODEL on SPLIT (default validate) with PARAMS
	$(PY) -m readiness.cli score $(MODEL) $(CFLAG) $(FFLAG) $(PFLAG) --split $(SPLIT)

loop:            ## run the experimental loop end to end (baseline queue)
	$(PY) -m readiness.cli loop $(CFLAG)

promote:         ## spend the one test touch on MODEL (default logistic) after its validate pass
	$(PY) -m readiness.cli promote $(MODEL) $(CFLAG) $(FFLAG) $(PFLAG) --spend-test-touch

backtest:        ## write experiments/<name>/backtest.html and .json from committed files only
	$(PY) -m readiness.cli backtest $(CFLAG)

phase1:          ## snapshot, features, the Phase 1 queue with promotion, the backtest, verify --phase 1
	$(PY) -m readiness.cli snapshot $(CFLAG) $(FFLAG)
	$(PY) -m readiness.cli features $(CFLAG) $(FFLAG)
	$(PY) -m readiness.cli loop $(CFLAG) $(FFLAG) --queue phase1 --promote
	$(PY) -m readiness.cli backtest $(CFLAG)
	$(PY) -m readiness.cli verify $(CFLAG) --phase 1

canary:          ## demonstrate the harness rejecting a leaked model
	$(PY) -m readiness.cli canary $(CFLAG)

verify:          ## check the exit criteria for PHASE (default 0)
	$(PY) -m readiness.cli verify $(CFLAG) --phase $(PHASE)

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
