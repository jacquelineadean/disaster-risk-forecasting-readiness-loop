# contracts/

Pre-registered contracts, one JSON file per contract, named after the
contract (`<name>.json`). Register one with:

```bash
readiness register <name> --hazard <hazard> --state XX     # one state
readiness register <name> --hazard <hazard>                # the whole country
```

and read it back with `readiness contract -c <name>`. The schema, the defaults
and the reasoning behind each field are in `docs/contracts.md`. A contract's
criteria are hashed; the hash is stamped on every experiment card written
against it, so editing a contract in place after experiments have run makes
those experiments visibly incomparable. Register a new name instead.

## Registered

Nine contracts ship registered. The three state contracts have committed
ledgers and blessed fingerprints under `experiments/` and `harness_expected/`;
the six national ones (plan §3, "six so four can pass") are registered as data
only and **have no ledger yet** — `make loop-all` writes one per contract, and
`make fleet-status` shows where each stands.

| contract | hazard | scope | period | zone events |
|---|---|---|---|---|
| `inland-flood-la` | inland flood | Louisiana parishes | quarter | dropped |
| `tornado-ok` | tornado | Oklahoma counties | quarter | dropped |
| `tropical-cyclone-gulf` | tropical cyclone | TX, LA, MS, AL, FL | month | expanded |
| `inland-flood-us` | inland flood | every US county | quarter | dropped |
| `tornado-us` | tornado | every US county | quarter | dropped |
| `hail-us` | hail | every US county | quarter | dropped |
| `severe-wind-us` | severe wind | every US county | quarter | dropped (Thunderstorm Wind is county-coded) |
| `winter-storm-us` | winter storm | every US county | month | expanded (zone-coded hazard) |
| `heat-us` | heat | every US county | month | expanded (zone-coded hazard) |

Every national contract was written by `readiness register` with the
documented defaults for everything the table does not show, so its digest is
the digest of the defaults plus the hazard, the period and the zone policy.
