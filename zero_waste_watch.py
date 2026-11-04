#!/usr/bin/env python3
"""
Zero Waste Bag watcher for Deliveroo HOP (UAE)

Looks at your Deliveroo HOP store page and sends a notification to your phone
(through the free ntfy app) when a Zero Waste Bag is listed and in stock.
Every appearance is logged to sightings.csv, so you can see when bags have
dropped, how fast they sell out and whether it's checking often enough.

Needs Python 3.8 or newer and nothing else.

    python3 zero_waste_watch.py --check       one look; prints what it sees, sends nothing
    python3 zero_waste_watch.py --test-push   sends a test notification to your phone
    python3 zero_waste_watch.py               keeps watching until you stop it (Ctrl+C)
    python3 zero_waste_watch.py --once        one check that can notify (used by GitHub Actions)
    python3 zero_waste_watch.py --report      when bags dropped, how fast they went, whether checks keep up

Every setting below can also be given as an environment variable of the same name.
It only reads the public store page: it never logs in, adds to basket or orders.
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Your HOP store page. On deliveroo.ae (in a browser), set your address, open
# "24/7 Deliveroo HOP with Choithrams" and copy the link from the address bar.
# To watch more than one store, separate the links with commas.
HOP_URLS = os.environ.get("HOP_URLS") or "PASTE_YOUR_HOP_LINK_HERE"

# The ntfy channel your phone is subscribed to. Anyone who knows it can send you
# notifications, so keep it hard to guess. On GitHub, set it as the NTFY_TOPIC
# secret; only paste it here when running on your own computer, and never in a
# public repo.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or ""
NTFY_SERVER = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"

# How often to look when left running, in minutes. Be gentle with Deliveroo.
CHECK_EVERY_MINUTES = float(os.environ.get("CHECK_EVERY_MINUTES") or "10")

# No notifications during these hours (Dubai time). A bag that is still listed
# when they end is announced then. Set to "" to allow notifications at any hour.
QUIET_HOURS = os.environ.get("QUIET_HOURS", "23-07")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(HERE, "state.json")
LOG_FILE = os.environ.get("LOG_FILE") or os.path.join(HERE, "sightings.csv")
PAGE_COPY = os.path.join(HERE, "last_page.html")

DUBAI = timezone(timedelta(hours=4))  # UAE time; no daylight saving

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)


# ---------------------------------------------------------------------------
# Fetching the page
# ---------------------------------------------------------------------------

def fetch(url, timeout=30):
    """Return (http_status, html, error_text). Status 0 means no response at all."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_body(response), ""
    except urllib.error.HTTPError as error:
        try:
            body = _read_body(error)
        except Exception:
            body = ""
        return error.code, body, "HTTP %s" % error.code
    except Exception as error:  # no internet, DNS failure, timeout...
        return 0, "", str(getattr(error, "reason", error))


def _read_body(response):
    raw = response.read()
    if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, "replace")


# ---------------------------------------------------------------------------
# Finding the bag in the page
# ---------------------------------------------------------------------------

BAG_NAME = re.compile(r"zero[\s\-_]*waste", re.I)
NAME_KEYS = ("name", "title", "label", "displayName", "itemName", "productName")
PRICE_KEYS = ("priceDiscounted", "discountedPrice", "salePrice", "finalPrice",
              "price", "displayPrice", "formattedPrice", "offers")
SOLD_OUT_TEXT = re.compile(r"sold\s*out|unavailable|out\s*of\s*stock|not\s*available", re.I)
AED_PRICE = re.compile(r"(?:AED|Dhs?)\s?\d[\d,]*(?:\.\d{1,2})?", re.I)

# A JSON key/value such as "name":"Zero Waste Bag (Minimum value AED 100)"
JSON_NAME = re.compile(
    r'"(?:name|title|label|displayName|itemName|productName)"\s*:\s*'
    r'"((?:[^"\\]|\\.){0,200}?zero[\s\-_]*waste(?:[^"\\]|\\.){0,200}?)"', re.I)
