#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`.

    python3 diff_runs.py --old restaurants.2026-09-01.json \\
                          --new restaurants.2026-09-07.json

Typical use is a scheduled re-run kept under a dated filename, diffed against
the previous one:

    python3 playwright_scraper.py --text restaurants --location "New York, NY" \\
        --out "restaurants_$(date +%F)"
    python3 diff_runs.py --old "restaurants_$(ls -t restaurants_*.json | sed -n 2p)" \\
                          --new "restaurants_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new: the listing was
                   filled or withdrawn, or simply fell outside the pages this
                   run fetched
  changed        — sku present in both, with a different title, salary or
                   equity range, job type, remote configuration, location, or
                   company size. See TRACKED_FIELDS.
  source_changed — sku present in both, but one row came from a LANDING run
                   (`apollo`) and the other from a JOB page (`jsonld`), and
                   they differ on a column only one of the two fills.
                   Reported separately because this says something about our
                   own two snapshots rather than about the job — and
                   --fail-on-change deliberately ignores it.

TWO THINGS TO KNOW BEFORE READING A DIFF OF THIS SITE
-----------------------------------------------------
**`removed` does not mean filled.** A landing listing paginates over
COMPANIES, twenty per page, and /role/r/software-engineer reported 47 pages
of them — so a two-page run holds a slice, and a job can leave the file
because the slice moved rather than because anything happened to the job.
Two runs are only comparable as a census when both fetched the same URL to
the same depth.

Mercor publishes no catalogue total on any route, so there is no site-stated
figure to drift — what a run records instead is its own coverage
(`records_in_payload`, `jobs_enumerated`, `jobs_fetched`).

**`position` and `page` are deliberately not tracked, and here that is a
choice rather than a necessity.** Unlike a sibling site whose serialisation
order wanders between requests, Mercor's `/explore` order was measured
STABLE on 2026-09-18: three fetches about ten seconds apart returned the
identical 390 ids in the identical order, 0 positions differing.

So a position change on this site would be a real change and not noise —
and it is still not diffed, for a reason worth stating rather than
inheriting. Ten seconds is evidence about ten seconds. It says nothing
about whether the ranking is stable across a night, which is the interval
a nightly diff actually spans, and a column that reports churn on every run
teaches a reader to ignore the diff. If someone measures the overnight case
and finds it stable, tracking `position` becomes a defensible change; until
then this is the conservative default and the measurement above is what a
future decision should be argued from.

A row this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# What is worth watching on a job board, and nothing else.
#
# A price monitor's fields are absent because a job has no price — porting
# them would be dead code that looks load-bearing (CLAUDE.md §4). What
# changes on a job listing is its PAY, its TERMS and the COMPANY behind it,
# and those are what anyone diffs a job board for.
#
# `locations`, `remote_locations` and `company_badges` are lists and compare
# element-wise, which is what you want: a company losing its "Actively
# Hiring" badge is a real change.
#
# Deliberately NOT tracked: `position` and `page`, and `scraped_at`. See the
# module docstring for the measurement.
TRACKED_FIELDS = (
    # the job
    "title", "job_type", "primary_role", "years_experience_min",
    "years_experience_max", "posted_at",
    # the pay
    "compensation", "salary_min", "salary_max", "salary_currency",
    "salary_period", "equity_min", "equity_max", "has_equity",
    # where
    "locations", "remote", "remote_kind", "wfh_flexible", "remote_locations",
    # the company
    "company_name", "company_size", "company_tagline", "company_badges",
    # only a job-page run fills these; see DETAIL_ONLY_FIELDS
    "benefits", "industry",
)

