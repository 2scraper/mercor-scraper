"""
page_flow.py
------------
The retry / solve / blocked decision, as DATA rather than as three copies of
an if-chain (CLAUDE.md §1).

Mercor answers a request five ways, and four of them want a different
response:

    a page whose __NEXT_DATA__ holds records         -> parse
    a page whose __NEXT_DATA__ holds none            -> parse, it is an answer
    HTTP 404, or the site's own not-found copy       -> stop, it is not a block
    a page we could not find a payload in            -> parse_error, dump it
    something else entirely                          -> wait, then retry

Three copies of that triage across three engines would drift, and the drift
would be silent — one engine reporting exit 3 where its twin reports exit 0
on the same response.

There is no fifth state for a challenge on this site because, measured on
2026-09-18, Mercor rendered none: eighteen candidate vendor markers counted
across seven captures, all zero, and identical 200s from `curl`, from
`python-requests` and from an empty User-Agent. `blocked` is in the policy
table anyway — an ungated site today is not an ungated site forever, and a
run should be able to say "blocked" rather than "empty" on the day that
changes.

Nothing here imports a browser, and **no JavaScript crosses this boundary**:
Selenium's `execute_script` takes a function BODY with an explicit `return`
while Playwright and pyppeteer take `() => expr`, so a shared snippet would
quietly acquire one driver's dialect. The callbacks below are named for the
OPERATION instead, and each engine spells it in its own dialect (§1).
"""

import logging
from typing import Callable, Optional

from product_parser import (detect_bot_challenge,  # noqa: F401
                            detect_page_state, route_of)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

# How many job links mean "this response is the marketplace index".
#
# `> 1` on purpose, per CLAUDE.md §5: waiting for ONE match resolves on
# something unrelated — a nav link, a footer — long before the grid is really
# there.
#
# Four, and the ceiling on this number is lower than it looks. `/explore`
# holds 390 records in its payload and paints only **15** anchors into the
# served markup: the grid is virtualised, so the DOM never shows the whole
# catalogue however long anything waits for it. A readiness threshold
# anywhere near the record count would time out on every single run.
#
# Which is the other half of why this is a readiness signal ONLY. The rows
# come out of `__NEXT_DATA__`, which is complete in the first response, so a
# page whose grid has not painted at all still parses to all 390 rows — and
# the engines therefore do not gate parsing on this.
MIN_CARD_MATCHES = 4

# Anchored on a URL pattern, never a CSS class: Mercor's classes are
# build-generated Tailwind strings that churn on every deploy, while
# `/jobs/list_…` is a contract with search engines (§4).
#
# TWO spellings, and the second one is the whole reason this selector is
# not the obvious one-liner. **Hydration rewrites every job anchor.**
# Mercor server-renders `href="/jobs/list_{id}/{slug}?returnPath=/explore"`
# and React then replaces it with `href="/explore?listingId={id}"`, a
# same-page modal route. So, measured in a live headless Chromium on
# 2026-09-18:
#
#     a[href*="/jobs/list_"]        15 in the served HTML, 0 in the live DOM
#     a[href*="listingId=list_"]     0 in the served HTML, 16 in the live DOM
#     the two together              16 at domcontentloaded and every poll after
#
# The single-spelling version is the kind of mistake that does not look
# like one: the rows still come out right, because they are read from
# `__NEXT_DATA__` and not from the DOM. What it costs is invisible in the
# output and expensive everywhere else — the readiness wait can never be
# satisfied, so every page burns its full timeout, and the captcha gate
# sees "zero content on this page" and decides a detected challenge is
# BLOCKING. On the first live run of this engine that turned mercor's
# background reCAPTCHA v3 into "attempting to solve" on a page holding all
# 390 rows; with an API key set, that is a charge per page for nothing
# (CLAUDE.md §8 — detected is not blocking, and §19 — never pay for a
# challenge that is not stopping you).
#
# Anything anchored to the DOM on this site needs checking in a real
# browser and not in a capture. The capture is the pre-hydration document.
READY_SELECTOR_LISTING = ('a[href*="/jobs/list_"], '
                          'a[href*="listingId=list_"]')

