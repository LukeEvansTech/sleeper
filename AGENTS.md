# sleeper agent guide

Single-user trading-card deal finder. `README.md` covers what it does and how to run it; this file
holds what the code does not say.

- Pipeline: `sleeper/sweep.py` `run()` (collect per want → store → photo check → alert). Matching
  and ceilings: `match.py`. Verdicts: `value.py` (pure; test it there). Pages: `web.py` and
  `templates/`.
- Deployed on the talos-cluster (`kubernetes/apps/custom/sleeper`), which alerts on
  `sleeper_last_success_timestamp_seconds` from `GET /metrics`. The cluster runs the image of the
  latest `v*` tag: a merge to `main` deploys nothing until a tag is pushed and Renovate's pin PR merges.
- Before pushing: `python -m pytest -q`, then black, isort, flake8, pylint, ruff and mypy with the
  configs in `.github/linters/` (super-linter runs the same).

## Review rules

- **Mark an alert sent only after the send succeeded.** The alerts table is the ledger; writing it
  first loses the alert for good when the send fails.
- **A sweep that searched nothing is not a success.** `/metrics` reads `searches > 0` and the alert
  status as well as `status`.
- **eBay pages fail at random.** `Browser.search` returns `None` after every try failed and `[]`
  only for a genuine no-results page; code must not treat `None` as "no listings", or live
  listings get marked inactive.
- **Do not add evasion of eBay's bot checks** (stealth patches, captcha solving). A blocked load is
  retried as is; sold prices come from a signed-in browser through `/api/sold`.