# The subset that only ONE of the two sources populates.
#
# The split runs both ways on this site, which is why it is worth stating.
# A landing row (`apollo`) has the equity range, the company's badges, size
# and tagline, and no salary period. A job-page row (`jsonld`) has the
# period, the benefits and the industry, and no equity at all — schema.org
# has no expression for it. So diffing a landing run against a job run would
# report each of these as a change on every row, and none of it would be
# about the job. When the two rows disagree on `data_source`, they are
# reported as `source_changed` rather than as changes (§8: a difference that
# comes with a provenance difference says something about our own two
# snapshots, not about the site).
DETAIL_ONLY_FIELDS = (
    "benefits", "industry", "salary_period",
    "equity_min", "equity_max", "has_equity",
    "company_size", "company_tagline", "company_badges",
)
# Kept as an alias so a caller written against the family's older name still
# works; the two are the same tuple.
PROFILE_ONLY_FIELDS = DETAIL_ONLY_FIELDS


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def diff_products(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed = [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose `data_source` differs between runs is not comparable on
        # the profile-only columns: a listing row leaves them null and a
        # profile row fills them, so every one of them would read as a change
        # and none of it would be about the business. Reporting it as a
        # change would be a false alarm about the site; the other columns
        # still compare fine.
        sources = (before.get("data_source"), after.get("data_source"))
        if sources[0] != sources[1] and any(f in field_changes
                                            for f in PROFILE_ONLY_FIELDS):
            profile_part = {f: v for f, v in field_changes.items()
                            if f in PROFILE_ONLY_FIELDS}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in PROFILE_ONLY_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "data_source": {"old": sources[0], "new": sources[1]},
                "changes": profile_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable across run kinds.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  "
              f"@ {p.get('company_name') or '?'}  "
              f"{p.get('compensation') or 'pay not stated'}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  "
              f"@ {p.get('company_name') or '?'}  "
              f"{p.get('compensation') or 'pay not stated'}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result["source_changed"]:
        src = c["data_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[data_source {src['old']!r} -> {src['new']!r}: a listing row "
              f"leaves these columns null and a profile row fills them, so "
              f"this is not a change in the business]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # price. A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku on price, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        kinds = set(modes.values())
        # `careers` against either marketplace mode is the severe case and
        # deserves its own sentence: the two share NO ids at all, so every
        # row would be reported as both added and removed. `listings`
        # against `job` is milder — same id space, different columns — but
        # still describes the mode change rather than the catalogue.
        if "careers" in kinds and kinds - {"careers"}:
            problems.append(
                f"the two runs are different POPULATIONS ({modes}). Mercor's "
                f"own openings are Ashby records with UUID ids; marketplace "
                f"listings are `list_…` ids. The two sets have no id in "
                f"common, so every row would be reported as both added and "
                f"removed.")
        else:
            problems.append(
                f"the two runs are different modes ({modes}). An index row "
                f"and a detail row carry different columns — the index has "
                f"`domain` and the slot counters, a detail row has the "
                f"company website and a currency — so `added`/`removed` "
                f"would describe the mode change rather than the catalogue.")

    # NO SORT GUARD, and that absence is a measurement rather than an
    # omission. A sibling repo in this family needs one because its default
    # ordering is a paid placement that changes WHICH rows are in the file.
    # Mercor offers no ordering control at all: `/explore` accepts no query
    # parameter of any kind — `?page`, `?limit`, `?offset` and `?domain`
    # each returned a byte-identical payload on 2026-09-18 — so there is no
    # sort to disagree about and no `sort` column on the row. A guard here
    # would have no input and would pass for the wrong reason (§22).
    #
    # What DOES change which rows are in a file here is the MODE, which is
    # guarded above, and in `--mode job` how many pages were fetched out of
    # the enumeration, which the note below reports.

    # And whether either run was CAPPED, which changes what `removed` means.
    for label, path in (("--old", args.old), ("--new", args.new)):
        _, meta = _run_status(path)
        meta = meta or {}
        enumerated = meta.get("jobs_enumerated")
        fetched = meta.get("jobs_fetched")
        if enumerated and fetched and fetched < enumerated:
            print(f"[i] {label} ({path}) fetched {fetched} of the "
                  f"{enumerated} job(s) it enumerated — a complete run, and "
                  f"a slice. A `removed` line may mean the slice moved "
                  f"rather than that a listing was taken down. Use --pages "
                  f"{enumerated} on both runs to compare a full census.")
        # And the one that applies to every --mode listings run, capped or
        # not: /explore is not the whole site. 72 of the 462 listings
        # enumerated on 2026-09-18 appeared only in the sitemap, so a
        # `removed` line in a listings diff can mean "this listing left the
        # index", which is not the same as "this job is gone". CLAUDE.md
        # §21 — complete and exhaustive are different words.
        if meta.get("mode") == "listings" and meta.get("records_in_payload"):
            print(f"[i] {label} ({path}) is a /explore run: it holds every "
                  f"listing the index serves, which is not every listing "
                  f"Mercor publishes. A `removed` line may mean a listing "
                  f"left the index rather than that it was filled. "
                  f"--mode job covers the rest.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include jobs that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two mercor-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # `source_changed` is not a reason to fail: it means one row came from a
    # listing run and the other from a profile run, so the columns only a
    # profile fills differ. That says something about our own two snapshots
    # rather than about the business, and alerting on it would train whoever
    # reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
