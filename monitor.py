"""JB Hi-Fi / Target AU Pokemon TCG monitor.

JB Hi-Fi: polls JB's search API for Pokemon TCG products sold by JB Hi-Fi itself
(no marketplace sellers).
Target AU: reads the product list embedded in Target's Pokemon Cards category page.

For either retailer it posts Discord alerts for new listings and restocks, and keeps
a "currently available" board message up to date.

Standard library only, so it runs on a bare GitHub Actions runner.

Usage:
    python monitor.py                    # JB Hi-Fi normal run (used by the workflow)
    python monitor.py --retailer target  # Target AU normal run
    python monitor.py --dry-run          # fetch + diff, print payloads, send nothing, save nothing
    python monitor.py --test-alert       # send one sample alert so you can see the format

Environment (for Target, a TARGET_-prefixed variable wins over the plain one,
e.g. TARGET_DISCORD_WEBHOOK_URL, so both monitors can share one .env):
    DISCORD_WEBHOOK_URL        required  channel that receives alerts
    DISCORD_BOARD_WEBHOOK_URL  optional  channel for the live board (defaults to the alert webhook)
    DISCORD_PING               optional  mention added to alerts, e.g. <@123> or <@&456>
    INCLUDE_ACCESSORIES        optional  "true" to also track sleeves/binders/deck boxes
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- config

API_URL = "https://api-prod.jbhifi.com.au/OnlineSearchApi/search"
# Public key the jbhifi.com.au frontend sends with every search request.
API_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json",
    "origin": "https://www.jbhifi.com.au",
    "referer": "https://www.jbhifi.com.au/",
    "x-jbg-api-key": "980e245d7d344759bfcb5f02ba91fae4",
    "x-jbg-company": "JBH",
    "x-jbg-country": "AU",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
    ),
}
# Server-side filter: trading cards, Pokemon franchise, sold by JB (not marketplace).
SEARCH_FILTER = (
    'attributes.categoryHierarchy: ANY("Trading card games") '
    'AND attributes.Primary_20franchise: ANY("Pokemon") '
    'AND attributes.isMarketplace: ANY("false")'
)
PRODUCT_URL = "https://www.jbhifi.com.au/products/{handle}"

GROUP_CARDS = 1186        # MERCH TRADING CARDS (boosters, ETBs, decks, boxes)
GROUP_ACCESSORIES = 1106  # MERCH TRADING CARD ACCESSORIES (sleeves, binders...)


STATUS_STYLE = {
    # status:      (label,          colour,   emoji)
    "preorder":    ("Pre-order",     0x8B5CF6, "🟣"),
    "in_stock":    ("In stock",      0x22C55E, "🟢"),
    "limited":     ("Limited stock", 0xF59E0B, "🟠"),
    "coming_soon": ("Coming soon",   0x3B82F6, "🔵"),
    "unavailable": ("Sold out",      0x6B7280, "⚫"),
}
AVAILABLE = {"preorder", "in_stock", "limited"}

# --------------------------------------------------------------------------- JB API


def http_json(url: str, body: dict | None, headers: dict, method: str = "POST", retries: int = 3):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt < retries:
                    wait = float(e.headers.get("Retry-After") or 2 * attempt)
                    time.sleep(wait)
                    continue
            return e.code, _safe_json(e.read())
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def _safe_json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:500]


def fetch_products(include_accessories: bool) -> dict[str, dict]:
    groups = {GROUP_CARDS} | ({GROUP_ACCESSORIES} if include_accessories else set())
    products: dict[str, dict] = {}
    token = None
    for _ in range(20):  # page guard
        body = {"query": "pokemon", "filter": SEARCH_FILTER, "pageSize": 100}
        if token:
            body["pageToken"] = token
        status, data = http_json(API_URL, body, API_HEADERS)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"JB search failed: HTTP {status}: {data}")
        for hit in data.get("hits", []):
            if hit.get("isMarketplace"):
                continue
            if (hit.get("productGroup") or {}).get("id") not in groups:
                continue
            p = normalise(hit)
            products[p["sku"]] = p
        token = data.get("nextPageToken")
        if not token:
            break
    return products


def normalise(hit: dict) -> dict:
    avail = hit.get("availability") or {}
    product = hit.get("product") or {}
    pricing = hit.get("pricing") or {}

    if avail.get("canPreOrder") or avail.get("productLifecycle") == "PreOrder":
        status = "preorder"
    elif not avail.get("canBuyOnline") and avail.get("cashNCarryStatus") in (None, "NotAvailable"):
        status = "unavailable"
    elif avail.get("overallStatus") == "InStock":
        status = "in_stock"
    elif avail.get("overallStatus") == "LimitedStock":
        status = "limited"
    else:
        status = "unavailable"

    where = []
    if avail.get("deliveryStatus") not in (None, "NotAvailable"):
        where.append("Online")
    if avail.get("cashNCarryStatus") not in (None, "NotAvailable"):
        where.append("In-store")

    was = pricing.get("wasPrice") if pricing.get("saveAmount") else None
    return {
        "sku": str(hit["sku"]),
        "title": hit.get("title", "").strip(),
        "url": PRODUCT_URL.format(handle=hit.get("handle", "")),
        "image": hit.get("product_image") or "",
        "price": float(hit.get("price") or 0),
        "was_price": float(was) if was else None,
        "status": status,
        "where": where,
        "limit": product.get("limitPerOrder"),
        "release": product.get("releaseDate"),
        "expansion": ((hit.get("facets") or {}).get("Expansion") or [None])[0],
        "group": (hit.get("productGroup") or {}).get("id"),
        # When JB published / last touched the listing; used only for latency logging.
        "_published": hit.get("published_at"),
        "_updated": hit.get("updated_at"),
    }


# --------------------------------------------------------------------------- Target AU

TARGET_CATEGORY_URL = "https://www.target.com.au/c/toys/trading-card-games/pokemon-cards/W1852642"
TARGET_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-AU,en;q=0.9",
    "user-agent": API_HEADERS["user-agent"],
}
TARGET_PAGE_SIZE = 48
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# The category also holds plush and sleeves. Cards = "Pokemon" + "TCG"/"Trading Card Game";
# accessories are Pokemon items matching these words ("Sleeved Booster" is still a card product).
TARGET_TCG_RE = re.compile(r"\b(tcg|trading card game)\b", re.I)
TARGET_ACCESSORY_RE = re.compile(
    r"\b(sleeves|deck protector|portfolio|binder|toploader|pages|playmat|deck box|album|"
    r"storage|protector)\b", re.I)


def http_text(url: str, headers: dict, retries: int = 3) -> str:
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                time.sleep(float(e.headers.get("Retry-After") or 2 * attempt))
                continue
            raise RuntimeError(f"GET {url} failed: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def target_kind(title: str) -> str | None:
    """'cards', 'accessory', or None for anything that isn't Pokemon TCG."""
    if not re.search(r"pok[eé]mon", title, re.I):
        return None
    if TARGET_ACCESSORY_RE.search(title):
        return "accessory"
    if TARGET_TCG_RE.search(title):
        return "cards"
    return None  # plush, figures, etc.


