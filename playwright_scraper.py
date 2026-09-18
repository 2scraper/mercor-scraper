"""
mercor-scraper — Playwright edition (primary engine)

Scrapes mercor.com: the marketplace index, one marketplace listing, and
Mercor's own corporate openings.

    --mode listings  work.mercor.com/explore
                     The marketplace index. 390 contract roles in ONE
                     response — rates, domains, eligibility, slot counts.
    --mode job       work.mercor.com/jobs/{id}/{slug}
                     One listing, with the company website, the company
                     description, the spot counts and a currency that the
                     index does not publish. Enumerated from the sitemap.
    --mode careers   www.mercor.com/careers
                     Mercor's own salaried roles, via Ashby. A different
                     population with its own id space.

What is different about Mercor
==============================

* **Nothing is gated.** Measured 2026-09-18 from a Hetzner datacentre
  address (AS24940, Helsinki): eighteen candidate bot-challenge markers
  counted across seven captures, every one zero; identical 200s from
  `curl`, from `python-requests` and from an EMPTY User-Agent; twelve rapid
  sequential detail fetches all 200 at ~0.4s each. No captcha, no proxy and
  no key is needed for any route this engine reads. The paid products buy
  volume and a specific country here, not access — see the README.

* **The rows are not in the DOM, and the obvious structured source is the
  wrong one.** `/explore` carries an `ItemList` JSON-LD of 326 URLs and a
  React Query cache of 390 complete records. The JSON-LD omits every one of
  the 55 `evergreen` listings. `product_parser` reads the cache and says
  why.

* **`/explore` has no second page.** `?page=2`, `?limit=`, `?offset=` and
  `?domain=` were each tried and each returned a byte-identical payload.
  `page_url()` returns None on purpose rather than manufacturing an address
  the site does not honour.

* **In `--mode job` a "page" is one job.** The addresses are read from
  `/sitemap.xml` and the `/explore` payload before the run starts — real
  independent URLs, not a convention guessed at — so the concurrency
  machinery and the sidecar's per-page failure list both mean what they say.

* **No single route enumerates the catalogue.** sitemap 447, explore 390,
  shared 375, union 462. Neither contains the other, and the 72
  sitemap-only listings are live jobs. `--mode job` fetches the union.

Usage
-----
    python3 playwright_scraper.py
    python3 playwright_scraper.py --mode listings --format csv --out mercor
    python3 playwright_scraper.py --mode careers
    python3 playwright_scraper.py --mode job --pages 50 --concurrency 4
    python3 playwright_scraper.py --mode job --url "https://work.mercor.com/jobs/list_AAABoGhhxWgF7ygE2LZOgIy3/b-2-b-sales-expert-3-yoe-us-only"

This site answered every one of those from a bare datacentre IP with no
credentials on 2026-09-18. `--proxy` and `--cdp-endpoint` work and are not
required.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS, detect_turnstile,
                            wait_for_turnstile, TURNSTILE_INTERCEPT_JS,
                            TURNSTILE_INJECT_JS, turnstile_task_for)
from product_parser import (MODES, DEFAULT_MODE, category_from_url,
                            detect_bot_challenge, is_supported_url,
                            mode_for_url, page_url, parse_detail,
                            parse_listing_page, parse_careers_page,
                            references_own_assets, site_turnstile_sitekey,
                            enumerate_job_urls, listing_meta, sku_from_url,
                            route_of, EXPLORE_URL, CAREERS_URL, SITEMAP_URL)
from output_writer import (dedupe_by_key, finish_run, EXIT_API_ERROR,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The page_flow state this page came back as ("content", "blocked",
    # "challenge", "empty", "unknown"). Carried so the caller can tell an
    # EMPTY page — a /p/<slug> hub, a no-match query, or one page past the
    # end of a listing — from a page that failed. Both produce zero rows and
    # they mean opposite things.
    state: Optional[str] = None
    # What this response said about itself. Mercor publishes no catalogue
    # total on any route, so unlike a site that states `totalResults` these
    # are counts of what ARRIVED rather than claims about the catalogue —
    # and saying so is the difference between "complete" and "exhaustive"
    # (CLAUDE.md §21).
    #
    # `total_available` holds the number of records in the payload.
    # `urls_in_itemlist` holds the number the same response's ItemList
    # JSON-LD indexes, which was SMALLER on 2026-09-18 (326 against 390).
    # Carrying both means every run records the gap instead of a reader
    # inheriting the figure from a comment (§13).
    total_available: Optional[int] = None
    total_companies: Optional[int] = None
    pages_available: Optional[int] = None
    urls_in_itemlist: Optional[int] = None
    # The page number the SERVER answered with. Always 1 here, because
    # `/explore` has exactly one page; kept so the three engines carry the
    # same outcome shape and so a site that starts paginating is detected
    # rather than assumed away.
    echoed_page: Optional[int] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The share of rows that must carry the columns Mercor fills on every
# record, below which the read has broken rather than the data being
# unusual.
#
# There is no price on this site, so there is no price coverage. What stands
# in its place is the handful of fields that were populated on 390 of 390
# listing records and on all 7 detail captures on 2026-09-18 — the title,
# the URL, the id and the pay range.
#
# Deliberately NOT in this check, each with the measurement that keeps it
# out — every one of these would fire on healthy data:
#   company_name            99 of 390. Mercor runs most of its marketplace
#                           for unnamed clients; a null here is the normal
#                           case and `company_brand_visible` says so.
#   hours_per_week         294 of 390
#   eligible_locations     134 of 390
#   rate_currency            0 of 390 on the index, which publishes none at
#                           all; USD on 40 of 40 sampled DETAIL pages.
#   domain                   0 on a detail row — that route omits it.
CORE_FIELD_FLOOR = 99
CORE_FIELDS = ("title", "url", "sku", "rate_min")

# ...and the set for --mode careers, which is different rather than shorter.
# An Ashby record always states a department and a published date; 106 of
# 108 state a salary, the two exceptions being roles published with no
# compensation block at all, so `rate_min` cannot carry a 99% floor here.
CORE_FIELDS_CAREERS = ("title", "url", "sku", "department")

# --mode job. A detail page publishes no `domain` and names a company on
# only some listings, so the floor is the four fields every one of the 7
# detail captures carried.
CORE_FIELDS_JOB = ("title", "url", "sku", "rate_min")


def _core_fields(mode: str):
    if mode == "job":
        return CORE_FIELDS_JOB
    if mode == "careers":
        return CORE_FIELDS_CAREERS
    return CORE_FIELDS

# A landing page holding less than this share of the rows a full page
# carries is reported as thin.
#
# Set LOW on purpose, and the reason is the site's own arithmetic: a landing
# page paginates over COMPANIES — twenty per page — while the rows are JOBS,
# and how many jobs twenty companies have is not fixed. Measured across the
# captures: 32, 37, 43, 45 and 5 rows per page, the 5 being the last page of
# a 47-page listing. A share computed against `page_size` would call a
# perfectly normal page thin, so this is compared against the ROW count of
# page 1 rather than against the site's page size.
THIN_PAGE_SHARE = 0.35


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    # Named OPERATIONS rather than JavaScript, and that is the point of the
    # split. Selenium's execute_script takes a function BODY with an explicit
    # `return` while Playwright and pyppeteer take `() => expr`, so a shared
    # module handing JS across this boundary would quietly acquire one
    # driver's dialect.
    #
    # There is no scroll primitive here, and its absence is measured rather
    # than forgotten: Mercor serialises its whole result set into
    # `__NEXT_DATA__` in the first response. All 390 listing records are in
    # the document before any scrolling could happen, so a scroll would be
    # ceremony that looks load-bearing (CLAUDE.md §4).
    #
    # The grid is in fact VIRTUALISED — the live DOM holds 16 job anchors
    # for 390 records and scrolling would paint more of them — which makes
    # the absence worth stating twice: scrolling would add DOM nodes and
    # not one row, because no row is read from the DOM.
    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
    }


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args) -> int:
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None, mode: str = "role") -> str:
    """`mode` is threaded through because the two page kinds are read by
    different mechanisms: a job page has no `__NEXT_DATA__` at all, so
    classifying one as a listing would call a perfectly good page a
    parse_error."""
    return page_flow.classify(html, status, page.url, mode)

# Every readiness constant and every state policy lives in page_flow.py, with
# its measurement beside it. Nothing about WHAT to do with a page is
# duplicated here — this file only knows HOW to ask Playwright.


def _fetch_text(session, url: str, timeout_ms: int = 60000) -> str:
    """Navigate and return whatever the parser should be given for this URL."""
    session.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return _snapshot(session.page, url) or ""


def _fetch_page_source(session, url: str, timeout_ms: int = 60000) -> str:
    """Navigate and return the serialised document.

    `content()` rather than `innerText`, and that matters for the sitemap.
    Chromium renders `text/xml` through its own XML viewer, and it was
    checked rather than assumed which of the two survives that: on
    `/sitemap.xml` `content()` keeps all 463 `<loc>` elements intact, so the
    parser's regex reads it unchanged. `innerText` gives the viewer's
    rendering, which happens to contain the URLs too but only by accident of
    how the viewer lays them out.

    Used for the two enumeration fetches. The per-page fetches go through
    `_snapshot`, which is the path with the retry, classification and solve
    machinery around it.
    """
    session.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return session.page.content() or ""


def _target_url(args) -> str:
    """The address this run actually fetches.

    Every mode has a correct default, so a bare `python3
    playwright_scraper.py` is a working command rather than a usage error.
    There is nothing to build from flags the way a sibling repo builds a
    landing URL from `--role`: Mercor's index takes no parameters at all
    (every one tried was ignored — see `page_url`), so the only choice a
    user has is which of three routes to read, and `--mode` is that choice.

    In `--mode job` the default is the enumeration rather than a single
    address; `_plan_page_urls` turns it into one URL per job. A `--url`
    naming one job is honoured and fetches exactly that job.
    """
    if args.url:
        return args.url
    if args.mode == "careers":
        return CAREERS_URL
    if args.mode == "job":
        # Page 1 of a job run is the first job in the enumeration. The
        # enumeration itself is fetched in `_plan_page_urls`, which is the
        # one place that needs the sitemap.
        return EXPLORE_URL
    return EXPLORE_URL


def _is_single_job_url(args) -> bool:
    """Whether `--url` names one specific job, rather than a route to walk.

    A `--mode job` run given a job URL fetches exactly that job and nothing
    else — no sitemap, no enumeration, one row. That is the "look this one
    up" case and it should cost one request.
    """
    return bool(args.url) and route_of(args.url) == "job"


def _enumerate_jobs(session, args) -> List[str]:
    """Every job URL this run could fetch, in a stable order.

    The union of two sources, because **neither contains the other**.
    Measured 2026-09-18: `/sitemap.xml` holds 447 job URLs, the `/explore`
    payload holds 390, they share 375, and the union is 462. Three of the
    72 sitemap-only listings were opened by hand and all three answered 200
    with `status: active` — they are live jobs that the index simply does
    not show. A run that enumerated from either source alone would miss
    real work and report a complete run while doing it.

    Both fetches go through the browser SESSION rather than through a
    separate HTTP client, so the enumeration leaves from the same exit as
    the pages it plans — a run whose sitemap came from one address and
    whose jobs came from another is two runs wearing one sidecar.

    Chromium renders XML through its own viewer, and that was checked
    rather than assumed: `page.content()` on the sitemap keeps all 463
    `<loc>` elements intact, so `sitemap_job_urls` reads it unchanged.

    Sitemap order leads because it is Mercor's own publication order and is
    stable between runs, which keeps `--pages 20` fetching the SAME twenty
    jobs each night rather than a different twenty — otherwise a nightly
    diff reports churn that is an artefact of the ordering.
    """
    sitemap_xml = ""
    try:
        sitemap_xml = _fetch_page_source(session, SITEMAP_URL)
    except Exception as exc:  # noqa: BLE001 - any driver error is the same here
        logger.warning("Could not read %s (%s). Falling back to the "
                       "/explore payload alone, which held 390 of the 462 "
                       "listings measured on 2026-09-18 — the run will be "
                       "short by whatever the sitemap would have added.",
                       SITEMAP_URL, exc)
    explore_html = ""
    try:
        explore_html = _fetch_page_source(session, EXPLORE_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s (%s). Falling back to the sitemap "
                       "alone.", EXPLORE_URL, exc)

    urls = enumerate_job_urls(sitemap_xml, explore_html)
    if not urls:
        logger.error("Neither %s nor %s yielded a single job URL. There is "
                     "nothing to enumerate, so this run has no work to do.",
                     SITEMAP_URL, EXPLORE_URL)
        return []
    from product_parser import sitemap_job_urls as _sm, listings_from_html as _lh
    n_sitemap = len(_sm(sitemap_xml))
    n_explore = len(_lh(explore_html))
    logger.info("Enumerated %d job(s): %d from the sitemap, %d in the "
                "/explore payload, %d in both. Neither source is a superset "
                "of the other on this site.",
                len(urls), n_sitemap, n_explore,
                n_sitemap + n_explore - len(urls))
    return urls


def _plan_page_urls(args, page_one_url: str,
                    pages_available: Optional[int],
                    job_urls: Optional[List[str]] = None) -> List[str]:
    """URLs for pages 2..N, decided once before any of them is fetched.

    On most sites in this family this function has to hedge: it compares the
    site's own next-link against what the URL convention would build, and
    falls back to chaining link-to-link when the two disagree, because a
    constructed URL the site does not honour produces a complete-looking run
    holding page 1.

    Mercor needs no hedging, and for the opposite reason to most: it has
    nothing to hedge ABOUT.

      * `/explore` and `/careers` have exactly one page each. Measured
        2026-09-18, `?page=2`, `?limit=10`, `?offset=100` and
        `?domain=Finance` on `/explore` each returned a byte-identical
        payload of the same 390 records. So `page_url()` returns None by
        design and this plans nothing — asking for more pages is answered
        with a log line rather than with duplicate rows and a false
        `complete`.

      * `--mode job` plans from a REAL LIST. `job_urls` is the enumeration
        (see `_enumerate_jobs`), so every planned page is an address the
        site published rather than one this code guessed, and walking off
        the end is impossible: the plan is capped by the length of the
        list, which is the only "pages available" figure this site offers.
    """
    if args.mode == "job":
        if not job_urls:
            # `--url` named one job. One page is the whole run.
            return []
        wanted = page_flow.pages_to_plan(args.pages, len(job_urls))
        if wanted < args.pages:
            logger.info("The enumeration holds %d job(s) and %d page(s) were "
                        "asked for; planning %d. There is no more catalogue "
                        "to walk to — this is every job Mercor publishes on "
                        "the sitemap and in the index.",
                        len(job_urls), args.pages, wanted)
        return list(job_urls[1:wanted])

    if not page_flow.pagination_is_addressable(page_one_url):
        if args.pages > 1:
            logger.info(
                "%s is served at ONE address and holds its whole result set "
                "in a single response, so %d page(s) were asked for and 1 "
                "will be fetched. Every pagination parameter tried on it "
                "(?page, ?limit, ?offset, ?domain) was ignored and returned "
                "an identical payload, so planning a page 2 would re-collect "
                "page 1 and report a complete multi-page run. Use --mode job "
                "if you want many pages: there a page is one job.",
                page_one_url, args.pages)
        return []

    wanted = page_flow.pages_to_plan(args.pages, pages_available)
    planned = []
    for n in range(2, wanted + 1):
        built = page_url(page_one_url, n)
        if built is None:
            break
        planned.append(built)
    return planned


_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    # Records the arguments Cloudflare passes to `turnstile.render()`.
    #
    # This has to be installed HERE, on the context, and not on a page after
    # navigation: Cloudflare calls `turnstile.render(container, params)` once
    # and keeps nothing, so `sitekey`, `action`, `cData` and `chlPageData`
    # exist only inside that call. A static read of a challenge page — any
    # static read, however careful — cannot produce a solvable task
    # (CLAUDE.md §19). It costs nothing on a page that never renders one.
    context.add_init_script(TURNSTILE_INTERCEPT_JS)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # the endpoint is a URL with the password in it. Unmasked, that
        # password lands in the terminal, in CI output and in any log the run
        # is piped to — which is the one thing this project promises does not
        # happen ("credentials never reach argv or logs"). The message is
        # rewritten with the credentials masked and the host and port kept,
        # because WHICH endpoint failed is the useful half and is not the
        # secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # What it covers here is Cloudflare's managed challenge. Mercor has
    # never answered this scraper with one — it is fronted by Cloudflare but
    # served every request measured on 2026-09-18 — so this path is
    # insurance rather than a route in use. That challenge publishes no
    # sitekey in its markup — Cloudflare calls `turnstile.render` once and
    # keeps nothing — so the LOCAL path needs the init-script hook that
    # records those arguments (see `handle_captcha_if_present`), while this
    # remote path is handled inside the browser and needs none of it.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a Playwright connection
# error repeats the endpoint five times (the message plus a four-line call
# log), so a masker that handled only the first occurrence would print the
# password four times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. Mercor does not geo-redirect — no exit country tried changed the
    markup — but `mercor.com` redirects to `www.mercor.com`, `/jobs`
    redirects across hosts to `work.mercor.com/explore`, and the index
    hydrates immediately after load (rewriting every job anchor as it
    goes), so a snapshot taken right after goto() can land exactly on a
    swap.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (a URL canonicalisation?) — "
                        "retrying content() in %dms (%d/%d).",
                        pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def _is_endpoint(url: str) -> bool:
    """Whether this address answers with raw JSON rather than a document.

    Always False on Mercor: every route this scraper reads is an HTML page
    with its payload serialised inside it. There IS an ungated JSON-ish
    route on this site — `/sitemap.xml` — but it is read by
    `_fetch_page_source` during enumeration rather than through the
    per-page snapshot path, so it never reaches here.

    The function is kept because `_snapshot` branches on it and a sibling
    engine's twin must branch the same way; removing it would make the
    three engines' snapshot paths differ for no reason.
    """
    return False


def _snapshot(page, url: str) -> Optional[str]:
    """What the parser is given for this address.

    Two shapes, because the two addresses answer with two things and
    Chromium does not hand them over the same way. A PAGE is read with
    `content()`. The ENDPOINT answers with JSON, which Chromium wraps in its
    own JSON-viewer markup — so `content()` there returns the viewer's HTML
    and the payload would be unreachable. `document.body.innerText` gives
    back exactly what the server sent.

    Getting this wrong is silent: the viewer markup parses as "not a listing
    payload", which reads as an empty result rather than as a bug.
    """
    if _is_endpoint(url):
        try:
            return page.evaluate("() => document.body.innerText") or ""
        except (PWError, PWTimeout) as e:
            logger.warning("Could not read the endpoint response: %s", e)
            return None
    return _content_when_settled(page)


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. The
    static-HTML and runtime reCAPTCHA detectors are run and reconciled
    against each other rather than short-circuited, because they can disagree
    about the variant and the parameters for one are rejected for the other.

    WHAT THIS SITE ACTUALLY PUTS IN FRONT OF A RUN, measured 2026-09-18,
    because CLAUDE.md §19 is explicit that a sentence about what a solver
    can do is the most expensive thing this family can get wrong:

    * Cloudflare's **managed challenge**. Mercor sits behind Cloudflare and
      has never answered this scraper with one — every route measured on
      2026-09-18 was served, from a bare datacentre address, to `curl` and
      to an empty User-Agent alike. The machinery is here for the day that
      changes. Such a challenge publishes no sitekey, because Cloudflare
      calls `turnstile.render()` once and keeps nothing. 2Captcha solves it
      with `TurnstileTaskProxyless`, but the task needs `sitekey`, `action`,
      `cData` and `chlPageData`, and the only way to get them is to hook
      `turnstile.render` before any page script runs. That hook is
      `TURNSTILE_INTERCEPT_JS`, installed on the context below.

    * The site's **own** reCAPTCHA, which is a different thing and IS
      configured — on every page, invisibly. Found on the first live run of
      this engine: reCAPTCHA **v3 Enterprise**, sitekey
      `6LcUUCgsAAAAAD_LMM5QDj1qUwsfKYDbNKa0v5wO`, `size=invisible`,
      discovered through `___grecaptcha_cfg` at runtime.

      Two things about it matter, and the second is the expensive one:

      It is invisible to a CAPTURE. The sitekey appears in no served HTML
      and in none of the eagerly-loaded JS chunks — it arrives in a lazily
      loaded bundle — so CLAUDE.md §18's "grep a good page for the site's
      own captcha config" finds nothing on this site. Only a real browser
      reveals it. That is a new shape of §18's lesson and it is why this
      block says what it does rather than "no captcha configured".

      And it never blocks. A v3 widget renders no challenge frame at all
      (§19); it scores the session in the background and Mercor served all
      390 rows alongside it. So it must never be paid for on a page that
      has content, which is exactly what the `already_rendered` gate below
      is for — see the note there about what a wrong readiness selector
      cost here.

    Neither is "unsolvable" — that word belongs to a PAGE with no widget on
    it, not to a vendor. What is true is narrower and is what the README
    says: on the routes this scraper reads, a residential exit removes the
    challenge entirely, so most runs never reach this code.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)

    if not challenge:
        # No reCAPTCHA. Turnstile is the other thing 2captcha solves, and on
        # this site it is the LIKELY one — Cloudflare is what stands in front
        # of a scored address here. The RUNTIME reading is the one that
        # matters: a Cloudflare challenge page publishes no sitekey in its
        # markup, so only the interception installed at context creation can
        # produce a solvable challenge. The static read is the fallback for
        # the site's OWN widget, whose sitekey IS published.
        # The RUNTIME reading is unconditional: it only returns something
        # when Cloudflare actually called `turnstile.render`, which happens
        # on a challenge page and nowhere else.
        challenge = wait_for_turnstile(lambda js: page.evaluate(js),
                                       lambda s: page.wait_for_timeout(s * 1000),
                                       page_url=page.url)
        # The STATIC reading is gated on the content not being there.
        # Mercor ships no Turnstile markers in its markup, so unlike a
        # sibling site this gate is not load-bearing against a false
        # positive here — it is kept so all three engines gate identically.
        #
        # What IS load-bearing on this site is the `already_rendered` check
        # below, and it was earned. Mercor's invisible reCAPTCHA v3 is
        # detected at runtime on every page. On the first live run of this
        # engine the readiness selector matched the SERVED markup and not
        # the hydrated DOM (hydration rewrites `/jobs/list_…` anchors to
        # `/explore?listingId=…`), so `already_rendered` read 0 on a page
        # holding all 390 rows and the engine went to "attempting to
        # solve". With an API key set that is a charge per page, for a
        # score-based widget that was never stopping anything.
        #
        # CLAUDE.md §18 states the rule for the PARSER's marker set; this is
        # the same rule applied to the SOLVER's — and the reminder that a
        # gate is only as good as the selector it counts with.
        if challenge is None and already_rendered <= MIN_CARD_MATCHES:
            challenge = detect_turnstile(html, page.url)
        if challenge and not challenge.sitekey:
            published = site_turnstile_sitekey(html)
            if published:
                # The site's own widget, which does publish its key. Better
                # than refusing: this is a solvable challenge that the
                # interception simply did not see render.
                challenge.sitekey = published
                logger.info("No sitekey was intercepted, but the page "
                            "publishes one in its own config — using it.")
            else:
                # Raising a task without a sitekey buys a rejected token and
                # an ERROR_CAPTCHA_UNSOLVABLE bill (CLAUDE.md §19). Say so
                # instead.
                logger.warning(
                    "A Cloudflare Turnstile is on this page but no sitekey "
                    "was captured, so no task can be built and none will be "
                    "paid for. That means the widget rendered before this "
                    "run's interception script was installed — which should "
                    "not happen on a page this engine navigated to, and does "
                    "happen if the browser was attached to mid-flight.")
                return False
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    if getattr(challenge, "is_turnstile", False):
        # Turnstile hands its token back through `cf-turnstile-response` and
        # the page's own callback, not through `g-recaptcha-response` and
        # grecaptcha's client registry.
        called_back = page.evaluate(TURNSTILE_INJECT_JS, token)
        logger.info("Turnstile token injected%s.",
                    " and handed to the page's callback" if called_back
                    else " (no callback was captured — relying on the form "
                         "field)")
        if getattr(challenge, "solved_user_agent", None):
            # Logged so it can be RULED OUT rather than guessed at. Measured
            # on a sibling site, a token minted under a mismatched UA was
            # accepted anyway — so this is evidence, not a known cause.
            logger.info("2captcha solved it against user agent %r. Cloudflare "
                        "checks that on a challenge page, so a mismatch here "
                        "is worth ruling out if a paid token is refused.",
                        challenge.solved_user_agent[:60] + "…")
    else:
        page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1):
    """(rows, listing) for this mode. `listing` is None in --mode job.

    `parse_detail` returns a single row or None; wrapping it here keeps
    every caller downstream — dedupe, merge, coverage logging, the writers —
    working on one shape instead of branching on the mode again.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it, a row
    from page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output. `smoke_test.py` asserts page+position
    is unique across a multi-page run for exactly that reason.
    """
    if args.mode == "job":
        row = parse_detail(html, url=url)
        return ([row] if row is not None else []), None
    if args.mode == "careers":
        # Mercor's own openings. One page, no pagination, and no listing
        # arithmetic to report — the payload is the whole set.
        return parse_careers_page(html, url=url), None
    listing = parse_listing_page(html, url=url, page=page_num)
    return listing.rows, listing


def _solve_budget(args, spent: int):
    """Whether another solve may be bought for this page, and the reason.

    `page_flow.SOLVES_PER_PAGE` says at most one purchase per page, and that
    is a MONEY limit rather than a style rule. It was not being enforced:
    `handle_captcha_if_present` is called twice per attempt — once before the
    page is classified, so a challenge is cleared before anything is judged,
    and once after, for the state that says the page really is gated — and
    only the SECOND call was counted.

    INHERITED EVIDENCE, from a SIBLING repo and not from Mercor — labelled
    because §13 says a number you inherited is not a number you measured,
    and this one cannot be reproduced here: Mercor has never rendered a
    challenge to this scraper, so no page of this site has ever bought a
    solve at all.

    On that sibling, from a datacenter address that meets a real Cloudflare
    challenge on every fetch (2026-09-17): ONE page bought THREE Turnstile
    solves — two from the uncounted call across two block attempts, one from
    the counted one — and every token was refused. The cap read as enforced
    and was not (CLAUDE.md §17: a policy constant nothing reads is the same
    defect as dead code).

    The fix is carried here anyway. A budget that is never exercised is
    still the difference between a bill and no bill on the day it is.

    Both call sites now go through here.
    """
    return spent < page_flow.SOLVES_PER_PAGE


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 403 refusal, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried.
    #
    # With a pool, each retry moves to a DIFFERENT exit and the budget is the
    # user's `--proxy-block-retries`. WITHOUT one — the ordinary case here,
    # because `--cdp-endpoint` brings its own exit — the retry re-fetches
    # through the same access path, and that is worth doing on this site
    # rather than giving up: a Scraping Browser profile was measured refusing
    # two requests and serving the third. Zero was the family default and it
    # made the first live run of this engine abandon page 1 on its first
    # block without retrying once.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        # Counted, because it can BUY. See _solve_budget.
        if _solve_budget(args, solves_bought):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                # A solve navigated the page. Give the destination a moment
                # before judging what came back.
                session.page.wait_for_timeout(1000)
        elif solves_bought:
            logger.info("Not solving again on page %d: %d purchase(s) already "
                        "made for it and SOLVES_PER_PAGE is %d. A challenge "
                        "that survives a paid token is not one this run can "
                        "pass.", page_num, solves_bought,
                        page_flow.SOLVES_PER_PAGE)

        html = _snapshot(session.page, url) or ""
        state = _classify(session.page, html, mode=args.mode)

        # Mercor server-renders its payload into `__NEXT_DATA__`, so a page
        # is parseable in the FIRST response and there is nothing to wait
        # for on a healthy one. Measured 2026-09-18 with no pause at all
        # after `wait_until="domcontentloaded"`: /explore gave all 390 rows,
        # /careers gave all 108, and a job page parsed in full. That is why
        # this engine has no scroll step and no readiness pause on the happy
        # path — either would be ceremony that looks load-bearing, and on a
        # virtualised grid a scroll would add DOM nodes and not one row.
        #
        # The wait below is therefore only for the state that says Mercor
        # served SOMETHING that is not the payload. That is the one case
        # where waiting can still help, and it is bounded.
        if state == "unknown":
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is something Mercor served (%d bytes, "
                        "its own assets referenced %d time(s)) but carries no "
                        "listing payload — waiting up to %.0fs rather than "
                        "spending a retry.", page_num, len(html),
                        references_own_assets(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                _ready_selector(args), _min_matches(args), wait_timeout,
                session.page.wait_for_timeout)
            if found < _min_matches(args):
                logger.info("Still nothing after %.0fs (%d match(es) for %s).",
                            wait_timeout / 1000, found, _ready_selector(args))
            html = _snapshot(session.page, url) or html
            state = _classify(session.page, html, mode=args.mode)

        # The paid path is reached only for state "challenge" — Cloudflare's
        # managed challenge, which IS a test and can be solved once
        # `TURNSTILE_INTERCEPT_JS` has captured its parameters. It is bounded
        # by SOLVES_PER_PAGE so a rotation loop cannot become a bill, and a
        # sitekey-less detection refuses to build a task at all rather than
        # paying for one the API will reject (CLAUDE.md §19).
        if (page_flow.should_solve(state)
                and _solve_budget(args, solves_bought)):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _snapshot(session.page, url) or html
                state = _classify(session.page, html, mode=args.mode)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning(
                        "The solve was NOT accepted: page %d is still %s. The "
                        "purchase is spent.", page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid, and one page past
            # the end of a listing has no products — so retrying it would
            # spend the user's budget re-confirming the same right answer,
            # and rotating the exit would blame an address for the URL it was
            # given.
            break

        # Blocked or challenged. A different exit is the one thing that
        # plausibly changes the outcome: the ADDRESS is what was scored, not
        # the URL, so retrying it unchanged would only confirm it. Measured
        # 2026-09-09 — the same URL that answers 403 from a datacentre exit
        # answers 200 from a residential one.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over `--cdp-endpoint` a profile allows
                # one live connection, so tearing the session down and
                # reconnecting risks `profile_locked` and would lose the very
                # cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d). On this "
                               "site that is often what clears it.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        # NAME the proxy when it was the proxy. CLAUDE.md §8: a dead exit and
        # a timeout want opposite responses — another try at the same exit
        # versus a different exit — so a message that cannot tell them apart
        # leaves the reader guessing which they got.
        #
        # This branch used to drop `exit_failed` on the floor. With a POOL the
        # reason was logged on rotation, but WITHOUT one — a single --proxy,
        # which is the common case — the run said only "gave up loading" for
        # a proxy that had refused the connection outright. Found by running
        # it: `--proxy http://127.0.0.1:9` reported the generic message while
        # `_proxy_failure()` had correctly identified
        # ERR_PROXY_CONNECTION_FAILED one frame earlier.
        if exit_failed:
            logger.error(
                "Gave up loading %s: the PROXY refused the connection (%s), "
                "which is not a timeout and will not fix itself on a retry "
                "from the same exit. Check the exit, or pass --proxy-file so "
                "the run can rotate to another one.", url, exit_failed)
        else:
            logger.error("Gave up loading %s after %d attempt(s).",
                         url, args.retries)
        outcome.load_failed = True
        outcome.blocked_by = None
        return outcome

    outcome.state = state

    if state == "blocked":
        # What a caller needs here is WHICH refusal this is, because the two
        # want different answers and only one of them is solvable:
        #
        #   a branded refusal page  — no widget, no sitekey, nothing to
        #       solve. A different exit is the only move.
        #   "Just a moment..." with `cf_chl_opt`  — a test. That classifies
        #       as "blocked" with a solvable challenge on it.
        #
        # Mercor has produced NEITHER against this scraper. Reaching this
        # branch at all means the site's posture changed, so the message
        # below says what was measured and when rather than asserting what
        # will clear it — there is no measurement here to offer.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        assets = references_own_assets(html or "")
        logger.error(
            "Mercor did not serve this request — %d bytes, its own asset "
            "paths referenced %d time(s), saved to %s. This is NEW: on "
            "2026-09-18 this site served every route to a bare Hetzner "
            "datacentre address (AS24940, Helsinki), to plain curl, to "
            "python-requests and to an empty User-Agent, with no challenge "
            "of any kind on any of seven captures. So there is no measured "
            "remedy to recommend here — what is known is only that the "
            "posture has changed since then. A residential --proxy or "
            "--cdp-endpoint for the Scraping Browser are the usual moves, "
            "and the saved HTML is what says whether this is a challenge "
            "(solvable) or a flat refusal (not). This is exit 3, distinct "
            "from a genuinely empty result (exit 4).%s",
            len(html or ""), assets, debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        outcome.blocked_by = "refused" if html else "no-response"
        outcome.final_url = session.page.url
        return outcome

    # No readiness wait and no scroll on the content path, and their absence
    # is MEASURED rather than forgotten — see the "unknown" branch above.
    # Mercor embeds the whole result set in the first response, so there is
    # nothing to wait for and nothing to scroll into view. Porting the
    # sibling repos' scroll loop here would be dead code that looks
    # load-bearing (CLAUDE.md §4) — and worse than usual on this site,
    # because the grid is virtualised: scrolling paints more DOM nodes and
    # produces not one extra row.

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED, and that
    # narrowing was earned twice.
    #
    # A marker on a page whose products have rendered guards nothing — that
    # is the "detected is not blocking" rule the captcha default follows,
    # applied to the blocking decision instead of the spending one. But
    # `state != "content"` is still too wide: an EMPTY page is a correct
    # answer, and a live run of a /p/<slug> hub reported exit 3 on a 191 KB
    # page the site had plainly served, because the hub's own performance
    # script names `akamaihd.net` and "akamai" was in the marker list. Both
    # halves were wrong; the marker is gone (see
    # product_parser.BOT_CHALLENGE_MARKERS) and this now only refines the
    # REASON for a page the policy had already given up on.
    vendor = (detect_bot_challenge(html, url=session.page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     (f" (tried {block_retries + 1} exit(s))" if has_pool
                      else f" (re-fetched {block_retries + 1} time(s))"))
        outcome.blocked_by = vendor
        return outcome

    if not page_flow.should_parse(state):
        # Reached only for a state the policy says holds no rows — and it
        # says so in ONE place, so an engine cannot quietly decide to parse
        # something its twins would not.
        logger.info("Page %d came back as %s; nothing to parse.", page_num,
                    state)
        outcome.final_url = session.page.url
        return outcome

    products, listing = _parse_for_mode(html, session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if listing is not None:
        # Recorded on every page rather than only the first, so all three
        # engines carry the same outcome shape. On this site there is only
        # ever one listing page, so "page 1's copy" and "every page" are
        # the same thing — stated rather than relied on.
        outcome.total_available = listing.total_jobs
        outcome.total_companies = listing.total_companies
        outcome.pages_available = listing.pages_available
        outcome.urls_in_itemlist = listing.urls_in_itemlist
        outcome.echoed_page = listing.echoed_page
        if page_num == 1:
            # Mercor states no catalogue total on any route, so what is
            # reported is this response's own two views of itself. Printing
            # a line of Nones about totals the site does not publish would
            # read like a parse failure on a page that parsed perfectly.
            logger.info("This response held %d listing(s); its own ItemList "
                        "JSON-LD indexes %d. Mercor publishes no catalogue "
                        "total, so neither figure is a claim about how much "
                        "there is — see the sidecar for this run's coverage.",
                        listing.records_in_payload, listing.urls_in_itemlist)
        if listing.page_repeated:
            # The site clamped an out-of-range page back and answered 200.
            # Reported as a fact about the SITE rather than as a failure:
            # the rows are real, they are simply page 1's, and the caller
            # stops the run here instead of collecting them again.
            # Unreachable on Mercor as measured — `page_repeated` is always
            # False, because `/explore` has one page and cannot clamp a
            # request back to it. Kept so that a site which STARTS
            # paginating is noticed rather than silently re-collecting.
            logger.info("Asked for page %s and Mercor answered with page "
                        "%s — that is a site saying the listing has ended. "
                        "Stopping rather than re-collecting page %s.",
                        listing.requested_page, listing.echoed_page,
                        listing.echoed_page)

    if products:
        # No price on this site, so no price coverage. What stands in its
        # place is the handful of fields Mercor filled on 390 of 390 listing
        # records on 2026-09-18 — reported every time, so a consumer gets
        # the number rather than a threshold someone guessed.
        for field_name in _core_fields(args.mode):
            filled = sum(1 for row in products if getattr(row, field_name, None) not in (None, "", []))
            share = 100.0 * filled / len(products)
            if share < CORE_FIELD_FLOOR:
                logger.warning(
                    "Only %.0f%% of page %d carries `%s`, against a measured "
                    "floor of %d%%. Every record of every capture had one, "
                    "so this is the payload shape moving rather than the "
                    "listings being unusual — re-run with --dump-html.",
                    share, page_num, field_name, CORE_FIELD_FLOOR)

        paid = sum(1 for row in products if row.rate_min is not None)
        remote = sum(1 for row in products
                     if (row.work_arrangement or "").lower() == "remote")
        described = sum(1 for row in products if row.description)
        named = sum(1 for row in products if row.company_name)
        # All reported, none floored, and that is deliberate. `company_name`
        # is the one that would tempt a floor and must not have one: Mercor
        # runs most of its marketplace for unnamed clients, and 291 of 390
        # listings withhold the name on purpose — `company_brand_visible`
        # says so per row. A floor there would fire on healthy data every
        # single run.
        if args.mode == "job":
            # One job has no page statistics, so a listing sentence would be
            # nonsense about a run of one row.
            row = products[0]
            pay = ("not stated" if row.rate_min is None else
                   "%g-%g %s %s" % (row.rate_min, row.rate_max or row.rate_min,
                                    row.rate_currency or "(currency not "
                                    "published)", row.rate_period or ""))
            logger.info("Job: %s at %s, pay %s.", row.title,
                        row.company_name or "an unnamed client", pay.strip())
        else:
            logger.info("Page %d: %d/%d state pay, %d/%d remote, %d/%d carry "
                        "a description, %d/%d name a company (the rest are "
                        "withheld clients, not missing data).",
                        page_num, paid, len(products), remote, len(products),
                        described, len(products), named, len(products))

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock.
        A worker that gets blocked can still walk the rest of the pool on its
        own.

    Its exit stays put for the worker's lifetime otherwise: a SESSION must
    not change address mid-flight, and a worker is one session.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page comes back with no rows at all — the end of the
    # listing. Without it, asking for 50 pages of a 5-page result would fetch
    # 45 empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra pages are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the listing "
                                        "and stopping dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). Not reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # All three modes are one row per business-at-a-location, so `sku` is the
    # key for all of them.
    dedupe_key = "sku"
    # Why the loop ended. "completed" means every requested page was
    # fetched; "no_new_products" means the enumeration itself ran out (also
    # a complete result). Anything else is an early stop, and the run is
    # only a partial view.
    #
    # "single_page_route" is complete by construction AND by measurement,
    # which is unusual enough to name: `/explore` and `/careers` each hold
    # their whole result set in one response, and every pagination
    # parameter tried on `/explore` (?page, ?limit, ?offset, ?domain)
    # returned a byte-identical payload on 2026-09-18. So one fetch really
    # is the whole route, and treating either as paginated would report a
    # complete multi-page run holding one page several times (§18).
    #
    # --mode job is the paginated mode, and its pages are job URLs from the
    # enumeration rather than a `?page=N` convention — so there is no
    # "walked off the end" case to guard: the plan cannot exceed the list.
    stop_reason = ("single_page_route" if args.mode in ("listings", "careers")
                   else "completed")

    # How many jobs `--mode job` found to fetch. Set by the enumeration
    # below and read after the browser is closed, so it is bound here rather
    # than inside the `with` block — an UnboundLocalError on the reporting
    # path would lose a run that had already done all its work.
    total_enumerated = 0

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if page_flow.concurrency_for_mode(args.mode, concurrency) == 1:
            # `listings` and `careers` fetch exactly one page, so a second
            # worker would have nothing to do. `job` is the mode that CAN
            # use them: every page is an independent job URL read from the
            # enumeration, which is precisely what the concurrency machinery
            # wants (§7).
            logger.info("--concurrency is ignored in --mode %s: the route "
                        "holds its whole result set in one response, so "
                        "there is a single page to fetch. --mode job is the "
                        "mode that parallelises.", args.mode)
            concurrency = 1
        elif page_flow.concurrency_limit(args.cdp_endpoint) == 1:
            # The limit is page_flow's to state, not this engine's, so all
            # three refuse in the same place for the same reason.
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. "
                           "Mercor served every request measured on "
                           "Cloudflare on EVERY route including robots.txt, "
                           "so an address that works is one worth not "
                           "burning. Pass --proxy-file to spread the load.",
                           concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            target = _target_url(args)

            # In --mode job the enumeration comes FIRST, because it is what
            # decides which address page 1 even is. Everywhere else page 1's
            # own content is what decides how many pages there are, so the
            # order is the other way round.
            job_urls: List[str] = []
            if args.mode == "job" and not _is_single_job_url(args):
                job_urls = _enumerate_jobs(session, args)
                total_enumerated = len(job_urls)
                if not job_urls:
                    # Nothing to fetch, so nothing is written — `save`
                    # leaves the previous run's good output in place and
                    # no sidecar contradicts it (CLAUDE.md §9).
                    return finish_run(
                        [], args.out, args.format, args.allow_empty,
                        blocked=False, stop_reason="enumeration_empty",
                        pages_requested=args.pages, pages_completed=0,
                        mode=args.mode, source=SOURCE_DEFAULT,
                        start_url=args.url or "", final_url="")
                target = job_urls[0]

            if target != args.url:
                logger.info("Fetching %s", target)

            # Page 1 is always fetched on its own: its content is what decides
            # how many pages 2..N there are to address at all.
            first = _fetch_one_page(session, args, pool, 1, target)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            elif args.mode == "job" and not job_urls:
                pass  # `--url` named one job; one page is the whole run
            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                planned = _plan_page_urls(args, first.final_url,
                                          first.pages_available,
                                          job_urls=job_urls)
                if len(planned) + 1 < args.pages:
                    # Capped by the site's own `pageCount`, a complete
                    # answer rather than an early stop — see COMPLETE_STOP_REASONS.
                    stop_reason = "page_cap_reached"

                if args.pages > 1 and concurrency > 1 and not page_flow.pagination_is_addressable(first.final_url):
                    logger.warning("--concurrency %d requested, but this "
                                   "listing's pages cannot be addressed "
                                   "independently — falling back to one page "
                                   "at a time.", concurrency)
                    concurrency = 1

                if planned and concurrency > 1:
                    # Close the page-1 browser before starting workers: it has
                    # done its job, and holding it open would cost one more
                    # browser than asked for.
                    session.close()
                    specs = [(n, planned[n - 2]) for n in range(2, len(planned) + 2)]
                    logger.info("Fetching pages 2-%d across %d workers%s.",
                                len(planned) + 1, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        # Should not happen without a failure or exhaustion,
                        # but say so rather than reporting a complete run.
                        stop_reason = "pages_unattempted"
                    session = None  # already closed
                elif planned:
                    url = planned[0]
                    for page_num in range(2, len(planned) + 2):
                        # A new exit per page is what actually spreads a run's
                        # volume, and it costs a browser relaunch: carrying the
                        # session across exits would defeat the point.
                        if pool and pool.rotates_per_page():
                            pool.advance(f"per-page rotation, page {page_num}")
                            session.relaunch()

                        outcome = _fetch_one_page(session, args, pool, page_num, url)
                        outcomes.append(outcome)
                        if not outcome.ok:
                            stop_reason = ("page_load_timeout" if outcome.load_failed
                                           else f"blocked_{outcome.blocked_by}")
                            blocked = outcome.blocked_by is not None
                            break

                        # Whether this page contributed anything not already
                        # seen. Kept as a running check because the condition is
                        # inherently sequential — "new" only means anything
                        # relative to the pages before it. The authoritative
                        # dedupe happens once, after the loop, in page order.
                        fresh_count = sum(1 for p in outcome.products
                                          if p.sku is None or p.sku not in seen_keys)
                        seen_keys.update(p.sku for p in outcome.products
                                         if p.sku is not None)

                        # A page past the first that contributes nothing new
                        # means the end of the results — or that pagination is
                        # looping back on itself. Either way there is nothing
                        # further to fetch, and this is the honest terminating
                        # condition: a property of the DATA, not of a CSS
                        # selector that may have been renamed.
                        if not fresh_count:
                            logger.info("Page %d added no rows not already seen "
                                        "— treating that as the end of the "
                                        "listing.", page_num)
                            stop_reason = "no_new_products"
                            break

                        if page_num - 1 < len(planned):
                            url = planned[page_num - 1]
                            time.sleep(args.delay)
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this is
    # what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # On this site ANY duplicate is unexpected, which is why it is
            # logged rather than silently applied. `/explore` names each
            # listing exactly once (390 records, 390 distinct ids measured
            # 2026-09-18) and it has no second page to overlap with, and a
            # `--mode job` run fetches a de-duplicated enumeration. So a
            # non-zero count here means either the site started repeating
            # rows or the enumeration handed the same URL to two workers,
            # and both are worth seeing.
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    if args.domain:
        # A CLIENT-SIDE filter, applied after the merge and never presented
        # as anything else. Mercor's index accepts no query parameters —
        # `?domain=Finance` was measured returning the identical 390 records
        # — so this cannot reduce what is downloaded and the run's own log
        # says so. Matched case-insensitively against the site's own
        # spelling.
        wanted = args.domain.strip().lower()
        before = len(all_rows)
        all_rows = [r for r in all_rows
                    if (getattr(r, "domain", None) or "").strip().lower() == wanted]
        logger.info("--domain %r kept %d of %d row(s). This filtered rows "
                    "that were already fetched: Mercor's index takes no "
                    "query parameters, so every row was downloaded either "
                    "way.", args.domain, len(all_rows), before)
        if not all_rows and before:
            seen_domains = sorted({(getattr(r, "domain", None) or "?")
                                   for oc in outcomes for r in oc.products})
            logger.warning("No row carries domain %r. This run saw: %s",
                           args.domain, ", ".join(seen_domains) or "none")

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides (CLAUDE.md §8).
    #
    # What "complete" can honestly mean differs by mode here, and CLAUDE.md
    # §21's "complete and exhaustive are different words" is the whole point:
    #
    #   listings  One response, and that IS the whole route — the run is
    #             genuinely complete. It is also a 390-of-462 SLICE of what
    #             Mercor publishes, because the index does not show every
    #             live listing. Both facts go in the log and the sidecar.
    #   job       Complete when every page the run planned was fetched. The
    #             plan comes from the sitemap ∪ index union, so this is the
    #             only mode that can cover everything.
    #   careers   One response. Ashby states no total to check against.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    pages_available = next((o.pages_available for o in outcomes
                            if o.pages_available is not None), None)
    indexed = next((o.urls_in_itemlist for o in outcomes
                    if o.urls_in_itemlist is not None), None)

    if args.mode == "listings" and all_rows:
        if indexed is not None:
            # Reported every run rather than inherited from a docstring
            # (§13). The ItemList was the SMALLER view on 2026-09-18 — 326
            # against 390 — and an inversion would mean the primary source
            # had started dropping rows.
            logger.info("This response held %d listing(s); its own ItemList "
                        "JSON-LD indexes %d of them. The gap is Mercor's "
                        "evergreen listings, which it omits from the index's "
                        "structured data — which is why this scraper reads "
                        "the React Query payload instead.",
                        len(all_rows), indexed)
        logger.info("--mode listings fetches everything /explore serves, and "
                    "/explore is not everything Mercor publishes: 72 of the "
                    "462 listings enumerated on 2026-09-18 appeared only in "
                    "the sitemap, and all three of those checked were live. "
                    "Use --mode job to cover them.")

    if args.mode == "job" and total_enumerated:
        done = len([o for o in outcomes if o.ok])
        logger.info("Fetched %d of the %d job(s) this run enumerated (%.1f%%)."
                    "%s", done, total_enumerated,
                    100.0 * done / total_enumerated,
                    "" if done >= total_enumerated
                    else "  Pass --pages %d to cover the rest." % total_enumerated)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    #
    # Mercor states no catalogue total on any route, so unlike a site that
    # publishes `totalResults` there is no figure to copy. What goes here is
    # this run's OWN arithmetic about how much of the site it covered —
    # which is what a bare "complete" hides (§21), and what stops a reader
    # of the file inheriting a number from a README that was measured on
    # some other day.
    extra = {"route_is_paginated": args.mode == "job", "mode": args.mode}
    if args.mode == "listings":
        extra["records_in_payload"] = total_available
        extra["urls_in_itemlist"] = indexed
        extra["pages_available"] = pages_available
    elif args.mode == "job":
        extra["jobs_enumerated"] = total_enumerated or None
        extra["jobs_fetched"] = len(ok_pages)
    if args.domain:
        extra["domain_filter"] = args.domain

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Mercor job scraper — the marketplace index, one "
                    "listing, or Mercor's own openings (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A mercor.com URL: the marketplace index "
                        "(https://work.mercor.com/explore), one listing "
                        "(https://work.mercor.com/jobs/list_.../slug) with "
                        "--mode job, or Mercor's own openings "
                        "(https://www.mercor.com/careers) with --mode "
                        "careers. OPTIONAL — every mode has a correct "
                        "default, because the index takes no parameters and "
                        "there is only one of each route. Giving a job URL "
                        "with --mode job fetches exactly that job and skips "
                        "the enumeration. Also read from MERCOR_URL in the "
                        "environment or in .env.")
    p.add_argument("--domain", default=None, metavar="NAME",
                   help="Keep only listings whose `domain` matches, case "
                        "insensitively. This filters rows AFTER they are "
                        "fetched and is honest about it: Mercor's index "
                        "accepts no query parameters at all — ?domain= on "
                        "/explore was measured returning the identical 390 "
                        "records — so nothing here reduces what is "
                        "downloaded. The 12 values Mercor publishes include "
                        "Finance, Software Engineering, Medicine, Law and "
                        "'Life, Physical, and Social Science'. --mode "
                        "listings and --mode job only; the careers route "
                        "publishes departments instead.")
    p.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE,
                   help="listings (default): work.mercor.com/explore, the "
                        "marketplace index. 390 contract roles in ONE "
                        "response, with rates, domains, eligibility and slot "
                        "counts; --pages does not apply, the route has no "
                        "second page. job: one listing per page, enumerated "
                        "from the sitemap and the index (462 jobs measured "
                        "2026-09-18). Adds the company website, the company "
                        "description, the spot counts and a currency, and is "
                        "the only mode that covers the 72 listings the index "
                        "does not show — use --pages to say how many, and "
                        "--concurrency to fetch them in parallel. careers: "
                        "www.mercor.com/careers, Mercor's OWN salaried "
                        "openings via Ashby. A different population with its "
                        "own id space, so diff_runs.py will not compare it "
                        "against the other two.")
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar. Defaults "
                        "to what the URL selects — 'explore', 'careers', or "
                        "the listing id. Mercor's routes carry no category "
                        "segment, so this names the RUN rather than a site "
                        "concept; the site's own category for a listing is "
                        "the `domain` column, which is where a filterable "
                        "field belongs.")
    p.add_argument("--pages", type=int, default=1,
                   help="Number of listing pages to fetch. Applies to --mode "
                        "role only. A run PLANS against the `pageCount` the "
                        "site states on page 1 and never asks past it — "
                        "asking for page 48 of a 47-page listing does not "
                        "error, it answers HTTP 200 with page 1 AGAIN, which "
                        "a run that trusted the request would collect twice. "
                        "A request for more is honoured up to that cap and "
                        "the sidecar records both numbers.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty hub "
                        "category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="mercor_jobs", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "which directory is searched — that is find_country "
                        "in the URL, or --country — so this only affects what "
                        "the browser claims about itself.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused (HTTP 403) or behind "
                        "a captcha, retry it from this many OTHER exits before "
                        "giving up (default 2). Needs a pool of more than one; "
                        "ignored otherwise. This is the flag that matters most "
                        "on this site: the refusal is a property of the "
                        "ADDRESS, and a different exit is what clears it.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation. Measured
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`, `Chrome`
    # and `Desktop` each 400. fingerprint_client.py's own --tags help has
    # said so all along; the engines' default contradicted it.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Neither "
                        "setting changes what Cloudflare does: a managed "
                        "challenge is what a scored address gets on every "
                        "route here, and a solve only helps once "
                        "TURNSTILE_INTERCEPT_JS has captured its parameters. "
                        "A residential exit removes the challenge entirely, "
                        "which is why most runs never reach this path.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)

    # Every mode has a correct default URL, so unlike the sibling repo this
    # engine was ported from there is nothing to refuse here and no pair of
    # flags that could disagree: Mercor's index accepts no parameters, so
    # there is no --role/--location equivalent that could contradict a path.
    # `--url` is checked for being a route this scraper reads, and a URL
    # that names a different route than --mode is a real conflict.
    if args.url:
        ok, why = is_supported_url(args.url)
        if not ok:
            p.error(why)
        implied = mode_for_url(args.url)
        if implied and implied != args.mode:
            # Refused rather than silently corrected. Reading a careers URL
            # under --mode listings would parse it with the wrong reader and
            # report zero rows for a perfectly good page — the failure
            # CLAUDE.md §20 describes, where a served page that parses to
            # nothing sends the reader to check the URL instead of the code.
            p.error("--url is a %s URL but --mode is %s. %s is read by "
                    "--mode %s; pass that, or drop --url and let --mode %s "
                    "use its own default."
                    % (implied, args.mode, args.url, implied, args.mode))

    if args.domain and args.mode == "careers":
        p.error("--domain filters the marketplace's `domain` column, which "
                "the careers route does not publish — its roles carry a "
                "`department` instead. Drop --domain, or use --mode "
                "listings.")


    if args.mode in ("listings", "careers") and args.pages != 1:
        # Said out loud rather than silently ignored: a user who passed
        # --pages 5 expects five pages of something. Measured rather than
        # assumed — `?page=2`, `?limit=`, `?offset=` and `?domain=` on
        # /explore each returned a byte-identical payload of the same 390
        # records on 2026-09-18, so there is genuinely nothing to fetch.
        logger.warning("--pages %d is ignored in --mode %s: the route holds "
                       "its whole result set in ONE response and honours no "
                       "pagination parameter. --mode job is the mode that "
                       "pages — there a page is one job, and --pages 462 "
                       "fetches every one Mercor publishes. The run status "
                       "will say single_page_route.", args.pages, args.mode)
        args.pages = 1

    if args.category is None:
        # Default the run label to what the run selects, so the sidecar names
        # it without the flag. With no --url the label is the mode, because
        # that IS the whole selection on this site: the index takes no
        # parameters, so two `--mode listings` runs are always comparable.
        args.category = (category_from_url(args.url) if args.url
                         else args.mode)
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest one:
        # `profile_locked` means another run still holds this `pid`, and a
        # harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
