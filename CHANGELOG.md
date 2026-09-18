# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A **patch** release means fixes; it does not
mean every flag and default is frozen, so a behaviour-changing default can
land in one — and when it does, the entry leads with it so nobody discovers
it from a bill or from a diff.

## [Unreleased]

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