def fetch_target(include_accessories: bool) -> dict[str, dict]:
    wanted = {"cards"} | ({"accessory"} if include_accessories else set())
    products: dict[str, dict] = {}
    for page in range(1, 11):  # page guard
        url = TARGET_CATEGORY_URL + (f"?page={page}" if page > 1 else "")
        m = NEXT_DATA_RE.search(http_text(url, TARGET_HEADERS))
        if not m:
            raise RuntimeError(f"Target page has no __NEXT_DATA__ (blocked or redesigned?): {url}")
        plist = json.loads(m.group(1))["props"]["pageProps"]["metadata"]["productList"]
        hits = plist.get("products") or []
        for hit in hits:
            if target_kind(hit.get("title", "")) in wanted:
                p = normalise_target(hit)
                products[p["sku"]] = p
        if not hits or page * TARGET_PAGE_SIZE >= (plist.get("totalNumProducts") or 0):
            break
    return products


def normalise_target(hit: dict) -> dict:
    variations = hit.get("variations") or []
    price = hit.get("price") or {}
    if hit.get("displayOnly"):
        status = "unavailable"
    elif any(v.get("inStock") for v in variations):
        status = "in_stock"
    elif any(v.get("comingSoon") for v in variations):
        status = "coming_soon"
    else:
        status = "unavailable"
    image = next((v.get("imageUrl") for v in variations if v.get("imageUrl")), None) or hit.get("imageUrl") or ""
    was = price.get("wasPrice") or 0
    return {
        "sku": str(hit["id"]),
        "title": " ".join(hit.get("title", "").split()),
        "url": hit.get("baseProductUrl") or "",
        "image": image,
        "price": float(price.get("offerPrice") or 0),
        "was_price": float(was) if was else None,
        "status": status,
        "where": [],
        "limit": None,
        "release": None,
        "expansion": None,
        "group": None,
    }


