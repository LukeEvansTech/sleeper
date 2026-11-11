# sleeper

A personal trading-card deal finder. It searches eBay.com (US view, priced for delivery to a US
forwarding address) for every card on a want list, judges each listing against your ceiling, recent
sold prices and the cost of grading a raw copy, and sends one alert per new deal. Each alert opens
that deal's own page, with the listing photo beside the official art and a verdict on whether to
buy the slab, buy raw and grade, or pass.

## How it works

```text
sweep (cron) ─▶ eBay search per want (Browse API or headless Chromium) ─▶ listings
              ─▶ ceiling + comps + gem rate ─▶ verdict per listing
              ─▶ photo check against the official art (OpenCV ORB)
              ─▶ slab check: a vision model reads the label; a CGC or non-10 slab is dropped
              ─▶ alert each new deal (Apprise), deal page per alert
```

- **Wants** are edited on the Wants page (or loaded with `POST /api/import`). A Union Arena want
  (set `FMA`, `EVA`, `CSM`, `AOT`) builds its searches from the card name; any other card takes an
  eBay query. Ceilings are per star bucket, in USD, compared with the landed price (price plus
  delivery). The Wants page shows each want as a card with its official art; on the page for one want you
  can untick arts you don't want, and listings whose photo matches only those stop counting as
  deals. 1★ defaults for cards with no ceiling of their own are set at the foot of the Wants page.
- **One print per want**: a want whose `cards` is a single print (`UE09BT/FMA-1-012_p1 SR★★`) only
  keeps listings whose title agrees with that print's number and star count. Prints split from one
  card carry `family` (the card's want ID) and `match` (its name): they share one eBay search and
  one set of sold comps, each listing goes to one print, and a deal whose photo matches a sibling's
  art moves to that sibling. Clicking a want opens its live listings and deals.
- **Deal**: under the ceiling and either `great`, `good` or `unknown` (no sold prices yet). A raw
  copy rated `fair` or `high` still counts when grading it costs under 85% of the PSA 10 sold
  median. Anything else under the ceiling is listed but not alerted.
- **Running auctions** are rated on their likely finish, not the bid so far: the larger of the
  landed bid and the sold median plus this listing's delivery. One with comps is a deal (and alerts) while it runs only when
  that likely finish is within the ceiling and a max bid is still possible, or for a raw copy that
  is cheap to grade at that price. With no comps it is `unknown` and not a deal until it is within six hours
  of its end, the default sweep interval. Each shows a **max bid**: the lower of the
  ceiling and 92% of the PSA 10 sold median (for a raw copy: what still beats grading, else 92% of the raw median), less delivery, then nudged to an amount other bidders
  avoid (not ending in 0 or 5, .01 or .99), or none when that is below the next eBay increment.
- **Outcomes**: each sweep asks the Browse API how listings that left the search (or auctions past
  their end) finished, up to 200 a sweep, again after 20 hours while still on sale and not after 30
  days. A sold auction's final price joins the sold comps (fixed-price sales do not: their price is unknown); the Ended page lists deals and alerts with
  their final price and, for an auction, whether sniping at sleeper's max bid would have won it
  and at about what price. That max bid is worked out as it stood just before the end, from sales
  on earlier days and never the auction's own result. Beating the final price counts as "in range",
  not a win, because eBay hides the winner's own maximum, and the page totals the wins and losses.
- **Verdict**: `great` / `good` / `fair` / `high` against the 90-day sold median for the same
  card, kind (PSA 10 or raw) and star bucket, plus the cost of one PSA 10 by grading a raw copy:
  `(raw price + grading fee + shipping) / gem rate`. The gem rate is the card's own PSA pop when 10
  or more have been graded, otherwise the set's.
- **Search source**: with `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` set (a production keyset from
  the eBay developer console), searches and listing photos go through the Browse API; without
  them, through headless Chromium. The Browse API query has no `-word` exclusion, so excluded words are
  matched against titles instead, and it has no sold search.
- **Sold comps**: eBay sends signed-out sold searches to a sign-in page, so sold prices are posted
  to `POST /api/sold` by a signed-in browser elsewhere.
- **Alerts**: the first sweep records a baseline and sends one summary. After that each new deal
  (or a deal now 10% cheaper than when alerted) gets its own alert, up to
  `SLEEPER_ALERT_INDIVIDUAL` per sweep; the rest go in one digest that opens the sweep's page. A
  listing whose photo does not match the official art is held back. A listing is marked alerted
  only after the send succeeds.

## Run it

```sh
uv venv -p 3.13 .venv && uv pip install -p .venv -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
SLEEPER_DATA=data SLEEPER_SCHEDULE='0 0 1 1 *' .venv/bin/python -m sleeper serve   # :8080
.venv/bin/python -m sleeper sweep --only Vegito --no-notify
```

Docker: `docker build -t sleeper .` then
`docker run --read-only --tmpfs /tmp -v sleeper:/data -p 8080:8080 sleeper`. The image runs as
uid 65532 and works with a read-only root filesystem as long as `/tmp` is writable.

## Configuration

| Variable                   | Default             | Description                                                            |
| -------------------------- | ------------------- | ---------------------------------------------------------------------- |
| `SLEEPER_DATA`             | `data`              | Directory for `sleeper.db`, official art (`refs/`) and photos.         |
| `SLEEPER_PUBLIC_URL`       | none                | Base URL of the web UI; alert links are absolute when set.             |
| `SLEEPER_SCHEDULE`         | `0 2,8,14,20 * * *` | Cron for the in-process sweep.                                         |
| `SLEEPER_TZ`               | `Europe/London`     | Timezone the schedule is read in.                                      |
| `APPRISE_URLS`             | none                | Comma-separated Apprise URLs. Empty sends nothing.                     |
| `SLEEPER_ALERT_INDIVIDUAL` | `5`                 | Deals per sweep that get their own alert; the rest share a digest.     |
| `SLEEPER_GRADING_FEE`      | `39.99`             | PSA fee per card, USD.                                                 |
| `SLEEPER_GRADING_SHIP`     | `6`                 | Return shipping per card, USD.                                         |
| `SLEEPER_TRIES`            | `8`                 | Page loads per search before it counts as failed.                      |
| `EBAY_CLIENT_ID`           | none                | Browse API App ID. With the secret, replaces the browser for searches. |
| `EBAY_CLIENT_SECRET`       | none                | Browse API Cert ID.                                                    |
| `SLEEPER_LLM_URL`          | none                | Endpoint (OpenAI-compatible) that reads slab labels; empty: no check.  |
| `SLEEPER_LLM_KEY`          | none                | Its API key.                                                           |
| `SLEEPER_LLM_MODEL`        | `claude`            | Vision model name at that endpoint.                                    |

## API

| Route                  | Purpose                                                                                   |
| ---------------------- | ----------------------------------------------------------------------------------------- |
| `POST /api/import`     | `{wants, defaults, gem_rates, block, pop: {fetched, rows}}`; wants match on set and name. |
| `POST /api/sold`       | `{sold: {item_id: {set, who, kind, star, usd, date, title}}}`.                            |
| `PUT /api/refs/<name>` | Upload one official-art image (JPEG or PNG).                                              |
| `GET /health`          | Process is up.                                                                            |
| `GET /metrics`         | `sleeper_last_success_timestamp_seconds` and friends, for alerting.                       |

The UI has no login: run it behind an internal-only route.