# A detail page and the careers page each state their heading server-side,
# and `h1` was confirmed present on both in a live browser (1 each,
# 2026-09-18).
#
# The careers page gets `h1` rather than an anchor selector because it
# renders no job links at all that a selector could wait on: its 108 Ashby
# URLs live in the payload, and after hydration the live DOM still held
# `a[href*="ashbyhq.com"]` = 0. Waiting on those would wait forever.
READY_SELECTOR_JOB = "h1"
READY_SELECTOR_CAREERS = "h1"

CONTENT_TIMEOUT_MS = 45_000
CONTENT_TIMEOUT_MS_JOB = 30_000


def ready_selector(mode: str) -> str:
    if mode == "job":
        return READY_SELECTOR_JOB
    if mode == "careers":
        return READY_SELECTOR_CAREERS
    return READY_SELECTOR_LISTING


def min_matches(mode: str) -> int:
    return 1 if mode in ("job", "careers") else MIN_CARD_MATCHES


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS_JOB if mode in ("job", "careers") else CONTENT_TIMEOUT_MS


# How long to keep polling for an anchor, and how often.
#
# Polled through `count(selector)` — a callback each engine implements with
# its own `querySelectorAll` call — and NEVER by handing the browser a string
# to evaluate. CLAUDE.md §18: a site whose Content-Security-Policy omits
# `unsafe-eval` kills `wait_for_function` with an `EvalError` and takes the
# run down with exit 1, on the site's most obvious URL. Mercor has not been
# measured for that, and the cheap habit costs nothing on a site that would
# have allowed it.
READY_POLL_MS = 500


def wait_for_count(count: Callable[[str], int], selector: str, minimum: int,
                   timeout_ms: int, sleep_ms: Callable[[int], None]) -> int:
    """Poll `count(selector)` until it reaches `minimum` or the budget runs out.

    Returns the last count seen, so a caller can report "3 of 4 expected"
    rather than only that it timed out.
    """
    waited = 0
    seen = 0
    while waited <= timeout_ms:
        seen = count(selector)
        if seen >= minimum:
            return seen
        sleep_ms(READY_POLL_MS)
        waited += READY_POLL_MS
    return seen


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", mode: str = "listings") -> str:
    """Name what Mercor answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url)`, with `mode` keyword-only in practice. A
    sibling repo shipped `classify(html, url=...)` in two of three engines
    against a callee that took `status` second, and both crashed on their
    first fetch — invisible to import, `--help`, `compileall` and 400+ green
    offline assertions, because none of those calls a function the way a live
    run does (§17). `smoke_test.py` binds every engine's call against this
    signature for that reason.
    """
    return detect_page_state(html or "", status, url, mode)