RETAILERS = {
    "jb": {
        "name": "JB Hi-Fi",
        "fetch": fetch_products,
        "state": "state.json",
        "board_url": "https://www.jbhifi.com.au/collections/pokemon-trading-cards",
        "env_prefix": "",
    },
    "target": {
        "name": "Target",
        "fetch": fetch_target,
        "state": "state_target.json",
        "board_url": TARGET_CATEGORY_URL,
        "env_prefix": "TARGET_",
    },
}
SHOP = RETAILERS["jb"]  # set from --retailer in main()


# --------------------------------------------------------------------------- formatting


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def short_title(title: str) -> str:
    for prefix in ("Pokemon TCG - ", "Pokémon TCG - ", "Pokemon TCG: ", "Pokémon TCG: ",
                   "Pokemon Trading Card Game - ", "Pokemon Trading Card Game: ", "Pokemon TCG "):
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


def ts(iso: str | None, style: str = "D") -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"<t:{int(dt.timestamp())}:{style}>"


def is_future(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


def alert_embed(p: dict, kind: str) -> dict:
    label, colour, emoji = STATUS_STYLE[p["status"]]
    heading = {"new": "New listing", "restock": "Back in stock", "live": "Now available"}[kind]

    price = money(p["price"])
    if p["was_price"]:
        price += f"  ~~{money(p['was_price'])}~~"

    fields = [
        {"name": "Price", "value": price, "inline": True},
        {"name": "Status", "value": f"{emoji} {label}", "inline": True},
        {"name": "Limit", "value": f"{p['limit']} per customer" if p["limit"] else "—", "inline": True},
    ]
    if p["status"] == "preorder" or is_future(p["release"]):
        rel = ts(p["release"], "D")
        if rel:
            fields.append({"name": "Release", "value": f"{rel} ({ts(p['release'], 'R')})", "inline": True})
    if p["where"]:
        fields.append({"name": "Available", "value": " · ".join(p["where"]), "inline": True})
    if p["expansion"]:
        fields.append({"name": "Set", "value": p["expansion"], "inline": True})

    embed = {
        "author": {"name": f"{SHOP['name']}  •  {heading}"},
        "title": p["title"][:256],
        "url": p["url"],
        "color": colour,
        "fields": fields,
        "footer": {"text": f"SKU {p['sku']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if p["image"]:
        embed["thumbnail"] = {"url": p["image"]}
    return embed


def board_embeds(products: dict[str, dict]) -> list[dict]:
    now = int(time.time())
    sections = [
        ("preorder", "Pre-orders"),
        ("in_stock", "In stock"),
        ("limited", "Limited stock"),
    ]
    live = [p for p in products.values() if p["status"] in AVAILABLE]

    header = {
        "title": f"{SHOP['name']} — Pokémon TCG available now",
        "url": SHOP["board_url"],
        "color": 0xFACC15,
        "description": (
            f"**{len(live)}** products live"
            + "".join(
                f"  ·  {STATUS_STYLE[s][2]} {sum(1 for p in live if p['status'] == s)}"
                for s, _ in sections
            )
            + f"\nLast checked <t:{now}:R>"
        ),
    }
    embeds = [header]
    for status, _ in sections:
        items = sorted(
            (p for p in live if p["status"] == status),
            key=lambda p: (p["release"] or "", p["title"]) if status == "preorder" else (-p["price"], p["title"]),
        )
        embeds.extend(board_card(p) for p in items)

    if len(embeds) == 1:
        header["description"] += "\n\nNothing available right now."
    return embeds


def board_card(p: dict) -> dict:
    """One compact card per product: image on the right, colour = status."""
    label, colour, emoji = STATUS_STYLE[p["status"]]
    line1 = [f"**{money(p['price'])}**", f"{emoji} {label}"]
    if p["status"] == "preorder" or is_future(p["release"]):
        rel = ts(p["release"], "D")
        if rel:
            line1.append(f"out {rel}")
    line2 = []
    if p["limit"]:
        line2.append(f"Limit {p['limit']}")
    if p["where"]:
        line2.append(" + ".join(p["where"]))
    card = {
        "title": short_title(p["title"])[:256],
        "url": p["url"],
        "color": colour,
        "description": " · ".join(line1) + ("\n" + " · ".join(line2) if line2 else ""),
    }
    if p["image"]:
        card["thumbnail"] = {"url": p["image"]}
    return card


# --------------------------------------------------------------------------- Discord


def webhook_parts(url: str) -> str:
    return url.split("?")[0].rstrip("/")


def discord_send(webhook: str, payload: dict) -> str | None:
    url = webhook_parts(webhook) + "?wait=true"
    status, data = http_json(url, payload, {"content-type": "application/json", "user-agent": "jb-tcg-monitor"})
    if status not in (200, 204):
        raise RuntimeError(f"Discord send failed: HTTP {status}: {data}")
    return (data or {}).get("id")


def discord_edit(webhook: str, message_id: str, payload: dict) -> bool:
    url = f"{webhook_parts(webhook)}/messages/{message_id}"
    status, data = http_json(url, payload, {"content-type": "application/json", "user-agent": "jb-tcg-monitor"},
                             method="PATCH")
    if status == 404:
        return False
    if status != 200:
        raise RuntimeError(f"Discord edit failed: HTTP {status}: {data}")
    return True


def send_alerts(webhook: str, ping: str, alerts: list[tuple[dict, str]]) -> None:
    # Discord allows 10 embeds per message; batch so a big drop is one or two messages.
    for i in range(0, len(alerts), 10):
        batch = alerts[i:i + 10]
        payload = {
            "username": f"{SHOP['name']} Monitor",
            "embeds": [alert_embed(p, kind) for p, kind in batch],
            "allowed_mentions": {"parse": ["users", "roles"]},
        }
        if ping:
            payload["content"] = ping
        discord_send(webhook, payload)
        time.sleep(1)


def discord_delete(webhook: str, message_id: str) -> None:
    url = f"{webhook_parts(webhook)}/messages/{message_id}"
    status, data = http_json(url, None, {"user-agent": "jb-tcg-monitor"}, method="DELETE")
    if status not in (204, 404):
        raise RuntimeError(f"Discord delete failed: HTTP {status}: {data}")


def update_board(webhook: str, state: dict, products: dict[str, dict]) -> None:
    embeds = board_embeds(products)
    # Discord caps a message at 10 embeds: header + 9 cards in the first, 10 cards in each after.
    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]
    payloads = [{"embeds": c, "allowed_mentions": {"parse": []}} for c in chunks]

    ids = state.get("board_message_ids")
    if ids is None and state.get("board_message_id"):
        ids = [state.pop("board_message_id")]  # migrate from single-message board
    ids = ids or []

    if len(ids) == len(payloads):
        ok = True
        for msg_id, payload in zip(ids, payloads):
            if not discord_edit(webhook, msg_id, payload):
                ok = False  # someone deleted a board message
                break
            time.sleep(0.5)
        if ok:
            state["board_message_ids"] = ids
            return

    # Message count changed (or a message vanished): repost the whole board so it stays contiguous.
    for msg_id in ids:
        discord_delete(webhook, msg_id)
        time.sleep(0.5)
    new_ids = []
    for payload in payloads:
        new_ids.append(discord_send(webhook, {**payload, "username": f"{SHOP['name']} Monitor"}))
        time.sleep(0.5)
    state["board_message_ids"] = new_ids
    print(f"Posted board as {len(new_ids)} message(s) {new_ids} — pin the first one in Discord.")


# --------------------------------------------------------------------------- state + diff


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def diff(prev: dict[str, dict], current: dict[str, dict]) -> list[tuple[dict, str]]:
    alerts = []
    for sku, p in current.items():
        old = prev.get(sku)
        if old is None:
            if p["status"] in AVAILABLE:
                alerts.append((p, "new"))
        elif p["status"] in AVAILABLE and old.get("status") not in AVAILABLE:
            alerts.append((p, "live" if old.get("status") == "coming_soon" else "restock"))
    return alerts


def merge(prev: dict[str, dict], current: dict[str, dict]) -> dict[str, dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = {}
    for sku, p in current.items():
        # No per-run timestamps here, so state.json only changes (and gets committed) on real changes.
        # Keys starting with "_" are per-fetch metadata (JB timestamps) and are not persisted.
        kept = {k: v for k, v in p.items() if not k.startswith("_")}
        merged[sku] = {**kept, "first_seen": prev.get(sku, {}).get("first_seen", now)}
    for sku, old in prev.items():
        if sku not in current:
            # Dropped out of search results: JB hides sold-out/delisted items.
            merged[sku] = {**old, "status": "unavailable"}
    return merged


# --------------------------------------------------------------------------- timing

INTERVAL_MIN = 15      # check on every quarter hour...
OFFSET_SEC = 1         # ...one second past it: 4:00:01, 4:15:01, ...


def slot_for(now: float, cron_minute: int | None) -> float:
    """The check time this run is responsible for.

    With the cron minute that triggered the run (e.g. 53), the slot is the first quarter hour
    after the most recent firing of that minute, so a run GitHub started late still knows which
    slot it was meant for. Without it, fall back to the next quarter hour.
    """
    period = INTERVAL_MIN * 60
    if cron_minute is None:
        return (now - OFFSET_SEC) // period * period + period + OFFSET_SEC
    hour = now // 3600 * 3600
    fired = hour + cron_minute * 60
    if fired > now:
        fired -= 3600
    return (fired // period) * period + period + OFFSET_SEC


def wait_for_slot(cron_minute: int | None) -> None:
    now = time.time()
    slot = slot_for(now, cron_minute)
    wait = slot - now
    when = datetime.fromtimestamp(slot, timezone.utc).strftime("%H:%M:%S UTC")
    if wait <= 0:
        # GitHub started us after the slot; check now rather than skip it.
        print(f"Started {-wait:.0f}s after the {when} slot; checking immediately.")
        return
    print(f"Waiting {wait:.1f}s for {when}")
    while (remaining := slot - time.time()) > 0:
        time.sleep(min(remaining, 30))


# --------------------------------------------------------------------------- main


LOG_DIR = Path(__file__).with_name("logs")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (KEY=VALUE lines). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log_csv(name: str, header: list[str], row: list) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / name
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def log_latency(alerts: list[tuple[dict, str]], detected: datetime) -> None:
    """How long after JB published (new) / last updated (restock) the listing we noticed it."""
    for p, kind in alerts:
        ref = p.get("_published") if kind == "new" else p.get("_updated")
        lag = None
        if ref:
            try:
                lag = round((detected - datetime.fromisoformat(ref.replace("Z", "+00:00"))).total_seconds(), 1)
            except ValueError:
                pass
        log_csv("latency.csv",
                ["detected_utc", "kind", "sku", "title", "status", "jb_timestamp", "lag_seconds"],
                [detected.isoformat(timespec="seconds"), kind, p["sku"], p["title"], p["status"], ref, lag])
        print(f"  latency {kind} {p['sku']}: {lag}s after JB timestamp {ref}")


def process(state: dict, current: dict[str, dict], cfg: dict, dry_run: bool,
            log_lag: bool = False) -> tuple[dict, list[tuple[dict, str]]]:
    """Diff a fetch against state, send alerts, return (merged products, alerts)."""
    prev = state.get("products", {})
    first_run = not prev and not state.get("seeded")
    alerts = [] if first_run else diff(prev, current)
    merged = merge(prev, current)
    if first_run:
        print("First run: seeding state without alerts.")
    for p, kind in alerts:
        print(f"[{kind}] {p['title']} — {money(p['price'])} — {p['status']}")
    if log_lag and alerts:
        log_latency(alerts, datetime.now(timezone.utc))
    if alerts and not dry_run:
        send_alerts(cfg["alert_hook"], cfg["ping"], alerts)
    state["seeded"] = True
    return merged, alerts


POLL_HEADER = ["utc", "ok", "ms", "products", "available", "error"]


def fetch_logged(cfg: dict) -> dict[str, dict]:
    t0 = time.time()
    started = datetime.now(timezone.utc)
    try:
        current = SHOP["fetch"](cfg["include_acc"])
    except Exception as e:
        if cfg.get("log_polls"):
            log_csv("polls.csv", POLL_HEADER,
                    [started.isoformat(timespec="seconds"), 0, int((time.time() - t0) * 1000), "", "", str(e)[:200]])
        raise
    ms = int((time.time() - t0) * 1000)
    avail = sum(p["status"] in AVAILABLE for p in current.values())
    if cfg.get("log_polls"):
        log_csv("polls.csv", POLL_HEADER, [started.isoformat(timespec="seconds"), 1, ms, len(current), avail, ""])
    print(f"[{started.strftime('%H:%M:%S.%f')[:-3]} UTC] Fetched {len(current)} products "
          f"({avail} available) in {ms} ms")
    return current


def run_once(cfg: dict, state_path: Path, dry_run: bool) -> int:
    current = fetch_logged(cfg)
    state = load_state(state_path) or {"products": {}}
    if not current and state.get("products"):
        print("API returned no products; skipping this run to avoid false sold-out states.")
        return 0
    merged, _ = process(state, current, cfg, dry_run)
    if dry_run:
        print(json.dumps({"embeds": board_embeds(merged)}, indent=2, ensure_ascii=False))
        return 0
    state["products"] = merged
    update_board(cfg["board_hook"], state, merged)
    save_state(state, state_path)
    return 0


def run_loop(cfg: dict, state_path: Path, interval: float, dry_run: bool) -> int:
    """Poll forever. Board is refreshed when something changes, or at most once a minute."""
    board_every = 60
    state = load_state(state_path) or {"products": {}}
    last_board = 0.0
    failures = 0
    print(f"Loop mode: polling every ~{interval:g}s (±20%), state in {state_path.name}. Ctrl-C to stop.")
    while True:
        t0 = time.time()
        try:
            current = fetch_logged(cfg)
            failures = 0
            if not current and state.get("products"):
                print("  empty result; ignoring")
            else:
                before = json.dumps(state.get("products", {}), sort_keys=True)
                ids_before = state.get("board_message_ids")
                merged, _ = process(state, current, cfg, dry_run, log_lag=True)
                state["products"] = merged
                changed = json.dumps(merged, sort_keys=True) != before
                if not dry_run and (changed or time.time() - last_board >= board_every):
                    update_board(cfg["board_hook"], state, merged)
                    last_board = time.time()
                if not dry_run and (changed or state.get("board_message_ids") != ids_before):
                    save_state(state, state_path)
            delay = interval * random.uniform(0.8, 1.2) - (time.time() - t0)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            failures += 1
            delay = min(300, interval * 2 ** failures)  # back off hard on errors / 429s
            print(f"  error #{failures}: {e} — backing off {delay:.0f}s", file=sys.stderr)
        time.sleep(max(1.0, delay))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # UTF-8 for emoji on Windows consoles; line buffering so loop output shows up live in logs.
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't send to Discord or write state")
    ap.add_argument("--test-alert", action="store_true", help="send one sample alert and exit")
    ap.add_argument("--at-quarter", action="store_true",
                    help="sleep until the next :00:01/:15:01/:30:01/:45:01 before checking")
    ap.add_argument("--schedule", default="",
                    help='cron expression that triggered the run (GitHub passes e.g. "53 * * * *")')
    ap.add_argument("--loop", type=float, metavar="SECONDS",
                    help="poll continuously every SECONDS (±20%%), logging to logs/")
    ap.add_argument("--retailer", choices=sorted(RETAILERS), default="jb", help="which store to monitor")
    ap.add_argument("--state", help="state file (default state.json for JB, state_target.json for Target; use a separate one for local trials)")
    ap.add_argument("--env-file", default=".env", help="local file with DISCORD_WEBHOOK_URL=... etc.")
    args = ap.parse_args()

    global SHOP
    SHOP = RETAILERS[args.retailer]
    here = Path(__file__).parent
    load_dotenv(here / args.env_file)

    def env(name: str) -> str:
        # Retailer-specific variable (e.g. TARGET_DISCORD_WEBHOOK_URL) wins over the shared one.
        prefixed = os.environ.get(SHOP["env_prefix"] + name, "").strip() if SHOP["env_prefix"] else ""
        return prefixed or os.environ.get(name, "").strip()

    alert_hook = env("DISCORD_WEBHOOK_URL")
    cfg = {
        "alert_hook": alert_hook,
        "board_hook": env("DISCORD_BOARD_WEBHOOK_URL") or alert_hook,
        "ping": env("DISCORD_PING"),
        "include_acc": env("INCLUDE_ACCESSORIES").lower() in ("1", "true", "yes"),
        "log_polls": bool(args.loop),
    }
    state_file = args.state or SHOP["state"]
    state_path = Path(state_file) if Path(state_file).is_absolute() else here / state_file

    if not alert_hook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL is not set (env var or .env file)", file=sys.stderr)
        return 2

    if args.test_alert:
        current = fetch_logged(cfg)
        sample = next((p for p in current.values() if p["status"] in AVAILABLE), None)
        if not sample:
            print("Nothing available to use as a sample.")
            return 1
        send_alerts(alert_hook, cfg["ping"], [(sample, "new")])
        print(f"Sent test alert for {sample['title']}")
        return 0

    if args.loop:
        if args.loop < 5:
            print("--loop below 5 seconds is asking to get rate-limited; use 5 or more.", file=sys.stderr)
            return 2
        try:
            return run_loop(cfg, state_path, args.loop, args.dry_run)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

    if args.at_quarter:
        first = args.schedule.split()[0] if args.schedule.strip() else ""
        wait_for_slot(int(first) if first.isdigit() else None)
    return run_once(cfg, state_path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
