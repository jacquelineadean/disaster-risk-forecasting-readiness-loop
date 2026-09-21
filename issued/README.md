# issued/

What `readiness issue` writes: one `<contract>/<period>.json` per contract and
forecast period, each carrying the probabilities for every county in the
contract's universe and the provenance that lets a reader re-derive them — the
contract digest, the model, version and constructor arguments, the id of the
test card that entitled the model to be issued, the training and feature
digests the refit had to match, the data and feature versions, the digest of
the guarded code at issue time, and the manifest keys behind all of it. The
files are outputs, so git ignores everything here but this page; a real run's
files are committed deliberately, beside the ledger cards they cite, because a
county brief cites them by path and period and the website re-validates them.
