# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A **patch** release means fixes; it does not
mean every flag and default is frozen, so a behaviour-changing default can
land in one — and when it does, the entry leads with it so nobody discovers
it from a bill or from a diff.

## [Unreleased]

> **Behaviour changes in this section.** `--pages`/`--concurrency` below 1
> and a negative `--delay`, `--retry-delay` or `--proxy-block-retries` are
> now usage errors (exit 2) in every engine. A `MERCOR_URL` from `.env` that
> names a different route from the `--mode` you type is now ignored for that
> run instead of exiting 2, and `.env.example` ships it commented out. CSV
> cells that begin with `=`, `+`, `-`, `@`, a tab or a line break now get a
> leading apostrophe (CSV only; JSON is unchanged; the sidecar records
> `csv_cells_escaped`). Output files are replaced atomically and keep their
> mode.

### Fixed (2026-10-15)

- **A failed write no longer destroys the previous good output.** JSON, CSV
  and `<out>.meta.json` were opened with `"w"`, so a write that failed
  halfway (measured: an unserialisable row) left a 1,042-byte `out.json` as
  96 bytes of invalid JSON. Each file is now written to a temporary file in
  the same directory, flushed, fsynced and renamed over the target; the
  temporary file is removed on error. A new file gets the mode `open()`
  would have given it (`0o666 & ~umask`); an existing file keeps its own.
- **CSV formula cells are neutralised.** Titles, descriptions and company
  names are third-party text; a cell beginning with a formula character is
  prefixed with `'` (after list columns are joined), numbers are left alone,
  and the count goes in the sidecar under any value the caller set.
- **`cp .env.example .env` no longer breaks `--mode job` and `--mode
  careers`.** The example set `MERCOR_URL` to the index, which the URL/mode
  check then treated as if the user had typed `--url`. The engines and
  `scraper_api_client.py` now remember whether `--url` was typed; a typed URL
  that disagrees with `--mode` is still refused. `scraper_api_client.py`
  now also refuses a typed URL for another route and defaults the index and
  careers modes to their own address.
- **The captcha allowance is spent by purchases, not by checks.** The first
  of the two solve call sites counted itself against `SOLVES_PER_PAGE`
  before knowing whether a challenge existed, so on a page that classified
  as blocked the solve the state policy asks for could never run. The count
  now happens at the moment a task is submitted; still at most one purchase
  per page.
- **The credential scan read nothing from some checkouts.** Its skip list
  was matched against the ABSOLUTE path, so a clone under any directory
  named `build`, `venv`, `env`, `.venv` or `__pycache__` scanned 0 files and
  printed "ok". Exclusions are now relative to the repository, a scan of 0
  files fails, and `.env*` files and `.json`/`.csv`/`.toml`/`.cfg`/`.ini`/
  `.sh` files are read too.
- `.gitignore` covers `--dump-html` page files without an `.html` suffix
  (`X.page2`), raw `.html` dumps at the top level and live-run directories
  (`live/`, `out/`, `results/`, `tmp/`, ...).
- `--locale` and `--retries` help, and several comments, described other
  sites' parameters and pages; they now describe Mercor's.
- Comments, docstrings, workflows and the README pointed at numbered
  sections of an instruction file readers cannot open. The reasons are now
  stated in place, and a check fails if such a pointer returns.
- Canary: the careers job no longer requires 80% of roles to publish a
  salary (that is the employer's choice); it now requires every published
  salary figure to be parsed into a rate.

Text and data copied from sibling repos when this one was bootstrapped,
describing those sites as if they were this one.

### Fixed

- **`diff_runs.py` never reported `source_changed`.** Its one-source column
  list named another site's fields (`equity_min`, `company_badges`, ...),
  none of which `JobPosting` has, so the same listing read from the index
  and from its detail page came out as a `changed` row. It now lists this
  site's one-source columns (`domain`, `employment_type`, `rate_currency`,
  `company_website`), and a new check replays the pair in
  `sample_output.json`. Its usage example and its output line also
  described another site's flags and "profile" rows.
- `selenium_scraper.py` and `puppeteer_scraper.py`: a post-run block keyed
  on `--mode role`, which this repo does not have, so the `/explore`
  coverage note never printed. It now runs on `--mode listings`, as it does
  in `playwright_scraper.py`. The dead thin-page constant went with it.
- `--pages` help in all three engines described another site's
  pagination (`--mode role`, "page 48 of a 47-page listing"). It now says
  what this site does: only `--mode job` paginates, one job per page.
