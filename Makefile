.PHONY: help install snapshot panel features score loop promote backtest phase1 loop-all backtest-all fleet-status canary verify test report serve ledger contracts dashboard docs-capture site serve-site clean-derived exposure-snapshot exposure-show spot-check issue brief issue-all brief-all phase2

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

# The period `make issue` and `make brief` work on, in the contract's own shape:
# YYYY-Qn for a quarterly contract, YYYY-Mnn for a monthly one, YYYY for an
# annual one.
PERIOD ?=
# Which county or state `make brief` writes for; one of the two.
COUNTY ?=
STATE ?=
# Which states `make exposure-snapshot` pulls; empty means every state in the
# pinned Census county file.
STATES ?=
SFLAG = $(if $(STATES),--states $(STATES),--all-states)

# The promoted model and its arguments for one contract, spelled as `issue`
# takes them: the first passing test card under the current contract digest, or
# nothing at all when the contract has not promoted anything. `make phase2`
# issues exactly the model each contract earned, with the arguments its card
# records, rather than assuming one model for the fleet.
PROMOTED = $(PY) -c 'import sys;from readiness import contracts,data;from readiness.harness.ledger import Ledger;c=contracts.load(sys.argv[1]);cards=[k for k in Ledger(data.paths(c).ledger).read() if k.split=="test" and k.contract_digest==c.digest() and (k.verdict or {}).get("passed") and not (k.canary or {}).get("rejected")];print("" if not cards else " ".join([cards[0].model]+["--param "+k+"="+(",".join(map(str,v)) if isinstance(v,list) else str(v)) for k,v in sorted(cards[0].data_snapshot.get("model_kwargs",{}).items())]))'

# The states the committed assessor counts sample, for `make phase2`'s briefs.
SPOT_STATES = $(PY) -c 'from readiness import verify;from readiness.exposure import spotcheck;print(" ".join(sorted({r.fips[:2] for r in spotcheck.load(verify.COUNTS_PATH)})))'

help:            ## show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
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

phase1:          ## snapshot, features, the Phase 1 queue with promotion, the backtest, verify --phase 1  (CONTRACT=)
	$(PY) -m readiness.cli snapshot $(CFLAG) $(FFLAG)
	$(PY) -m readiness.cli features $(CFLAG) $(FFLAG)
	$(PY) -m readiness.cli loop $(CFLAG) $(FFLAG) --queue phase1 --promote
	$(PY) -m readiness.cli backtest $(CFLAG)
	$(PY) -m readiness.cli verify $(CFLAG) --phase 1

loop-all:        ## the fleet: every national contract through the Phase 2 queue, promoting each first validate pass
	$(PY) -m readiness.cli fleet --national --queue phase2 --promote $(FFLAG)

backtest-all:    ## write every registered contract's backtest report from its committed files
	for c in $$($(PY) -m readiness.cli contracts --names); do \
	  $(PY) -m readiness.cli backtest -c $$c || exit 1; \
	done

fleet-status:    ## where every registered contract stands, from the ledgers alone
	$(PY) -m readiness.cli fleet --status

exposure-snapshot: ## pull and pin USA Structures county counts (STATES=OK,LA; default every state)
	$(PY) -m readiness.cli exposure snapshot $(SFLAG)

exposure-show:   ## print the pinned county rows (COUNTY=40001 or STATE=OK; default every pinned state)
	$(PY) -m readiness.cli exposure show $(if $(COUNTY),--county $(COUNTY),$(if $(STATE),--state $(STATE),))

spot-check:      ## our county totals over the committed assessor counts; exit 1 below the exit rule
	$(PY) -m readiness.cli exposure spot-check

issue:           ## refit MODEL and write issued/<contract>/<PERIOD>.json (MODEL=, PERIOD=, CONTRACT=)
	@test -n "$(PERIOD)" || { echo "issue: pass PERIOD=YYYY-Qn (or YYYY-Mnn, or YYYY)"; exit 2; }
	$(PY) -m readiness.cli issue $(MODEL) $(CFLAG) --period $(PERIOD) $(FFLAG)

brief:           ## write briefs/<fips>/<PERIOD>.html and .json (COUNTY= or STATE=, PERIOD=)
	@test -n "$(PERIOD)" || { echo "brief: pass PERIOD=YYYY-Qn"; exit 2; }
	@test -n "$(COUNTY)$(STATE)" || { echo "brief: pass COUNTY=40109 or STATE=OK"; exit 2; }
	$(PY) -m readiness.cli brief $(if $(COUNTY),--county $(COUNTY),--state $(STATE)) --period $(PERIOD)

issue-all:       ## issue every national contract that has a passing test card (PERIOD=)
	@test -n "$(PERIOD)" || { echo "issue-all: pass PERIOD=YYYY-Qn"; exit 2; }
	for c in $$($(PY) -m readiness.cli contracts --names --national); do \
	  spec=$$($(PROMOTED) $$c); \
	  if [ -n "$$spec" ]; then \
	    $(PY) -m readiness.cli issue $$spec -c $$c --period $(PERIOD) $(FFLAG) || true; \
	  else echo "issue-all: $$c has no passing test card; nothing to issue"; fi; \
	done

brief-all:       ## a brief per county of every state the assessor counts sample (PERIOD=)
	@test -n "$(PERIOD)" || { echo "brief-all: pass PERIOD=YYYY-Qn"; exit 2; }
	for st in $$($(SPOT_STATES)); do \
	  $(PY) -m readiness.cli brief --state $$st --period $(PERIOD) || true; \
	done

phase2:          ## the fleet, the exposure join, issuance, the briefs, verify --phase 2 (PERIOD=)
	@test -n "$(PERIOD)" || { echo "phase2: pass PERIOD=YYYY-Qn, the period to issue"; exit 2; }
	$(MAKE) loop-all
	$(MAKE) backtest-all
	$(PY) -m readiness.cli exposure snapshot --all-states
	# The spot-check and the issue/brief refusals below report rather than stop:
	# `verify --phase 2` at the end is the gate that decides the phase.
	-$(PY) -m readiness.cli exposure spot-check
	$(MAKE) issue-all PERIOD=$(PERIOD)
	$(MAKE) brief-all PERIOD=$(PERIOD)
	$(PY) -m readiness.cli verify --phase 2

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
