# briefs/

What `readiness brief` writes: `<fips>/<period>.html` and `<fips>/<period>.json`
per county and forecast period — the rendered page and the `cite.Document` it
was rendered from, so anyone can re-run `readiness.cite.validate` on the
committed file and get the same answer the command got before it wrote
anything. A brief with a violation is never written. The files are outputs, so
git ignores everything here but this page; a real run's briefs are committed
deliberately, and `tools/build_site.py` lists the ones that still validate
against the repository as it stands.