- `scraper_api_client.py --help`: the description said `--cdp-url` was
  required because Mercor refuses datacentre exits (false: every route was
  served to one), `--mode` claimed a default of "profile", `--url` named
  another site's routes, and `--category` claimed to tag rows.
- Issue templates: the bug report and site-change templates were written
  for another site (its bot vendor, URLs and columns). Rewritten from this
  README. Three links to a `TROUBLESHOOTING.md` that does not exist, one of
  them in `--dump-html` help, now point at the README's "Traps that look
  like bugs".
- `CONTRIBUTING.md`: removed four bullets describing another site's routes,
  pagination and pay strings.
- `.gitignore` / `.dockerignore`: another site's output prefix replaced with
  this repo's default, `mercor_jobs` (and `mercor_jobs_scraperapi`).
- Stale comments naming donor behaviour as this repo's, in the engines,
  `captcha_solver.py` (a pointer to a section that does not exist) and
  `smoke_test.py` (now names the sibling it happened in).
- **The Scraper API path sent `waitFor` in a form the live API rejects,
  and read the wrong field as the target's status.** Measured 2026-09-23
  against `scraper.2captcha.com/tasks/sync`: `waitFor` sent as a
  JSON-encoded string (what this client built for every `--wait-*` flag)
  is answered HTTP 422 "params.waitFor must be an object" and is still
  billed ($0.0005); the same request with an object gets HTTP 200. It is
  now an object. And the response's `status` is the API's own verdict
  ("success"), not the target site's HTTP code, which is `http_code` —
  so a target 403 or 503 never reached the page classifier. The target
  status is now read from `http_code` (falling back to `status` only if
  that is an integer). Pinned by an offline check that drives the real
  client with `requests.post` stubbed; verified by control (red with the
  old client).

- `SECURITY.md` said this project has no releases or version tags; it has
  both. "Supported versions" now names the latest release and `main`.

## [0.1.0] — 2026-09-18

