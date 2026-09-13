#!/usr/bin/env python3
"""Pull gastric-cancer items from a list of RSS feeds into news.json.

Usage:
    python fetch_news.py            fetch and merge into news.json
    python fetch_news.py --check    test every feed URL, write nothing
    python fetch_news.py --loose    ignore the keyword filter (see what's there)
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

ROOT = Path(__file__).parent
SOURCES = ROOT / "sources.json"
NEWS = ROOT / "news.json"

UA = "gastric-watch/1.0 (personal news aggregator)"
UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 20

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref$|source$)")


# ---------------------------------------------------------------- helpers

def clean_html(raw):
    """Strip tags and entities, collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def trim_sentences(text, max_sentences=2, max_chars=280):
    """Keep it to roughly two sentences. A blurb is a reason to click."""
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    return out


def canonical_url(url):
    """Drop tracking params and fragments so the same article dedupes."""
    if not url:
        return ""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = "&".join(
        p for p in s.query.split("&")
        if p and not TRACKING_PARAMS.match(p.split("=")[0].lower())
    )
    host = s.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = s.path.rstrip("/") or "/"
    return urlunsplit((s.scheme.lower() or "https", host, path, query, ""))


def title_key(title):
    """Normalised title, for catching the same story across outlets."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def source_allowed(outlet, url, config):
    """Keep medical/scientific outlets, drop consumer and lifestyle press.

    Google News links redirect, so the domain is often useless — the outlet name
    derived from the headline is the reliable signal there.
    """
    name = (outlet or "").strip().lower()
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]

    allowed = [a.lower() for a in config.get("allowed_sources") or []]
    if allowed:
        return any(a == name or a in name or a in host for a in allowed)

    for bad in config.get("blocked_sources") or []:
        bad = bad.lower()
        if bad == name or bad in name:
            return False
    for domain in config.get("blocked_domains") or []:
        domain = domain.lower()
        if host == domain or host.endswith("." + domain):
            return False
    return True


def _matches(haystack, terms):
    """Whole-word / whole-phrase match, so 'GEJ' doesn't fire inside another word."""
    for term in terms:
        pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, haystack):
            return term
    return None


def is_relevant(item, config):
    """Three gates: the disease, then the setting, then the exclusions."""
    title = (item["title"] or "").lower()
    haystack = f"{item['title']} {item.get('blurb', '')}".lower()

    in_title = config.get("disease_in_title", True) and not item.get("relax_title_rule")
    scope = title if in_title else haystack
    if not _matches(scope, config.get("keywords", [])):
        return False

    context = config.get("context_keywords") or []
    if context and not _matches(haystack, context):
        return False

    excluded = _matches(haystack, config.get("exclude_keywords") or [])
    if excluded:
        # "unresectable" is the setting we want; "resectable" alone is not.
        if excluded == "resectable" and _matches(haystack, ["unresectable"]):
            return True
        return False

    return True


def entry_date(entry):
    """Publication date as an aware UTC datetime, or None."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


META_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S),
    re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S),
    re.compile(r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', re.I | re.S),
]


def meta_description(url, session):
    """Last resort: the article page's own share blurb."""
    try:
        resp = session.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
        if resp.status_code != 200:
            return ""
        head = resp.text[:200_000]
    except requests.RequestException:
        return ""
    for pattern in META_PATTERNS:
        match = pattern.search(head)
        if match:
            return clean_html(match.group(1))
    return ""


def build_blurb(entry, url, session, allow_fetch):
    """Publisher's own words, in descending order of preference."""
    for candidate in (entry.get("summary"), entry.get("description")):
        text = clean_html(candidate)
        if len(text) > 40:
            return trim_sentences(text)

    for block in entry.get("content") or []:
        text = clean_html(block.get("value"))
        if len(text) > 40:
            return trim_sentences(text)

    if allow_fetch and url:
        text = meta_description(url, session)
        if len(text) > 40:
            return trim_sentences(text)

    return ""


# ---------------------------------------------------------------- main flow

def collect(config, loose=False):
    session = requests.Session()
    allow_fetch = config.get("fetch_meta_description", True)
    items, problems = [], []

    for feed in config["feeds"]:
        if not feed.get("enabled", True):
            continue
        name, url = feed["name"], feed["url"]
        try:
            resp = session.get(url, timeout=TIMEOUT, headers={
                "User-Agent": UA_BROWSER if feed.get("browser_agent") else UA,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            })
            if resp.status_code != 200:
                problems.append(f"{name}: HTTP {resp.status_code}")
                continue
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            problems.append(f"{name}: {exc}")
            continue

        if getattr(parsed, "bozo", 0) and not parsed.entries:
            problems.append(f"{name}: could not parse ({parsed.get('bozo_exception')})")
            continue
        if not parsed.entries:
            problems.append(f"{name}: parsed but returned no entries")
            continue

        kept = 0
        blocked = 0
        for entry in parsed.entries:
            title = clean_html(entry.get("title"))
            link = canonical_url(entry.get("link"))
            if not title or not link:
                continue

            outlet = name
            if feed.get("derive_source"):
                split = re.match(r"^(.*?)\s+[-\u2013]\s+([^-\u2013]{2,40})$", title)
                if split:
                    title, outlet = split.group(1).strip(), split.group(2).strip()

            when = entry_date(entry)
            if not source_allowed(outlet, link, config):
                blocked += 1
                continue

            item = {
                "title": title,
                "url": link,
                "source": outlet,
                "published": when.isoformat() if when else None,
                "blurb": "",
            }
            if feed.get("relax_title_rule"):
                item["relax_title_rule"] = True

            # Cheap keyword check on title plus the raw feed summary, so we
            # only pay for a page fetch on items we're likely to keep.
            preview = clean_html(entry.get("summary") or entry.get("description"))
            probe = {
                "title": title,
                "blurb": preview,
                "relax_title_rule": feed.get("relax_title_rule", False),
            }
            if not loose and not is_relevant(probe, config):
                continue

            item["blurb"] = build_blurb(entry, link, session, allow_fetch)
            items.append(item)
            kept += 1

        summary = f"  {name}: {len(parsed.entries)} entries, {kept} relevant"
        if blocked:
            summary += f", {blocked} dropped on source"
        print(summary)

    return items, problems


