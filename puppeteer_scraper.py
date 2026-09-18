"""
mercor-scraper — Puppeteer edition

Scrapes mercor.com: the marketplace index, one marketplace listing, and
Mercor's own corporate openings.

    --mode listings  work.mercor.com/explore
                     The marketplace index. 390 contract roles in ONE
                     response — rates, domains, eligibility, slot counts.
    --mode job       work.mercor.com/jobs/{id}/{slug}
                     One listing, with the company website, the company
                     description and a currency the index does not publish.
                     Enumerated from the sitemap and the index.
    --mode careers   www.mercor.com/careers
                     Mercor's own salaried roles, via Ashby.

What is different about Mercor
==============================

* **Nothing is gated.** Measured 2026-09-18 from a Hetzner datacentre
  address (AS24940, Helsinki): eighteen candidate bot-challenge markers
  counted across seven captures, every one zero; identical 200s from
  `curl`, from `python-requests` and from an EMPTY User-Agent. No captcha,
  no proxy and no key is needed for any route this engine reads.

* **The rows are not in the DOM, and the obvious structured source is the
  wrong one.** `/explore` carries an `ItemList` JSON-LD of 326 URLs and a
  React Query cache of 390 complete records; the JSON-LD omits every one of
  the 55 `evergreen` listings. `product_parser` reads the cache.

* **Hydration rewrites every job anchor.** The served markup has
  `href="/jobs/list_…"`, the live DOM has `href="/explore?listingId=…"`.
  Anything anchored to the DOM here must be checked in a real browser and
  not in a capture — see `page_flow.READY_SELECTOR_LISTING` for what the
  single-spelling version cost.

* **`/explore` has no second page.** `?page=2`, `?limit=`, `?offset=` and
  `?domain=` each returned a byte-identical payload. In `--mode job` a
  "page" is one job instead, and those addresses are read from the sitemap
  rather than guessed from a convention.

* **No single route enumerates the catalogue.** sitemap 447, explore 390,
  shared 375, union 462 — neither contains the other, and the 72
  sitemap-only listings are live jobs.

Usage
-----
    python3 puppeteer_scraper.py
    python3 puppeteer_scraper.py --mode careers
    python3 puppeteer_scraper.py --mode job --pages 50
    python3 puppeteer_scraper.py --mode job --url "https://work.mercor.com/jobs/list_AAABoGhhxWgF7ygE2LZOgIy3/b-2-b-sales-expert-3-yoe-us-only"

This site answered every one of those from a bare datacentre IP with no
credentials on 2026-09-18. `--proxy` and `--cdp-endpoint` work and are not
required.

pyppeteer is effectively unmaintained and its own README points at
Playwright; this engine exists for parity and is verified against the same
live site, not assumed to match.
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

# At module level, deliberately, and not inside the launch path where it
# started out. The offline suite guards `import puppeteer_scraper` behind
# try/except ImportError and REPORTS the skip, and CI's engine-smoke job fails
# on any reported skip — that whole mechanism only works if importing this
# module actually requires the driver. With the import hidden inside
# _Session.open(), the module imported cleanly with no pyppeteer installed at
# all, the group never skipped, and CI could not have noticed a broken import.
# It also let CI install pyppeteer 0.0.25 (a stub, resolved from an unpinned
# `pip install pyppeteer`) without anything failing, because nothing ever
# imported it.
from pyppeteer import launch, connect

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
                            enumerate_job_urls, sitemap_job_urls,
                            listings_from_html, route_of,
                            EXPLORE_URL, CAREERS_URL, SITEMAP_URL)
from output_writer import (dedupe_by_key, finish_run, EXIT_API_ERROR,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The share of rows that must carry the columns Mercor fills on every
# record, below which the read has broken rather than the data being
# unusual. 390 of 390 listing records carried all four on 2026-09-18.
#
# Deliberately NOT here, each with the measurement that keeps it out:
# `company_name` (99 of 390 — Mercor runs most of its marketplace for
# unnamed clients), `hours_per_week` (294 of 390), `eligible_locations`
# (134 of 390), `rate_currency` (0 on the index, which publishes none).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = ("title", "url", "sku", "rate_min")

# --mode careers. Different rather than shorter: an Ashby record always
# states a department, but only 106 of 108 state a salary, so `rate_min`
# cannot carry a 99% floor here.
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

# A page holding less than this share of the fullest page in the same run is
# reported as thin. A landing page holds twenty COMPANIES and however
# many jobs they have — 32 to 45 across the captures — so this is a soft
# signal rather than a fixed page size.
THIN_PAGE_SHARE = 0.35

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share (how long to wait for
    challenge, when to scroll, when only a fresh session helps) and it is
    written against plain synchronous callables — which is the right shape for
    two of the three drivers. Bridging here keeps the policy in one place
    rather than growing an async copy of it that would drift.

    The second benefit is the one the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which is not something
    pyppeteer's own API offers.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one as "Future exception was never retrieved:
        # NetworkError('Protocol error Target.sendMessageToTarget: Target
        # closed.')" — at ERROR level, AFTER a successful run has printed its
        # results. Five of those under a "Saved 48 products" line read as a
        # failed run. Only that shape is swallowed; anything else still gets
        # the default handler, because silencing the loop wholesale would hide
        # real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH, not one or the other. asyncio puts its own words in
        # `message` ("Future exception was never retrieved") and the library's
        # in `exception` (a NetworkError about a closed CDP session), and an
        # `or` between them looks at the exception and never sees the message
        # — which is why these kept printing after they were "handled".
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                # asyncio's own words when the loop stops with work in
                # flight. Emitted after a successful run; see close().
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                # A CDP message addressed to a session that has gone away.
                # Routine over a remote browser: three of six captures of
                # this site had their target closed mid-scroll and succeeded
                # on the next attempt.
                "No session with given id",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping the loop outright leaves pyppeteer's own background tasks
        pending — its websocket reader and keepalive — and asyncio then prints
        "Task was destroyed but it is pending!" plus a traceback for each of
        them. That happens AFTER the output has been written, so the run is
        fine and the log looks like a crash. Four tracebacks under a
        successful run is how a reader learns to ignore the log.

        Cancelling first is the fix, and it has to happen ON the loop thread —
        `call_soon_threadsafe` is what gets it there.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            if pending:
                logger.debug("Cancelled %d pending pyppeteer task(s) on "
                             "teardown.", len(pending))
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # What this response said about itself. Mercor publishes no catalogue
    # and how many pages the site will serve. These are what pages are
    # planned from.
    total_available: Optional[int] = None
    total_companies: Optional[int] = None
    # How many entries the same response's ItemList JSON-LD indexes. 326
    # against 390 records on 2026-09-18 — the gap is why the parser reads
    # the React Query cache, and recording it per run means nobody inherits
    # the figure from a comment (§13).
    urls_in_itemlist: Optional[int] = None
    pages_available: Optional[int] = None
    # The page number the SERVER answered with, read back out of the Apollo
    # cache key — not an echo of the request. Asking for page 48 of a
    # 47-page listing comes back stating page 1, which is how a run learns it
    # has walked off the end instead of silently re-collecting page 1.
    echoed_page: Optional[int] = None
    # The page number the SERVER answered with, read back out of the
    # Apollo cache key rather than echoed from the request.

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
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


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded number: it drifts the moment a newer Chromium ships, and
    claiming an older Chrome than the JS engine and TLS handshake report is
    itself a mismatch a fingerprinter can key on. pyppeteer's
    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone, so the cookie jar goes with the exit.
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            try:
                self.browser = self.bridge.run(
                    connect(browserWSEndpoint=self.args.cdp_endpoint,
                            ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — see below
                # The websockets library raises InvalidStatusCode here, and
                # its message is just "server rejected WebSocket connection:
                # HTTP 500" — which names neither the endpoint nor the
                # reason, and matched none of the patterns __main__ uses to
                # map a remote failure onto exit 5. A live profile run
                # therefore died with a raw traceback and exit 1, telling a
                # harness to go looking for a bug in this code when the real
                # answer is "wait, or use a different pid".
                #
                # Re-raised with the endpoint MASKED and the meaning spelled
                # out. HTTP 500 from cb.2captcha.com is overwhelmingly
                # `profile_locked`: a Scraping Browser profile allows ONE live
                # connection, and the run before this one may still hold it.
                raise RuntimeError(
                    "could not connect to --cdp-endpoint %s: %s\n"
                    "A Scraping Browser profile allows ONE live connection at "
                    "a time, so an HTTP 500 here usually means another run "
                    "still holds this `pid`. Wait for it to finish, or use a "
                    "different pid."
                    % (_mask_credentials(self.args.cdp_endpoint),
                       _mask_credentials(str(e)))) from None
            self.page = self.bridge.run(self.browser.newPage())
            return self

        # --lang really applies the locale rather than accepting the flag
        # and ignoring it. It does NOT decide which listing is
        # searched; that is find_country in the URL.
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       f"--lang={self.args.locale}"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises
        # "signal only works in main thread of the main interpreter" because
        # the event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead, so nothing is
        # lost — the browser is still closed on both success and failure.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if self.args.fingerprint:
            self._apply_fingerprint()
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def _apply_fingerprint(self):
        """Apply a 2captcha fingerprint to this page.

        The SAME init script the Playwright and Selenium engines install,
        shared deliberately: two engines applying different halves of one
        fingerprint would be a contradiction of exactly the kind a
        fingerprint is meant to avoid.

        Never reached over --cdp-endpoint (the remote browser brings its own
        identity, and stacking a second is worse than none) — that branch
        returns before this is called.
        """
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(
                self.page.evaluateOnNewDocument(playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:  # noqa: BLE001 — a fingerprint is not the run
            logger.warning("Could not apply the fingerprint (%s) — continuing "
                           "without it.", e)

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        """Close the page, and on a REMOTE browser disconnect from it too.

        The disconnect is not tidiness. Closing only the page leaves
        pyppeteer's websocket to the remote browser open, and when the
        bridge's event loop then shuts down, `websockets` unwinds its own
        connection outside a running loop — printing four "Exception ignored
        in: <coroutine …>" tracebacks AFTER the output has already been
        written. A successful run that ends in four tracebacks is how a
        reader learns to ignore the log, which is the same reasoning as
        _AsyncBridge.close()'s.

        The remote BROWSER is deliberately left running: it is not ours, and
        a Scraping Browser profile is reused across runs.
        """
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
                self.bridge.run(self.browser.disconnect(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here; every decision about what to do
# with the answer is in page_flow.py so all three engines make it the same way.
def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        return len(bridge.run(page.querySelectorAll(selector)))

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            # A URL canonicalisation can navigate, so a snapshot can land
            # exactly on the document swap. None tells the caller to skip a
            # check rather than fail the run.
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

    # Named OPERATIONS rather than JavaScript crossing the page_flow
    # boundary: pyppeteer takes `() => expr` while Selenium takes a function
    # body with an explicit `return`, so a shared module passing JS would
    # acquire one driver's dialect.
    #
    # No scroll primitive, and its absence is measured rather than forgotten:
    # Mercor serialises its whole result set into __NEXT_DATA__ in the
    # first response. Mirrors the
    # other two engines.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


def _content(session) -> Optional[str]:
    return _driver(session)["content"]()


def _is_single_job_url(args) -> bool:
    """Whether `--url` names one specific job, rather than a route to walk.

    A `--mode job` run given a job URL fetches exactly that job and nothing
    else — no sitemap, no enumeration, one row.
    """
    return bool(args.url) and route_of(args.url) == "job"


def _plan_page_urls(args, page_one_url, pages_available, job_urls=None):
    """URLs for pages 2..N, decided once before any of them is fetched.

    Mercor needs no hedging between a site's next-link and a URL
    convention, and for the opposite reason to most sites in this family:
    it has nothing to hedge ABOUT.

      * `/explore` and `/careers` have exactly one page each. Measured
        2026-09-18, `?page=2`, `?limit=10`, `?offset=100` and
        `?domain=Finance` on `/explore` each returned a byte-identical
        payload of the same 390 records. `page_url()` returns None by
        design, and this plans nothing.
      * `--mode job` plans from a REAL LIST — the enumeration built from
        `/sitemap.xml` and the `/explore` payload — so every planned page
        is an address the site published, and the plan cannot run off the
        end because it is capped by the length of the list.
    """
    if args.mode == "job":
        if not job_urls:
            return []
        wanted = page_flow.pages_to_plan(args.pages, len(job_urls))
        if wanted < args.pages:
            logger.info("The enumeration holds %d job(s) and %d page(s) were "
                        "asked for; planning %d. There is no more catalogue "
                        "to walk to.", len(job_urls), args.pages, wanted)
        return list(job_urls[1:wanted])

    if not page_flow.pagination_is_addressable(page_one_url):
        if args.pages > 1:
            logger.info(
                "%s is served at ONE address and holds its whole result set "
                "in a single response, so %d page(s) were asked for and 1 "
                "will be fetched. Every pagination parameter tried on it was "
                "ignored, so planning a page 2 would re-collect page 1 and "
                "report a complete multi-page run. Use --mode job if you "
                "want many pages: there a page is one job.",
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


def _enumerate_jobs(fetch_source, args):
    """Every job URL this run could fetch, in a stable order.

    The union of two sources, because **neither contains the other**.
    Measured 2026-09-18: `/sitemap.xml` holds 447 job URLs, the `/explore`
    payload holds 390, they share 375, and the union is 462. Three of the 72
    sitemap-only listings were opened by hand and all three were live. A run
    enumerating from either source alone would miss real work while
    reporting a complete run.

    `fetch_source` is a callable the ENGINE supplies, so both fetches leave
    from the same exit as the pages they plan — a run whose sitemap came
    from one address and whose jobs came from another is two runs wearing
    one sidecar. It must return the serialised document rather than its
    text: Chromium renders XML through its own viewer, and `content()` keeps
    all 463 `<loc>` elements intact where a text reading does not.

    Sitemap order leads because it is Mercor's own publication order and is
    stable between runs, which keeps `--pages 20` fetching the SAME twenty
    jobs each night rather than a different twenty.
    """
    sitemap_xml = ""
    try:
        sitemap_xml = fetch_source(SITEMAP_URL)
    except Exception as exc:  # noqa: BLE001 - any driver error is the same here
        logger.warning("Could not read %s (%s). Falling back to the /explore "
                       "payload alone, which held 390 of the 462 listings "
                       "measured on 2026-09-18.", SITEMAP_URL, exc)
    explore_html = ""
    try:
        explore_html = fetch_source(EXPLORE_URL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s (%s). Falling back to the sitemap "
                       "alone.", EXPLORE_URL, exc)

    urls = enumerate_job_urls(sitemap_xml, explore_html)
    if not urls:
        logger.error("Neither %s nor %s yielded a single job URL, so this "
                     "run has no work to do.", SITEMAP_URL, EXPLORE_URL)
        return []
    n_sitemap = len(sitemap_job_urls(sitemap_xml))
    n_explore = len(listings_from_html(explore_html))
    logger.info("Enumerated %d job(s): %d from the sitemap, %d in the "
                "/explore payload, %d in both. Neither source is a superset "
                "of the other on this site.",
                len(urls), n_sitemap, n_explore,
                n_sitemap + n_explore - len(urls))
    return urls


def _parse_for_mode(html: str, url: str, args, page_num: int = 1):
    """(rows, listing) for this mode. `listing` is None outside --mode listings.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it, a row
    from page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output.
    """
    if args.mode == "job":
        row = parse_detail(html, url=url)
        return ([row] if row is not None else []), None
    if args.mode == "careers":
        return parse_careers_page(html, url=url), None
    listing = parse_listing_page(html, url=url, page=page_num)
    return listing.rows, listing


def _is_endpoint(url: str) -> bool:
    """Whether this address answers with raw JSON rather than a document.

    Always False on Mercor: unlike the sibling repo this engine was
    ported from, there is no ungated JSON endpoint here — every route is an
    HTML page with its payload serialised inside it. Kept so the three
    engines' snapshot paths branch identically.
    """
    return False


def _snapshot(session, url: str):
    """What the parser is given for this address. Mirrors the other engines.

    A PAGE is read with `content()`. The ENDPOINT answers with JSON, which
    Chromium wraps in its own JSON-viewer markup, so reading the body text is
    the only way to get back what the server actually sent.
    """
    if _is_endpoint(url):
        try:
            return session.bridge.run(session.page.evaluate(
                "() => document.body.innerText")) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read the endpoint response: %s", e)
            return None
    return _driver(session)["content"]()


def _fetch_page_source(session, url: str, timeout_ms: int = 60000) -> str:
    """Navigate and return the serialised document. Used for enumeration.

    `content()` rather than a text reading, and that matters for the
    sitemap: Chromium renders `text/xml` through its own XML viewer, and
    `content()` keeps all 463 `<loc>` elements intact where the viewer's
    rendered text does not.
    """
    session.bridge.run(session.page.goto(
        url, {"waitUntil": "domcontentloaded", "timeout": timeout_ms}))
    return session.bridge.run(session.page.content()) or ""


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine — see its docstring for why the anchor count is
    checked here rather than after the readiness wait.
    """
    bridge, page = session.bridge, session.page
    html = _content(session)
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = len(bridge.run(page.querySelectorAll(selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True





def _target_url(args) -> str:
    """The address this run actually fetches.

    Every mode has a correct default, so a bare invocation is a working
    command rather than a usage error: Mercor's index takes no parameters,
    so the only choice a user has is which of three routes to read, and
    `--mode` is that choice.
    """
    if args.url:
        return args.url
    if args.mode == "careers":
        return CAREERS_URL
    return EXPLORE_URL


def _solve_budget(args, spent: int):
    """Whether another solve may be bought for this page.

    `page_flow.SOLVES_PER_PAGE` says at most one purchase per page, and that
    is a MONEY limit rather than a style rule. It was not being enforced:
    `handle_captcha_if_present` is called twice per attempt — once before the
    page is classified and once after — and only the SECOND call was counted.

    INHERITED EVIDENCE, from a SIBLING repo and not from Mercor — labelled
    because §13 says a number you inherited is not a number you measured,
    and this one cannot be reproduced here: Mercor has never rendered a
    challenge to this scraper, so no page of this site has ever bought a
    solve at all.

    On that sibling, from a datacenter address that meets a real Cloudflare
    challenge on every fetch (2026-09-17): ONE page bought THREE Turnstile
    solves, and every token was refused. The cap read as enforced and was
    not (CLAUDE.md §17). Both call sites now go through here.

    The fix is carried here anyway. A budget that is never exercised is
    still the difference between a bill and no bill on the day it is.
    """
    return spent < page_flow.SOLVES_PER_PAGE


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    The retry/rotate/wait policy is page_flow's and finish_run's; what differs
    here is only the driver calls. Kept structurally parallel on purpose —
    the two files are meant to be diffable, because "all three engines agree"
    is checked by reading them side by side as well as by the smoke suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
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
        # `exit_failed` holds Chromium's own name for a proxy fault, or None.
        # Named and shaped exactly as in the other two engines: the give-up
        # branch below reports it, and three engines that structure this
        # differently drift (CLAUDE.md §6).
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                           "timeout": 60000}))
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                # pyppeteer surfaces a dead proxy as a page error whose text
                # carries Chromium's own name for it, exactly as Playwright
                # does; a timeout and an unusable exit want opposite
                # responses, so they are told apart by that text.
                text = str(e)
                marker = next((m for m in _PROXY_ERROR_MARKERS if m in text), None)
                if marker:
                    exit_failed = marker
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if load_failed and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)
            continue
        if load_failed:
            break


        # Counted, because it can BUY. See _solve_budget.
        if _solve_budget(args, solves_bought):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)

        elif solves_bought:
            logger.info("Not solving again on page %d: %d purchase(s) "
                        "already made for it and SOLVES_PER_PAGE is %d.",
                        page_num, solves_bought, page_flow.SOLVES_PER_PAGE)
        html = _snapshot(session, url) or ""
        state = page_flow.classify(html, None, page.url)

        # Mercor server-renders its payload, so a page is parseable
        # in the FIRST response. The wait below is only for the state that
        # says Mercor
        # served SOMETHING that is not the payload. Mirrors the other two
        # engines.
        if state == "unknown":
            wait_ms = page_flow.content_timeout_ms(args.mode)
            sel = page_flow.ready_selector(args.mode)
            need = page_flow.min_matches(args.mode)
            logger.info("Page %d is something Mercor served (%d bytes, its own "
                        "assets referenced %d time(s)) but carries no listing "
                        "payload — waiting up to %.0fs rather than spending a "
                        "retry.", page_num, len(html),
                        references_own_assets(html), wait_ms / 1000.0)
            found = page_flow.wait_for_count(d["count"], sel, need, wait_ms,
                                             d["sleep"])
            if found < need:
                logger.info("Still nothing after %.0fs (%d match(es) for %s).",
                            wait_ms / 1000.0, found, sel)
            html = _snapshot(session, url) or html
            state = page_flow.classify(html, None, page.url)

        # The paid path is reached only for state "challenge" — Cloudflare's
        # Managed Challenge, which IS a test. It is NOT reached for
        # "blocked": that page carries no widget, so a solve there would be a
        # charge for nothing. Mirrors the other two engines.
        #
        # The paid path is reached only for state "challenge", which no
        # capture of this site has ever produced. Wired up because a bot
        # manager can be switched on between deploys, and bounded by
        # SOLVES_PER_PAGE so a speculative path cannot become a bill.
        if (page_flow.should_solve(state)
                and _solve_budget(args, solves_bought)):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)
                html = _content(session) or html
                state = page_flow.classify(html, url=page.url)
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid — so retrying it
            # would re-confirm the same right answer, and rotating the exit
            # would blame an address for the URL it was given.
            break

        # Blocked or challenged. The ADDRESS is what was scored, not the URL,
        # so a different exit is the only thing that plausibly changes the
        # outcome.
        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)

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
        # Mercor has never refused this scraper — see
        # playwright_scraper's twin of this block. This is the HARD refusal.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "Mercor did not serve this request — %d bytes, its own asset "
            "paths referenced %d time(s), saved to %s. This is NEW: on "
            "2026-09-18 this site served every route to a bare "
            "datacentre address with no challenge of any kind, so there "
            "is no measured remedy to recommend — the saved HTML is the "
            "evidence for what changed. This is exit 3, distinct from an "
            "empty result (exit 4).",
            len(html or ""), references_own_assets(html or ""), debug_html)
        outcome.blocked_by = "cloudflare (hard block)" if html else "no-response"
        outcome.final_url = page.url
        return outcome

    # No readiness wait and no scroll on the content path, and their absence
    # is MEASURED rather than forgotten — see the "unknown" branch above.

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED. An EMPTY
    # page is a correct answer, and a live run of a /p/<slug> hub
    # reported exit 3 on a page the site had plainly served because the
    # hub's own performance script names `akamaihd.net`. Mirrors
    # playwright_scraper exactly.
    vendor = (detect_bot_challenge(html, url=page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    if not page_flow.should_parse(state):
        logger.info("Page %d came back as %s; nothing to parse.", page_num,
                    state)
        outcome.final_url = page.url
        return outcome

    products, listing = _parse_for_mode(html, page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if listing is not None:
        # Recorded on every page so all three engines carry one shape; on
        # recorded every time and page 1's copy is what the run plans against.
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
                        "JSON-LD indexes %d.", listing.records_in_payload,
                        listing.urls_in_itemlist)
        if listing.page_repeated:
            # The site clamped an out-of-range page back and answered 200.
            logger.info("Asked for page %s and Mercor answered with page "
                        "%s — that is this site's way of saying the listing "
                        "has ended.", listing.requested_page,
                        listing.echoed_page)

    if products:
        for field_name in _core_fields(args.mode):
            filled = sum(1 for row in products
                         if getattr(row, field_name, None) not in (None, "", []))
            share = 100.0 * filled / len(products)
            if share < CORE_FIELD_FLOOR:
                logger.warning(
                    "Only %.0f%% of page %d carries `%s`, against a measured "
                    "floor of %d%%. Every record of every capture had one, so "
                    "this is the payload shape moving rather than the "
                    "listings being unusual — re-run with --dump-html.",
                    share, page_num, field_name, CORE_FIELD_FLOOR)
        paid = sum(1 for row in products if row.rate_min is not None)
        remote = sum(1 for row in products
                     if (row.work_arrangement or "").lower() == "remote")
        described = sum(1 for row in products if row.description)
        named = sum(1 for row in products if row.company_name)
        # All reported, none floored. `company_name` is the one that would
        # tempt a floor and must not have one: 291 of 390 listings withhold
        # the client's name on purpose, and `company_brand_visible` says so
        # per row. A floor there would fire on healthy data every run.
        if args.mode == "job":
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
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = page.url
    return outcome


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # All three modes are one row per business-at-a-location.
    dedupe_key = "sku"
    # `/explore` and `/careers` each hold their whole result set in one
    # response — measured, not assumed: every pagination parameter tried on
    # `/explore` returned a byte-identical payload on 2026-09-18. --mode job
    # is the paginated mode, and its pages are enumerated job URLs.
    stop_reason = ("single_page_route" if args.mode in ("listings", "careers")
                   else "completed")

    # How many jobs --mode job found to fetch. Bound here rather than inside
    # the session block, so the reporting path cannot raise UnboundLocalError
    # on a run that had already done all its work.
    total_enumerated = 0

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    bridge = _AsyncBridge()
    session = None
    try:
        session = _Session(bridge, args, pool).open()

        target = _target_url(args)

        # In --mode job the enumeration comes FIRST, because it is what
        # decides which address page 1 even is. Everywhere else page 1's own
        # content decides how many pages there are, so the order reverses.
        job_urls = []
        if args.mode == "job" and not _is_single_job_url(args):
            job_urls = _enumerate_jobs(
                lambda u: _fetch_page_source(session, u), args)
            if not job_urls:
                return finish_run(
                    [], args.out, args.format, args.allow_empty,
                    blocked=False, stop_reason="enumeration_empty",
                    pages_requested=args.pages, pages_completed=0,
                    mode=args.mode, source=SOURCE_DEFAULT,
                    start_url=args.url or "", final_url="")
            total_enumerated = len(job_urls)
            target = job_urls[0]

        if target != args.url:
            logger.info("Fetching %s", target)

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

            # Planned through the SHARED helper, so all three engines decide
            # this identically: one page for a route that has one, and one
            # page per enumerated job in --mode job.
            page_one = first.final_url or target
            planned = _plan_page_urls(args, page_one, first.pages_available,
                                      job_urls=job_urls)
            if len(planned) + 1 < args.pages:
                stop_reason = "page_cap_reached"

            for index, url in enumerate(planned):
                page_num = index + 2
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

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break

                if index + 1 < len(planned):
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()
        bridge.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    if args.domain:
        # A CLIENT-SIDE filter, applied after the merge and never presented
        # as anything else: Mercor's index accepts no query parameters, so
        # this cannot reduce what is downloaded and the log says so.
        wanted = args.domain.strip().lower()
        before = len(all_rows)
        all_rows = [r for r in all_rows
                    if (getattr(r, "domain", None) or "").strip().lower() == wanted]
        logger.info("--domain %r kept %d of %d row(s). This filtered rows "
                    "that were already fetched.", args.domain,
                    len(all_rows), before)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page" as a hard expectation: the LAST page of a
    # listing is legitimately short. Mirrors the other two engines.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    pages_available = next((o.pages_available for o in outcomes
                            if o.pages_available is not None), None)
    if args.mode == "role" and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A landing page carries the jobs of twenty companies, "
                "which varies, so this is a soft signal.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("This /explore response held %d listing(s) and this "
                        "run holds %d of them. /explore is not everything "
                        "Mercor publishes — 72 of the 462 listings "
                        "enumerated on 2026-09-18 appeared only in the "
                        "sitemap. Use --mode job to cover those.",
                        total_available, len(all_rows))

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    # Byte-identical in shape to the other two engines: the site's own
    # counts, which describe the LISTING rather than any job. `total_jobs`
    # beside `total_companies` is what stops a reader dividing rows by pages
    # — Mercor publishes no catalogue total on any route.
    # One-per-run context, in the sidecar rather than repeated down a column,
    # and byte-identical in shape to the other two engines. Mercor states no
    # catalogue total on any route, so what goes here is this run's OWN
    # coverage arithmetic — which is what a bare "complete" hides (§21).
    indexed = next((o.urls_in_itemlist for o in outcomes
                    if o.urls_in_itemlist is not None), None)
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
                    "edition). pyppeteer is effectively unmaintained — "
                    "playwright_scraper.py is the primary engine.")
    p.add_argument("--url", default=None,
                   help="A mercor.com URL: the marketplace index "
                        "(https://work.mercor.com/explore), one listing "
                        "(https://work.mercor.com/jobs/list_.../slug) with "
                        "--mode job, or Mercor's own openings "
                        "(https://www.mercor.com/careers) with --mode "
                        "careers. OPTIONAL — every mode has a correct "
                        "default. Also read from MERCOR_URL in the "
                        "environment or .env.")
    p.add_argument("--domain", default=None, metavar="NAME",
                   help="Keep only listings whose `domain` matches, case "
                        "insensitively. Filters rows AFTER they are fetched "
                        "and is honest about it: Mercor's index accepts no "
                        "query parameters, so nothing here reduces what is "
                        "downloaded. --mode listings and --mode job only.")
    p.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE,
                   help="listings (default): work.mercor.com/explore, the "
                        "marketplace index — 390 contract roles in ONE "
                        "response; --pages does not apply. job: one listing "
                        "per page, enumerated from the sitemap and the index "
                        "(462 jobs measured 2026-09-18); the only mode that "
                        "covers the listings the index does not show. "
                        "careers: www.mercor.com/careers, Mercor's OWN "
                        "salaried openings via Ashby — a different "
                        "population with its own id space.")
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar. Defaults "
                        "to what the run selects. Mercor's routes carry no "
                        "category segment, so this names the RUN, not a site "
                        "concept — the site's own category is the `domain` "
                        "column.")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to fetch (--mode role only). A run "
                        "plans against the `pageCount` the site states on "
                        "page 1 and never asks past it: page 48 of a 47-page "
                        "listing answers HTTP 200 with page 1 AGAIN.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and IGNORED here: parallel "
                        "page fetching lives in playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: an empty "
                        "hub category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="mercor_jobs", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. "
                        "Credentials are sent over CDP (page.authenticate), "
                        "never on the browser's command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note that "
                        "NO challenge has ever been observed on this site — a "
                        "refused request gets no page at all — so neither "
                        "setting has anything to act on today, and neither "
                        "helps with a refusal.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so a credentialed Scraping "
                        "Browser endpoint works here.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US), passed to Chromium "
                        "as --lang. It does NOT decide which directory is "
                        "searched; that is find_country in the URL.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's "
                        "Fingerprint API and apply it to the launched "
                        "browser. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint, where the Scraping Browser supplies "
                        "its own.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium 117, which runs "
                        "under Rosetta far enough to print --version and then "
                        "fails to open its DevTools socket (measured "
                        "2026-09-08; the same failure occurs with no wrapper "
                        "code at all, so it is the build, not this engine). "
                        "Point it at a Chrome or Chromium of your own — "
                        "Playwright's, if you have it installed.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)

    # Spelled identically to the other two engines.
    #
    # Every mode has a correct default URL, so there is nothing to refuse
    # about a missing one and no pair of flags that could disagree: Mercor's
    # index accepts no parameters, so there is no --role/--location
    # equivalent that could contradict a path.
    if args.url:
        ok, why = is_supported_url(args.url)
        if not ok:
            p.error(why)
        implied = mode_for_url(args.url)
        if implied and implied != args.mode:
            # Refused rather than silently corrected: reading a careers URL
            # under --mode listings would parse it with the wrong reader and
            # report zero rows for a perfectly good page (CLAUDE.md §20).
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
        # Said out loud rather than silently ignored. Measured, not assumed:
        # every pagination parameter tried on /explore returned a
        # byte-identical payload on 2026-09-18.
        logger.warning("--pages %d is ignored in --mode %s: the route holds "
                       "its whole result set in ONE response and honours no "
                       "pagination parameter. --mode job is the mode that "
                       "pages — there a page is one job.",
                       args.pages, args.mode)
        args.pages = 1

    if args.category is None:
        args.category = (category_from_url(args.url) if args.url
                         else args.mode)
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if ("profile_locked" in text or "connect to --cdp-endpoint" in text
                or "rejected WebSocket connection" in text):
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
