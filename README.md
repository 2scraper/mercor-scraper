# mercor-scraper

Scrapes [mercor.com](https://mercor.com) — the AI-talent marketplace's job
index, individual listings, and Mercor's own corporate openings — with
Playwright, Selenium, Puppeteer, or the 2Captcha **Scraping Browser API**
over CDP.

[![release](https://img.shields.io/github/v/release/2scraper/mercor-scraper)](https://github.com/2scraper/mercor-scraper/releases)
[![tests](https://github.com/2scraper/mercor-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/mercor-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/mercor-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/mercor-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20puppeteer-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-success)](#you-do-not-need-to-buy-anything-for-this-site)

---

## You do not need to buy anything for this site

This is the honest headline, and it is unusual enough in this family to put
above everything else: **every route this scraper reads was served in full
to a bare datacentre address with no credentials at all.**

Measured 2026-09-18 from a Hetzner VPS in Helsinki (AS24940):

| Route | Result |
|---|---|
| `work.mercor.com/explore` | 200 — **390 listings in one response** |
| `work.mercor.com/jobs/{id}/{slug}` | 200 — twelve rapid fetches, ~0.4 s each, no rate limiting |
| `www.mercor.com/careers` | 200 — **108 roles** |
| `work.mercor.com/sitemap.xml` | 200 — **447 job URLs** |

…to `curl`, to `python-requests`, and to a request with an **empty**
User-Agent. Eighteen candidate bot-challenge markers were counted across
seven captures — `cf-turnstile`, `challenges.cloudflare.com`, DataDome,
PerimeterX, Incapsula, Akamai, `errors.edgesuite.net`, AWS WAF, reCAPTCHA
in both spellings, hCaptcha, `data-sitekey` and the rest — and **every one
was zero**.

So this is a working command:

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium
python3 playwright_scraper.py
```

No key, no proxy, no account. What the paid products actually buy here is
in [What the 2Captcha products are for](#what-the-2captcha-products-are-for)
— it is volume and a specific exit country, not access.

---

## Quick start

```bash
# The marketplace index — 390 contract roles in one response
python3 playwright_scraper.py --mode listings --format both --out mercor

# Mercor's own salaried openings (a different host, a different population)
python3 playwright_scraper.py --mode careers

# Every listing's detail page. A "page" here is one job, enumerated from the
# sitemap AND the index — 462 of them on 2026-09-18.
# (--concurrency is Playwright-only; the other two engines log that they
#  ignore it and fetch one page at a time.)
python3 playwright_scraper.py --mode job --pages 462 --concurrency 4

# One specific listing
python3 playwright_scraper.py --mode job \
  --url "https://work.mercor.com/jobs/list_AAABoGhhxWgF7ygE2LZOgIy3/b-2-b-sales-expert-3-yoe-us-only"
```

Every mode has a correct default URL, so `--url` is optional — Mercor's
index accepts no query parameters, so there is only one address per route.

---

## Three modes, because the site publishes three different things

| Mode | Route | What it gives you |
|---|---|---|
| `listings` *(default)* | `work.mercor.com/explore` | The whole marketplace index in **one** response: rate ranges, the site's own `domain` category, work/residence eligibility, slot counters. |
| `job` | `work.mercor.com/jobs/{id}/{slug}` | One listing, adding the company website, company description, spot counts and a **currency** the index does not publish. The only mode that covers listings the index omits. |
| `careers` | `www.mercor.com/careers` | Mercor's own salaried roles, via the Ashby ATS. Department, team, Ashby apply URL, salary tiers. |

`listings` and `job` are two views of **one** thing, keyed by the same
`listingId`, so a `job` run enriches a `listings` run and `diff_runs.py`
joins them on `sku`. `careers` is a genuinely different population with its
own UUID id space and **no ids in common**, so `diff_runs.py` refuses to
compare it against the other two — every row would otherwise read as both
added and removed.

---

## Traps that look like bugs

Read this section before concluding the tool is broken.

**Most listings do not name a company, and that is the site.** 99 of 390
name one; the other 291 are clients Mercor runs the hiring for without
naming them. `company_brand_visible` is the site's own flag saying which is
which, so a null `company_name` beside `False` is *withheld*, not missing.

**`/explore` is complete and is not exhaustive.** A `--mode listings` run
fetches everything that route serves — genuinely `status: complete` — and
the route held **390 of the 462** listings the site publishes. Neither
enumeration source contains the other:

```
sitemap.xml   447 job URLs
/explore      390 records
shared        375
sitemap only   72   ← all three checked were live, status "active"
explore only   15
union         462
```

Use `--mode job` to cover the 72. The sidecar records both figures so a
consumer never has to infer one from the other.

**The index has no second page.** `?page=2`, `?limit=10`, `?offset=100` and
`?domain=Finance` each returned a byte-identical payload. `--pages` above 1
is ignored in `--mode listings` and `--mode careers`, with a log line
saying so. `--mode job` is the mode that paginates, and there a page is one
job.

**Rates are not all hourly.** Across the 390: `hourly` 347, `per-task` 31,
`one-time` 9, `yearly` 3. Read `rate_period` before doing arithmetic on
`rate_min`/`rate_max` — and note `commitment` is a *different* axis (how
much time is asked for: hourly 228, part-time 104, task-based 35, full-time
23). A task-based commitment can still be paid an hourly rate, and 31 are.

**`rate_currency` is null on every listings row.** The index publishes no
currency anywhere, so the column is null rather than a defaulted `"USD"`.
Detail pages do state one — USD on 40 of 40 sampled — and `--mode job`
fills it. The careers route publishes USD *and* GBP, so nothing is assumed
there either.

**The site's own JSON-LD is incomplete — do not use it.** `/explore` ships
an `ItemList` of 326 URLs beside a React Query cache of 390 full records.
The 64 it omits are all 55 `evergreen` listings plus 9 standard. This
scraper reads the cache; a parser anchored on the JSON-LD would silently
lose 16% of the catalogue.

**Hydration rewrites every job link.** The served markup has
`href="/jobs/list_…"`; the live DOM has `href="/explore?listingId=…"`. It
costs nothing here (rows come from `__NEXT_DATA__`, not the DOM) but it
will bite anyone writing a DOM-based selector against a saved capture.

---

## Output

One row shape across all three modes — `JobPosting` in `output_writer.py`.
See [`sample_output.json`](sample_output.json) and
[`sample_output.csv`](sample_output.csv), both cut from real runs on
2026-09-18 and including the same listing read **both** ways so the join on
`sku` is visible.

The family prefix — `source`, `scraped_at`, `url`, `sku`, `title` — is
byte-identical and in that order across every repo in this family.

There is no `price`, `currency`, `discount_pct`, `in_stock` or `brand`
column: this is a job board, and six columns null on every row of every run
are worse than absent ones. There is no `rating` either, and that is a
measurement — Mercor publishes no rating, score or review count for a
listing, a client or a contractor on any route read here.

`data_source` says which route produced a row (`explore` / `detail` /
`careers`), because **neither route is a subset of the other**: the index
has `domain`, `posted_at` and the slot counters; a detail page has the
company website, the company description and a currency. Checked field by
field on the same job: **zero disagreements** on the 37 columns both
publish.

**Exit codes:** `0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero
rows · `5` remote API error · `6` partial.

A run that finds nothing **writes nothing** — last night's good output is
never replaced with `[]`. `--allow-empty` opts out.

---

## Engines

Playwright is primary. All three agree on exit codes, run status and the
rows themselves — verified live on 2026-09-18 across all three modes, with
row output identical except one listing whose `remaining_slots` changed
between runs, which is the site and not the engines.

```bash
pip install -r requirements.txt -r requirements-playwright.txt  # then: playwright install chromium
pip install -r requirements.txt -r requirements-selenium.txt
pip install -r requirements.txt -r requirements-puppeteer.txt
```

**Install exactly one.** Playwright and pyppeteer declare mutually
unsatisfiable pins (`pyee` <12 vs ≥13), and pyppeteer and selenium collide
on `urllib3`. Use a virtualenv per engine if you need more than one.

Known limits, stated rather than left to be discovered:

- **`--concurrency` works in the Playwright engine only.** Selenium and
  pyppeteer accept the flag — it is part of the family's CLI contract — and
  log that they are ignoring it, fetching one page at a time. Parallel
  fetching is implemented in `playwright_scraper.py`, which is the primary
  engine. This matters only in `--mode job`, the one mode with more than one
  page to fetch.
- **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password, unlike Playwright's `connect_over_cdp` and Puppeteer's
  `browserWSEndpoint`.
- **Selenium's `--proxy-server` cannot authenticate at all.** Credentials are
  stripped and warned about rather than silently ignored.
- **pyppeteer is effectively unmaintained** — its own README points at
  Playwright.

Everything else is identical, and "identical" here means verified rather
than intended: same flags, same exit codes, same run status, same rows.

`scraper_api_client.py` is a fourth path that renders the page on 2Captcha's
infrastructure and returns HTML over plain HTTPS. On this site it is the one
with the least to recommend it, because nothing here needs someone else's
exit.

---

## What the 2Captcha products are for

Four separately-billed products behind one key. On this site none of them
is required, and saying what each actually buys is more useful than a pitch:

- **Proxies** — not for access. For *volume*: `--mode job` can fetch 462
  detail pages, and spreading those across exits is politer than sending
  them all from one address. `--concurrency N` without a pool sends N× the
  traffic from a single IP, and the engines warn about it.
- **Scraping Browser API** — a specific exit **country**, and no local
  Chromium to install. `--cdp-endpoint`. One live connection per profile, so
  `--concurrency > 1` is refused with that reason; use several `pid`s.
- **Fingerprints** — `--fingerprint`. Never combined with `--cdp-endpoint`:
  the remote browser brings its own, and stacking a second creates a
  contradiction rather than better cover.
- **Captcha solving** — see below.

### Captchas: what is actually here

Mercor has **never rendered a challenge** to this scraper. But "no challenge
rendered" is not "no captcha configured", and on this site the distinction
has teeth.

**Mercor runs reCAPTCHA v3 Enterprise on every page.** Sitekey
`6LcUUCgsAAAAAD_LMM5QDj1qUwsfKYDbNKa0v5wO`, `size=invisible`, discovered
through `___grecaptcha_cfg` on the first live run of the Playwright engine.

Two things about it matter:

1. **It is invisible to a capture.** The sitekey appears in no served HTML
   and in none of the eagerly-loaded JS bundles — it arrives in a lazily
   loaded chunk. Grepping a saved page for the site's own captcha config
   finds nothing. Only a real browser reveals it.
2. **It never blocks.** A v3 widget renders no challenge frame; it scores
   the session in the background, and Mercor served all 390 rows alongside
   it. So it must never be paid for on a page that already has content —
   which is what `--solve-captcha when-blocked` (the default) enforces.

This repo **does** implement enterprise reCAPTCHA
(`RecaptchaV2EnterpriseTaskProxyless`), ordinary reCAPTCHA v2/v3, and
Cloudflare Turnstile (`TurnstileTaskProxyless`, with the
`turnstile.render` interception a Managed Challenge requires). It has
simply never needed any of them here. If Mercor's posture changes, the
machinery is already in place.

One more measured non-finding: Mercor is fronted by Cloudflare, and every
page — including both 404s — carries `/cdn-cgi/challenge-platform`. That is
Cloudflare's ordinary bot-management script, **not** a challenge. It was
briefly in this repo's marker set and made a delisted job report exit 3;
counting it on pages known to be good is what caught that.

---

## Configuration

Credentials live in `.env` next to the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps`.

```bash
cp .env.example .env
python3 env_config.py          # prints what was picked up, without secrets
```

Precedence, highest first: **explicit flag → exported env var → `.env` →
default.** A copied `.env.example` reads as *unset* for every credential, so
it is never sent to an API as if it were a key.

Variables: `TWOCAPTCHA_KEY`, `MERCOR_CDP_ENDPOINT`, `MERCOR_PROXY`,
`MERCOR_URL`.

---

## Comparing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json
```

Joins on `sku`. It **refuses** to compare runs that are not both `complete`,
that were taken in different modes, or that come from different populations
— a partial run's un-fetched pages would otherwise read as delisted jobs.

A price difference that comes with a `data_source` difference is reported as
`source_changed` rather than `changed`: that says something about our two
snapshots, not about the site.

---

## Testing

```bash
python3 smoke_test.py          # the offline suite; no network, no engine needed
python3 smoke_test.py -v       # print every check
pytest                         # same checks, via tests/test_smoke.py
```

Fixtures are cut from real captures taken 2026-09-18, and each was verified
to parse **identically** to its untrimmed original before being committed.

CI runs the suite offline on the oldest and newest supported Python, builds
and runs the Docker image, and installs each engine in its own virtualenv to
check the versions users actually get. The **canary** runs a real scrape
daily from a bare GitHub runner with no secrets — which is what keeps this
README's central claim honest. If Mercor ever puts these routes behind a
challenge or a key, the badge goes red the next morning and the claim is
retested without anyone having to remember to.

---

## Licence & contributing

MIT — see [LICENSE](LICENSE). Bug reports and site-change reports are
welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and the issue templates.
Scrape responsibly: respect `robots.txt` (this scraper reads only paths
Mercor's own `robots.txt` allows), keep request rates civil, and do not
republish personal data.
