# Security

This software is decision support and never a warning channel: nothing here
should be used to decide whether to act on a hazard that is imminent.
Official alerts come from the National Weather Service and IPAWS.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository: **Security,
then Report a vulnerability**. If that form is not enabled, open an issue that
says only that you have a report and want a private channel, with no details,
and one will be arranged.

Include the commit or tag you tested, the steps to reproduce, and what an
attacker could do with it.

## Where a finding matters most

- The connectors in `readiness/connectors/`: they fetch, checksum and pin
  public data, and read partner records and EM-DAT exports whose bytes must
  never be committed or published.
- `tools/build_site.py`: it must never pack anything under
  `snapshots/records/` or publish a pilot's labels.
- The blinding in `readiness/plans/`: a blinded gap report must carry no name,
  address, coordinate or county of a real facility.
- The hash chain and anchors under `experiments/`, and the test-touch budget.

A leak of a real facility's identity or of a partner's data is treated as a
security issue, not a documentation one.