SCRIPT_JSON = re.compile(
    r'<script\b[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>', re.I | re.S)
NEXT_FLIGHT = re.compile(r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)', re.S)

_DECODER = json.JSONDecoder()

_TRUE_MEANS_AVAILABLE = {"available", "isavailable", "instock", "isinstock",
                         "purchasable", "ispurchasable", "orderable", "isorderable"}
_TRUE_MEANS_GONE = {"soldout", "issoldout", "outofstock", "isoutofstock",
                    "unavailable", "isunavailable", "hidden", "ishidden"}
_COUNT_KEYS = {"stock", "stocklevel", "stockquantity", "quantityavailable",
               "availablequantity", "inventory", "remaining"}
_STATUS_KEYS = {"availability", "status", "stockstatus", "availabilitystatus"}


def _walk(obj):
    """Every dict inside a parsed JSON value."""
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _name_of(d):
    for key in NAME_KEYS:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def _looks_like_product(d):
    return _name_of(d) is not None and any(
        "price" in str(key).lower() or key == "offers" for key in d)


def _price_text(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        if isinstance(value, int) and value >= 1000:  # probably fils, e.g. 5000 = AED 50.00
            return "AED %.2f" % (value / 100)
        return "AED %g" % value
    if isinstance(value, dict):
        for key in ("formatted", "formattedPrice", "display", "displayValue", "text"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        currency = (value.get("code") or value.get("currency") or value.get("currencyCode")
                    or value.get("priceCurrency") or "AED")
        if isinstance(value.get("fractional"), (int, float)):
            return "%s %.2f" % (currency, value["fractional"] / 100)
        for key in ("amount", "value", "price"):
            amount = value.get(key)
            if isinstance(amount, (int, float, str)) and not isinstance(amount, bool):
                return "%s %s" % (currency, amount)
    if isinstance(value, list):
        for part in value:
            text = _price_text(part)
            if text:
                return text
    return None


def _price_of(d):
    for key in PRICE_KEYS:
        if key in d:
            text = _price_text(d[key])
            if text:
                return text
    return None


def _availability(d, depth=0):
    """True or False if the page data says whether it's in stock; None if it doesn't say."""
    simple = {re.sub(r"[^a-z]", "", str(k).lower()): v for k, v in d.items()}
    for key, value in simple.items():
        if isinstance(value, bool):
            if key in _TRUE_MEANS_AVAILABLE:
                return value
            if key in _TRUE_MEANS_GONE:
                return not value
    for key, value in simple.items():
        if key in _COUNT_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            return value > 0
        if key in _STATUS_KEYS and isinstance(value, str):
            text = value.lower()
            if re.search(r"sold|unavail|out\W*of\W*stock|outofstock|disabled|inactive", text):
                return False
            if re.search(r"in\W*stock|instock|available|active", text):
                return True
    if depth < 2:
        for key in ("offers", "availability", "stock", "inventory", "status"):
            nested = d.get(key)
            for candidate in (nested if isinstance(nested, list) else [nested]):
                if isinstance(candidate, dict):
                    answer = _availability(candidate, depth + 1)
                    if answer is not None:
                        return answer
    return None


def _merge(a, b):
    """Combine two stock answers; any 'in stock' wins so a bag is never missed."""
    if a is True or b is True:
        return True
    if a is False or b is False:
        return False
    return None


def _object_around(text, pos, window=6000):
    """The innermost JSON object in `text` that contains position `pos`."""
    end, floor = pos, max(0, pos - window)
    for _ in range(300):
        start = text.rfind("{", floor, end)
        if start < 0:
            return None
        end = start
        try:
            obj, stop = _DECODER.raw_decode(text, start)
        except ValueError:
            continue
        if stop > pos and isinstance(obj, dict):
            return obj
    return None


def _streamed_page_data(html):
    """Newer Next.js pages stream their data as self.__next_f.push([1, "..."]) strings."""
    parts = []
    for match in NEXT_FLIGHT.finditer(html):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except ValueError:
            pass
    return "".join(parts)


def _visible_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(text))


def find_bags(html):
    """Return (bags, menu_size). Each bag is {name, price, available, source}."""
    bags = {}
    data_products = 0

    def add(d):
        name = _name_of(d)
        if not name or not BAG_NAME.search(name) or not _looks_like_product(d):
            return
        bag = {"name": name, "price": _price_of(d), "available": _availability(d), "source": "page data"}
        key = name.lower()
        if key in bags:
            bags[key]["available"] = _merge(bags[key]["available"], bag["available"])
            bags[key]["price"] = bags[key]["price"] or bag["price"]
        else:
            bags[key] = bag

    # 1. JSON blocks in the page (Next.js __NEXT_DATA__, structured data)
    for match in SCRIPT_JSON.finditer(html):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        for d in _walk(data):
            if _looks_like_product(d):
                data_products += 1
                add(d)

    # 2. Data streamed in script strings or kept in script variables: find the
    #    bag's name and read the JSON object wrapped around it.
    streamed = _streamed_page_data(html)
    if streamed:
        data_products = max(data_products, len(re.findall(r'"price', streamed, re.I)))
    for text in (streamed, html):
        for match in JSON_NAME.finditer(text or ""):
            obj = _object_around(text, match.start())
            if obj:
                for d in _walk(obj):
                    add(d)

    text = _visible_text(html)
    menu_size = max(data_products, len(AED_PRICE.findall(text)))

    # 3. If the page data didn't say whether a bag is in stock, look for a
    #    "Sold out" label just after its name on the page.
    for bag in bags.values():
        if bag["available"] is None:
            at = text.lower().find(bag["name"].lower())
            if at >= 0 and SOLD_OUT_TEXT.search(text[at: at + len(bag["name"]) + 80]):
                bag["available"] = False

    # 4. No readable page data at all: fall back to the words on the page,
    #    counting only mentions with a price next to them (not headings or promos).
    if not bags and data_products == 0:
        for match in BAG_NAME.finditer(text):
            snippet = text[match.start(): match.start() + 110]
            name, price, after = _split_name_and_price(snippet)
            if not price:
                continue
            key = name.lower()
            if key not in bags:
                bags[key] = {
                    "name": name,
                    "price": price,
                    "available": False if SOLD_OUT_TEXT.search(after[:40]) else None,
                    "source": "page text",
                }

    return list(bags.values()), menu_size


def _split_name_and_price(snippet):
    """'Zero Waste Bag (Minimum value AED 100) 1 Each AED 50.00 Sold out'
    -> ('Zero Waste Bag (Minimum value AED 100) 1 Each', 'AED 50.00', ' Sold out')"""
    for match in AED_PRICE.finditer(snippet):
        before = snippet[max(0, match.start() - 14): match.start()].lower()
        if re.search(r"(value|worth|min\w*|up to)\W*$", before):
            continue  # "Minimum value AED 100" is part of the name, not the price
        return snippet[:match.start()].strip(" ·-|,"), match.group(0), snippet[match.end():]
    return snippet[:60].strip(), None, ""


def store_name(html, url):
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if match:
        name = " ".join(unescape(match.group(1)).split())
        name = re.split(r"\s+(?:delivery from|-\s*Order with|\|)\s+", name, maxsplit=1)[0].strip(" -|")
        if name:
            return name
    path = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    return path[-1].replace("-", " ").title() if path else "Deliveroo HOP"


BLOCK_SIGNS = ("just a moment", "cf-chl", "challenge-platform", "attention required",
               "access denied", "are you a robot", "px-captcha", "request unsuccessful",
               "verify you are human", "enable javascript and cookies")


def check_store(url):
    status, html, error = fetch(url)
    result = {"url": url, "status": status, "error": error, "html": html,
              "store": None, "bags": [], "menu_size": 0}
    if html:
        result["store"] = store_name(html, url)
        result["bags"], result["menu_size"] = find_bags(html)
    if result["bags"] or (status == 200 and result["menu_size"] >= 5):
        result["outcome"] = "ok"
    elif status == 0:
        result["outcome"] = "offline"
    elif status in (401, 403, 429, 503) or any(s in html[:60000].lower() for s in BLOCK_SIGNS):
        result["outcome"] = "blocked"
    elif status in (404, 410):
        result["outcome"] = "not_found"
    else:
        result["outcome"] = "unreadable"
    return result


def problem_text(result):
    outcome = result["outcome"]
    if outcome == "blocked":
        return "Deliveroo refused the request (HTTP %s); it is treating the check as a bot." % result["status"]
    if outcome == "offline":
        return "Couldn't reach Deliveroo (%s)." % result["error"]
    if outcome == "not_found":
        return "That HOP link gives 'page not found'."
    return "Got a page (HTTP %s) but couldn't find a menu in it." % result["status"]


def fix_hint(result):
    outcome = result["outcome"]
    if outcome == "blocked":
        return "If this runs on GitHub, run it on your own computer instead (see the README)."
    if outcome == "offline":
        return "Check the internet connection; it will keep trying."
    if outcome == "not_found":
        return "Copy your HOP link again from deliveroo.ae and update HOP_URLS."
    return "Deliveroo may have changed its page. Run --check and keep last_page.html for debugging."


# ---------------------------------------------------------------------------
# Notifications, memory and the sightings log
# ---------------------------------------------------------------------------

def push(title, message, click=None, priority=4, tags=None):
    """Send a notification through ntfy. Returns True if it was accepted."""
    topic = (NTFY_TOPIC or "").strip()
    if not topic:
        print("  No notification sent: NTFY_TOPIC isn't set. On GitHub, add it as a repository "
              "secret; on your own computer, paste it near the top of this script.")
        return False
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    request = urllib.request.Request(
        NTFY_SERVER.rstrip("/") + "/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            accepted = 200 <= response.status < 300
    except Exception as error:
        print("  Couldn't send the notification: %s" % error)
        return False
    print("  Notification sent." if accepted else "  ntfy didn't accept the notification.")
    return accepted


def now_dubai():
    return datetime.now(DUBAI)


def in_quiet_hours(moment):
    match = re.fullmatch(r"\s*(\d{1,2})(?::\d{2})?\s*-\s*(\d{1,2})(?::\d{2})?\s*", QUIET_HOURS or "")
    if not match:
        return False
    start, end, hour = int(match.group(1)) % 24, int(match.group(2)) % 24, moment.hour
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("stores", {})
    return state


def save_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(temporary, STATE_FILE)


LOG_FIELDS = ["time_dubai", "store", "event", "bag", "price", "minutes_listed", "checks_seen"]


def log_event(moment, store, event, bag, minutes="", checks=""):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LOG_FIELDS)
        writer.writerow([moment.strftime("%Y-%m-%d %H:%M"), store, event,
                         bag.get("name", ""), bag.get("price") or "", minutes, checks])


def _minutes_since(iso_time, moment):
    try:
        return int((moment - datetime.fromisoformat(iso_time)).total_seconds() // 60)
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def hop_urls():
    return [u for u in re.split(r"[,\s]+", HOP_URLS or "") if u.startswith("http")]


def describe(result, moment):
    print("\n%s  %s" % (moment.strftime("%a %d %b %H:%M"), result["store"] or result["url"]))
    if result["outcome"] != "ok":
        print("  Problem: " + problem_text(result))
        print("  " + fix_hint(result))
        return
    print("  Menu read OK (%d products seen)." % result["menu_size"])
    if not result["bags"]:
        print("  No Zero Waste Bag listed right now.")
        return
    labels = {True: "IN STOCK", False: "sold out",
              None: "listed (the page doesn't say if it's in stock, so it counts as available)"}
    for bag in result["bags"]:
        price = " · %s" % bag["price"] if bag["price"] else ""
        print("  * %s%s · %s" % (bag["name"], price, labels[bag["available"]]))


def _store_key(url):
    """Saved state is filed under a short fingerprint of the link, not the link
    itself, so a public repo doesn't reveal which HOP store is being watched."""
    return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:12]


def run_once(state, notify=True):
    moment = now_dubai()
    quiet = in_quiet_hours(moment)
    for url in hop_urls():
        result = check_store(url)
        record = state["stores"].setdefault(_store_key(url), {})
        record.setdefault("listed", {})
        record.setdefault("failures", 0)
        record.setdefault("problem_alert_sent", False)
        store = result["store"] or record.get("store") or "Deliveroo HOP"
        record["store"] = store
        record.setdefault("watching_since", moment.isoformat(timespec="minutes"))
        describe(result, moment)

        if result["outcome"] != "ok":
            # Capped so the saved state stops changing (and being re-saved) during a long outage.
            record["failures"] = min(record["failures"] + 1, 3)
            record["last_problem"] = problem_text(result)
            if notify and record["failures"] >= 3 and not record["problem_alert_sent"]:
                if push("Zero Waste watcher can't see Deliveroo",
                        "%s That's %d checks in a row. %s" % (
                            problem_text(result), record["failures"], fix_hint(result)),
                        priority=3, tags=["warning"]):
                    record["problem_alert_sent"] = True
            continue

        record["failures"] = 0
        record["problem_alert_sent"] = False
        record.pop("last_problem", None)

        on_sale = {b["name"].lower(): b for b in result["bags"] if b["available"] is not False}
        listed = record["listed"]
        for key in list(listed):
            if key not in on_sale:
                entry = listed.pop(key)
                log_event(moment, store, "gone", entry,
                          _minutes_since(entry["since"], moment), entry.get("checks", 1))
        for key, bag in on_sale.items():
            if key in listed:
                listed[key]["price"] = bag["price"] or listed[key].get("price")
                listed[key]["checks"] = listed[key].get("checks", 1) + 1
            else:
                listed[key] = {"name": bag["name"], "price": bag["price"], "checks": 1,
                               "since": moment.isoformat(timespec="minutes"), "notified": False}
                log_event(moment, store, "appeared", bag)

        waiting = [entry for entry in listed.values() if not entry["notified"]]
        if waiting and notify:
            if quiet:
                print("  Quiet hours: you'll be told when they end if it's still there.")
            else:
                lines = [e["name"] + (" · %s" % e["price"] if e.get("price") else "") for e in waiting]
                message = "%s\n%s\nTap to open Deliveroo." % (store, "\n".join(lines))
                if push("Zero Waste Bag is live", message, click=url, priority=4, tags=["shopping_cart"]):
                    for entry in waiting:
                        entry["notified"] = True


def cmd_check():
    for url in hop_urls():
        result = check_store(url)
        describe(result, now_dubai())
        if result["outcome"] == "ok":
            on_sale = [b for b in result["bags"] if b["available"] is not False]
            print("  => " + ("You'd get a notification now." if on_sale else "Nothing to notify."))
        if result["html"]:
            with open(PAGE_COPY, "w", encoding="utf-8") as f:
                f.write(result["html"])
    print("\nA copy of the page was saved as %s (handy if the result looks wrong)." % PAGE_COPY)


def cmd_test_push():
    ok = push("Test from your Zero Waste watcher",
              "Notifications work. You'll get one like this when a bag is listed.",
              tags=["white_check_mark"])
    return 0 if ok else 1


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cmd_report():
    """Which hours bags have dropped, on how many days, how long they lasted,
    and whether the watcher is checking often enough to catch them."""
    rows = []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        pass

    starts = []
    for record in load_state()["stores"].values():
        try:
            starts.append(datetime.fromisoformat(record["watching_since"]))
        except (KeyError, TypeError, ValueError):
            pass
    days_watched = (now_dubai().date() - min(starts).date()).days + 1 if starts else None
    watched = " in %d day(s) of watching" % days_watched if days_watched else ""

    drops, still_open = [], {}
    for row in rows:
        try:
            moment = datetime.strptime(row["time_dubai"], "%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            continue
        key = (row.get("store"), (row.get("bag") or "").lower())
        if row.get("event") == "appeared":
            still_open[key] = {"start": moment, "end": None, "checks": None,
                               "bag": row.get("bag") or "", "price": row.get("price") or ""}
        elif row.get("event") == "gone" and key in still_open:
            drop = still_open.pop(key)
            drop["end"] = moment
            drop["checks"] = _as_int(row.get("checks_seen"))
            drops.append(drop)
    drops.extend(still_open.values())
    drops.sort(key=lambda d: d["start"])

    if not drops:
        print("No bags spotted yet%s." % watched)
        return

    drop_days = {d["start"].date() for d in drops}
    print("Bags spotted %d time(s), on %d different day(s)%s." % (len(drops), len(drop_days), watched))

    print("\nWhat time they appeared (Dubai):")
    blocks = {}
    for d in drops:
        block = d["start"].hour // 2 * 2
        blocks[block] = blocks.get(block, 0) + 1
    for block in range(min(blocks), max(blocks) + 1, 2):
        count = blocks.get(block, 0)
        print("  %02d:00 to %02d:00  %s" % (block, block + 2, ("#" * count + " %d" % count) if count else "0"))

    lasted = [(d["end"] - d["start"]).total_seconds() / 60 for d in drops if d["end"]]
    if lasted:
        print("\nGone within %d to %d minutes of being spotted (typically %d)." % (
            min(lasted), max(lasted), round(statistics.median(lasted))))

    timed = [d for d in drops if d["end"] and d["checks"] is not None]
    gone_fast = [d for d in timed if d["checks"] <= 1]
    if gone_fast:
        print("\n%d of %d were gone by the very next check. Bags are selling out faster than the "
              "watcher looks, so some have probably come and gone unseen." % (len(gone_fast), len(timed)))
        if len(gone_fast) * 3 >= len(timed):
            print("Worth checking more often: see 'Checking faster' in the README.")
    elif timed:
        print("\nEvery bag was still there at the next check, so the checking rate is keeping up.")

    print("\nMost recent:")
    for d in drops[-15:]:
        ending = "gone by %s" % d["end"].strftime("%H:%M") if d["end"] else "still listed"
        print("  %s  %s  %s %s" % (d["start"].strftime("%a %d %b %H:%M"), ending, d["bag"], d["price"]))
    print("\nTimes are only as precise as how often the watcher checks.")


def run_forever():
    urls = hop_urls()
    print("Watching %d HOP store(s), checking about every %g minutes. Press Ctrl+C to stop."
          % (len(urls), CHECK_EVERY_MINUTES))
    while True:
        state = load_state()
        try:
            run_once(state)
        except Exception as error:  # keep going whatever happens
            print("  Unexpected problem: %s" % error)
        save_state(state)
        time.sleep(max(60.0, CHECK_EVERY_MINUTES * 60 * random.uniform(0.85, 1.15)))


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="Tells you when a Deliveroo HOP Zero Waste Bag is listed.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="look once and print what's there; sends nothing")
    mode.add_argument("--once", action="store_true", help="look once and notify if a bag has appeared")
    mode.add_argument("--test-push", action="store_true", help="send a test notification")
    mode.add_argument("--report", action="store_true", help="summarise when bags have appeared")
    args = parser.parse_args()

    if args.test_push:
        return cmd_test_push()
    if args.report:
        return cmd_report()
    if not hop_urls():
        print("Set HOP_URLS first: on deliveroo.ae (in a browser) set your address, open "
              "'24/7 Deliveroo HOP with Choithrams' and copy the link. On GitHub, add it as the "
              "HOP_URLS repository secret; on your own computer, paste it into HOP_URLS at the "
              "top of this file.")
        # Scheduled runs exit cleanly so GitHub doesn't email a failure every few minutes.
        return 0 if args.once else 2
    if args.check:
        return cmd_check()
    if args.once:
        state = load_state()
        run_once(state)
        save_state(state)
        return 0
    try:
        run_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