def merge(existing, fresh, config):
    """Keep first-seen dates stable so nothing jumps the queue on a re-stamp."""
    by_url = {}
    by_title = {}
    now = datetime.now(timezone.utc)

    for item in existing + fresh:
        url = item["url"]
        tkey = title_key(item["title"])
        prior = by_url.get(url) or by_title.get(tkey)

        if prior:
            # Same story. Keep the earliest date we've ever seen for it, and
            # fill in a blurb if we previously had none.
            for field in ("published", "first_seen"):
                old, new = prior.get(field), item.get(field)
                if new and (not old or new < old):
                    prior[field] = new
            if not prior.get("blurb") and item.get("blurb"):
                prior["blurb"] = item["blurb"]
            # Note additional outlets carrying the same story.
            others = set(prior.get("also_in", []))
            if item["source"] != prior["source"]:
                others.add(item["source"])
            if others:
                prior["also_in"] = sorted(others)
            continue

        item.setdefault("first_seen", now.isoformat())
        by_url[url] = item
        by_title[tkey] = item

    merged = list(by_url.values())

    cutoff = now - timedelta(days=config.get("retention_days", 180))
    def sort_date(item):
        raw = item.get("published") or item.get("first_seen")
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return now

    merged = [i for i in merged if sort_date(i) >= cutoff]
    merged.sort(key=sort_date, reverse=True)
    return merged[: config.get("max_items", 400)]


def check_feeds(config):
    session = requests.Session()
    print("Checking every feed URL.\n")
    ok = True
    for feed in config["feeds"]:
        name, url = feed["name"], feed["url"]
        state = "on " if feed.get("enabled", True) else "off"
        try:
            agent = UA_BROWSER if feed.get("browser_agent") else UA
            resp = session.get(url, timeout=TIMEOUT, headers={"User-Agent": agent})
            parsed = feedparser.parse(resp.content)
            count = len(parsed.entries)
            if resp.status_code != 200:
                print(f"  [{state}] {name}: HTTP {resp.status_code}  <- broken")
                ok = False
            elif count == 0:
                print(f"  [{state}] {name}: 200 but no entries  <- probably wrong URL")
                ok = False
            elif count > 500:
                print(f"  [{state}] {name}: {count} entries  <- not a news feed, disable this")
                ok = False
            else:
                newest = parsed.entries[0].get("title", "")[:60]
                print(f"  [{state}] {name}: {count} entries, newest \"{newest}…\"")
        except requests.RequestException as exc:
            print(f"  [{state}] {name}: {exc}  <- broken")
            ok = False
    print("\nEdit sources.json to fix or disable anything broken.")
    return ok


def main():
    config = json.loads(SOURCES.read_text())

    if "--check" in sys.argv:
        check_feeds(config)
        return

    dump = "--quiet" not in sys.argv
    loose = "--loose" in sys.argv
    if loose:
        print("Keyword filter off — showing everything the feeds carry.\n")

    print("Fetching feeds.")
    fresh, problems = collect(config, loose=loose)

    existing = []
    if NEWS.exists():
        try:
            existing = json.loads(NEWS.read_text()).get("items", [])
        except json.JSONDecodeError:
            print("  news.json was unreadable, starting fresh")

    if dump:
        print("\nEverything that passed the filter:\n")
        by_outlet = {}
        for item in fresh:
            by_outlet.setdefault(item["source"], []).append(item["title"])
        for outlet in sorted(by_outlet, key=lambda o: -len(by_outlet[o])):
            print(f"  {outlet} ({len(by_outlet[outlet])})")
            for t in by_outlet[outlet]:
                print(f"      {t[:110]}")
        print()

    # Re-apply the current filters to everything already saved, so tightening a
    # rule or blocking an outlet clears out what it let through before.
    if not loose:
        kept_existing = [
            item for item in existing
            if source_allowed(item.get("source"), item.get("url", ""), config)
            and is_relevant(item, config)
        ]
        pruned = len(existing) - len(kept_existing)
        if pruned:
            print(f"Removed {pruned} saved item(s) that no longer pass the filters")
        existing = kept_existing

    merged = merge(existing, fresh, config)
    added = len(merged) - len(existing)

    NEWS.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(),
        "items": merged,
    }, indent=1, ensure_ascii=False))

    print(f"\n{len(merged)} items in news.json ({added:+d} this run)")
    if problems:
        print("\nFeeds that didn't work:")
        for line in problems:
            print(f"  {line}")
        print("Run with --check for detail.")


if __name__ == "__main__":
    main()