STATE_POLICY = {
    # A page whose payload holds records.
    "content":     {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A page Mercor served whose payload holds no records. The site answered
    # exactly what was asked and there is nothing in it — a real answer, and
    # EXIT_NO_PRODUCTS rather than EXIT_BLOCKED. Reporting it as blocked
    # sends a user hunting for a proxy problem that is not there.
    "empty":       {"retry": False, "solve": False, "blocked": False, "parse": True},
    # Never yet observed on this site. `solve` is True so that a challenge
    # appearing tomorrow is met with the tools this repo already has rather
    # than with a code change; `retry` is True because on every other site
    # in this family a different exit clears a refusal far more cheaply than
    # a solve does.
    "blocked":     {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
    # A substantial page with no `__NEXT_DATA__` we could read. NOT "zero
    # jobs": a served page that parses to nothing is OUR bug, and reporting
    # it as an empty index sends the reader to check the URL instead of the
    # parser (CLAUDE.md §20). Worth one retry in case the document was
    # caught mid-swap, and always worth a dump.
    "parse_error": {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # A 404. Retrying an address that does not exist is pure waste, and it
    # is not a block — saying so stops a user rotating proxies over a
    # delisted job. This state is reachable in ordinary use here: `--mode
    # job` works from an enumeration that can outlive a listing, so a job
    # taken down between the sitemap being fetched and its page being read
    # lands exactly here.
    "not_found":   {"retry": False, "solve": False, "blocked": False, "parse": False},
    # Something that is neither the site nor a recognised refusal — an
    # upstream error page, a proxy's own response, Chromium's network-error
    # page. A wait, not a spend.
    "unknown":     {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all.
#
# True, and CONSULTED rather than merely documented — the engines read it,
# so setting it False really does stop the retry loop. (A sibling repo
# carried this constant with a paragraph of justification and no reader,
# which is the same defect as dead code that looks load-bearing: §17.)
#
# True is the cautious setting rather than a measured one here, and the
# honest statement of the evidence is that there is none: Mercor has never
# refused this scraper, so no retry has ever been exercised against a real
# refusal. On every site in this family that DOES refuse, a retry that moves
# the address is the retry worth making.
RETRY_ON_BLOCKED = True

# How many times to re-fetch a blocked page when there is no proxy pool to
# rotate into.
#
# One. Without a pool every retry leaves from the same address, and an
# address-level refusal answers a second identical request identically. WITH
# a pool the engines retry once per remaining exit instead, because there the
# retry changes the one variable the refusal would depend on.
BLOCK_RETRIES_WITHOUT_POOL = 1

# At most one solve per page. A challenge that survives a solved token is not
# a challenge this run can pass, and a second solve is a second charge for
# the same answer.
SOLVES_PER_PAGE = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pagination_is_addressable(url: str) -> bool:
    """Whether page N of this run can be fetched without walking to it.

    CLAUDE.md §18 says to ask this PER URL rather than per site, and on
    Mercor the routes give opposite answers for opposite reasons:

      * `/explore` is NOT addressable, because it has no page 2 at all.
        Measured 2026-09-18: `?page=2`, `?limit=10`, `?offset=100` and
        `?domain=Finance` each returned a byte-identical payload holding
        the same 390 records. The route ships the whole index in one
        response. A `page_url()` used here would fetch that payload again,
        find no new sku, conclude the index was exhausted and report a
        COMPLETE multi-page run holding page one several times.
      * `/careers` is not addressable either, for the same reason: 108
        records in one response and no second page.
      * A JOB URL is addressable, and this is where the word earns its
        keep. In `--mode job` a "page" is one job detail page, and those
        addresses are not guessed from a convention — they are read from
        `/sitemap.xml` and from the `/explore` payload before the run
        starts. Every one is a real, independent URL, which is precisely
        what the concurrency machinery needs and what makes the sidecar's
        "which pages failed by number" mean something.

    So a `listings` or `careers` run plans one page and says why, and a
    `job` run may use every worker it is given.
    """
    return route_of(url or "") == "job"


def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what the enumeration holds.

    Mercor states no total and no page count on any route, so unlike a site
    that hands over `totalPages` there is nothing to read off page 1. What
    `pages_available` carries instead depends on the mode, and both are
    facts rather than guesses:

      * `listings` and `careers`: 1, because the route has exactly one page.
      * `job`: the size of the enumeration — the union of `/sitemap.xml`
        and the `/explore` payload, 462 distinct listings on 2026-09-18.

    Capping at it matters in the `job` mode: asking for the 463rd job when
    the enumeration holds 462 is not an empty page, it is an IndexError
    waiting to happen, and there is no more catalogue to walk to.
    """
    wanted = max(1, int(pages_requested or 1))
    if pages_available and pages_available > 0:
        return min(wanted, int(pages_available))
    return wanted


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


def concurrency_for_mode(mode: str, concurrency: int) -> int:
    """Workers this mode can actually use.

    `listings` and `careers` fetch exactly one page, so any worker past the
    first would have nothing to do. Clamping here rather than letting the
    engine start idle threads keeps the run's own log honest about what it
    did — and CLAUDE.md §7 is explicit that concurrency without a proxy pool
    is a faster way to get an address scored than to gather data, which is a
    poor trade for threads that fetch nothing.
    """
    if mode in ("listings", "careers"):
        return 1
    return max(1, int(concurrency or 1))