First release. Scrapes [mercor.com](https://mercor.com) with Playwright,
Selenium, Puppeteer, or the 2Captcha Scraping Browser API over CDP.

### Added

- **Three modes**, because the site publishes three different things:
  - `--mode listings` (default) — `work.mercor.com/explore`, the marketplace
    index. **390 contract roles in one response** with rate ranges, the
    site's own `domain` category, work/residence eligibility and slot
    counters.
  - `--mode job` — `work.mercor.com/jobs/{id}/{slug}`. One listing per page,
    adding the company website, company description, spot counts and a
    **currency** the index does not publish. This is the paginated mode: a
    "page" is one job, and the addresses come from a real enumeration rather
    than a guessed `?page=N` convention.
  - `--mode careers` — `www.mercor.com/careers`, Mercor's own salaried roles
    via the Ashby ATS. A different population with its own id space, so
    `diff_runs.py` refuses to compare it against the other two.
- `--domain`, a client-side filter on the site's own category, documented as
  client-side because Mercor's index accepts no query parameters at all.
- One `JobPosting` row shape across all three modes, with the family's
  `source, scraped_at, url, sku, title` prefix byte-identical and in order.
- All three engines plus `scraper_api_client.py`, agreeing on exit codes,
  run status and rows — verified live across all three modes.
- An **ungated** daily canary: a real scrape from a bare GitHub runner with
  no secrets, expected green. See below for why that is the point.

### Measured on 2026-09-18, from a Hetzner datacentre address (AS24940, Helsinki)

Every number in the README and in the code comments is dated and tied to
this run. The ones that shaped the design:

- **Nothing is gated.** Eighteen candidate bot-challenge markers counted
  across seven captures — all zero. Identical 200s from `curl`, from
  `python-requests` and from an **empty** User-Agent. Twelve rapid detail
  fetches, all 200 at ~0.4 s. No key, no proxy and no account is needed for
  any route this scraper reads.
- **The site's own JSON-LD index is incomplete.** `/explore` ships an
  `ItemList` of 326 URLs beside a React Query cache of **390** full records;
  the 64 it omits are all 55 `evergreen` listings plus 9 standard. This
  scraper reads the cache. A parser anchored on the JSON-LD — the obvious
  choice — would silently lose 16% of the catalogue.
- **No single route enumerates the catalogue.** sitemap 447, index 390,
  shared 375, **union 462**; neither contains the other, and all three
  sitemap-only listings checked were live. `--mode job` fetches the union,
  and a `--mode listings` run records in its sidecar that it holds 390 of
  them.
- **`/explore` has no second page.** `?page=2`, `?limit=`, `?offset=` and
  `?domain=` each returned a byte-identical payload, so `page_url()` returns
  None by design rather than manufacturing an address the site does not
  honour.
- **The marketplace host runs invisible reCAPTCHA Enterprise; the corporate
  host runs none.** `work.mercor.com` loads `enterprise.js` and an anchor
  iframe (`size=invisible`, sitekey
  `6LcUUCgsAAAAAD_LMM5QDj1qUwsfKYDbNKa0v5wO`) on the index, on detail pages
  and on its 404s; `www.mercor.com` and `/careers` load zero captcha
  requests and zero iframes. Its sitekey is in no served HTML and in none of
  the eagerly-loaded JS bundles, so a static grep for the site's own captcha
  config finds nothing — only a real browser reveals it. No challenge frame
  was rendered on any route on any run, so it never blocks and must never be
  paid for, which `--solve-captcha when-blocked` (the default) enforces. The
  variant is inferred rather than confirmed: invisible with no challenge
  frame reads as v3, but an unchallenging v2-invisible looks identical.
- **Pay is not all hourly**: `hourly` 347, `per-task` 31, `one-time` 9,
  `yearly` 3 across the 390. And `commitment` is a different axis from
  `rate_period` — a task-based commitment can still be paid hourly, and 31
  are.
- **Currency is read, never defaulted.** Null on every index row (the route
  publishes none), USD on 40 of 40 sampled detail pages, and USD **or GBP**
  on the careers route.
- **`/explore`'s ordering is stable** — three fetches returned the identical
  390 ids in the identical order — while the slot counters are live and
  drift within minutes.

### Notes for anyone extending this

Four traps cost real time here and are pinned by tests so they stay fixed:

- **`__NEXT_DATA__` carries a `nonce`** on every page of this site. A regex
  written without one matches nothing, and the failure reads like the site
  having stopped server-rendering rather than like a typo.
- **`/cdn-cgi/challenge-platform` is on every page, including both 404s.**
  It is Cloudflare's ordinary bot-management script, not a challenge. It was
  briefly in the marker set and made a delisted job report exit 3 (blocked)
  instead of `not_found`. Count every candidate marker on a page you know is
  good before adding it.
- **Hydration rewrites every job anchor**, from `/jobs/list_…` to
  `/explore?listingId=…`. A readiness selector verified against a saved
  capture can match zero elements in a live browser — silently, because the
  rows come from `__NEXT_DATA__` either way. What it costs is a full timeout
  per page, and a captcha gate that concludes "no content here" and tries to
  pay.
- **The positive-asset heuristic is inverted here.** "Was this built out of
  the site's own assets?" works on sibling sites; on Mercor the 404 is the
  same Next.js app and references `/_next/static` *more* than a real page
  does. `detect_page_state` leads with the presence of `__NEXT_DATA__`
  instead, which the 404 carries none of.
- **The HTTP status must reach the classifier, and here it is nearly the
  only signal.** All three engines discarded what the navigation returned,
  so a withdrawn listing classified as `empty` — a claim that the site
  served an answer with nothing in it, about a job that is simply gone.
  `--mode job` meets this routinely: the enumeration is built before any of
  its 462 addresses is fetched. There is no body to fall back on — the live
  404 carries `__NEXT_DATA__` like any other page, its visible text is the
  empty string, and "404" appears *more* on good pages (7 and 17) than on
  the 404s (1 and 2). Playwright and pyppeteer now bind `goto()`'s response;
  Selenium reads Chrome's performance log, because `driver.get()` returns
  None and WebDriver exposes no status at all. The cross-engine fallback is
  that Mercor bounces a dead address to `/login?redirect=…`, which is
  readable from the final URL alone.
- **The mirrors never told the classifier which mode they were in**, so a
  `--mode job` detail page was judged as if it were a listing. It worked by
  luck, because `empty` still parses.
- **`diff_runs.TRACKED_FIELDS` must name columns that exist.** Ported
  verbatim it named 25 fields of which this row class has four, so the diff
  compared nothing: a listing whose rate went 100 → 999 with its status
  changed to `closed` reported *0 changed* and exit 0. A price monitor that
  cannot see a price change is worse than none, because it reports success.
  No fixture and no live run would have shown it — two runs of identical
  data report "0 changed" whether the comparison works or not — so it is
  pinned against the dataclass instead. The family has now paid for this
  twice; the first was a supermarket scraper whose price diff tracked a
  blogging platform's "claps".

[Unreleased]: https://github.com/2scraper/mercor-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/2scraper/mercor-scraper/releases/tag/v0.1.0
