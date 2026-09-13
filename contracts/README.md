# contracts/

Pre-registered contracts, one JSON file per contract, named after the
contract (`<name>.json`). Register one with:

```bash
readiness register <name> --hazard <hazard> --state XX
```

and read it back with `readiness contract -c <name>`. The schema, the defaults
and the reasoning behind each field are in `docs/contracts.md`. A contract's
criteria are hashed; the hash is stamped on every experiment card written
against it, so editing a contract in place after experiments have run makes
those experiments visibly incomparable. Register a new name instead.
