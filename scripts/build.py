"""Daily price build for the shopping-list app.

Israeli supermarket chains must publish their full price lists per store every day.
This script downloads them (through the open-source il-supermarket-scraper package),
keeps only the stores in the cities the family chose in the app, and writes small
JSON files the app reads:

  data/meta.json        when it ran, the chosen cities, the stores that have prices
  data/cities.json      every city where some chain has a store (for the region picker)
  data/catalog.json     [barcode, name, manufacturer, size] for every product found
  data/prices/NN.json   {barcode: {storeIndex: price}}, NN = last two digits of the barcode

Shelf prices only: promotions and club discounts are not applied.
"""
import datetime
import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from il_supermarket_scarper import ScarpingTask, ScraperFactory

HERE = Path(__file__).resolve().parent
OUT = Path(os.getenv("OUT_DIR", "data"))
DUMPS = Path(os.getenv("DUMPS_DIR", "dumps"))
DEFAULT_CITIES = ["פתח תקווה", "אלעד", "שוהם", "ראש העין"]
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_KEY = os.getenv("FIREBASE_API_KEY", "")
ONLY_SCRAPERS = [s for s in os.getenv("ONLY_SCRAPERS", "").split(",") if s]  # for test runs
PROCESSES = int(os.getenv("NUMBER_OF_PROCESSES", "3"))
CBS = {int(k): v for k, v in json.loads((HERE / "cities.json").read_text(encoding="utf-8")).items()}


def log(*a):
    print(*a, flush=True)


# ---------- city names ----------
def city_key(name):
    """Comparison key: chains spell the same city in different ways (תקוה / תקווה, hyphens, quotes)."""
    s = re.sub(r"[\"'׳״`.\-־_/()]", " ", str(name or ""))
    s = re.sub(r"\s+", "", s)
    return s.replace("וו", "ו").replace("יי", "י")


CANON = {city_key(v): v for v in CBS.values()}


def city_name(raw):
    """Canonical city name from whatever the chain wrote: an official locality code or free text."""
    raw = str(raw or "").strip()
    if not raw or raw in ("0", "00", "000", "0000"):
        return ""
    if raw.isdigit():
        return CBS.get(int(raw), "")
    return CANON.get(city_key(raw), re.sub(r"\s+", " ", raw))


# ---------- reading the chains' files ----------
def read_text(path):
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    head = data[:200].decode("ascii", errors="ignore").lower()
    for enc in (["windows-1255"] if "1255" in head or "8859-8" in head else []) + ["utf-8-sig", "windows-1255"]:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


LEAF = re.compile(r"<([\w:.\-]+)(?:\s[^>]*)?>([^<]*)</\1\s*>")


def fields(block):
    return {m.group(1).split(":")[-1].lower(): m.group(2).strip() for m in LEAF.finditer(block)}


def blocks(text, tag):
    return re.findall(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}\s*>", text, flags=re.S | re.I)


def first(text, *tags):
    for tag in tags:
        m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>\s*([^<]*?)\s*</(?:\w+:)?{tag}\s*>", text, flags=re.I)
        if m and m.group(1):
            return m.group(1)
    return ""


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def files_in(folder, prefix):
    """Downloaded files whose name starts with prefix (the package's folder layout is not assumed)."""
    if not folder.exists():
        return []
    return [p for p in folder.rglob("*") if p.is_file() and p.name.lower().startswith(prefix)]


def stamp(name):
    """Sortable timestamp from a file name like PriceFull7290027600007-001-202611120300.gz."""
    nums = re.findall(r"\d{8,14}", name.split("-", 1)[-1])
    return max(nums, key=len) if nums else ""


def scrape(scrapers, file_types, regex=None, limit=None):
    """Run the scraper package for the given chains. A failing chain never stops the others."""
    task = ScarpingTask(
        enabled_scrapers=scrapers,
        files_types=file_types,
        file_name_regex=regex,
        multiprocessing=PROCESSES,
        output_configuration={"output_mode": "disk", "base_storage_path": str(DUMPS)},
        status_configuration={"database_type": "json", "base_path": str(DUMPS / "status")},
        timeout_in_seconds=60 * 20,
    )
    try:
        task.start(limit=limit, single_pass=True)
        task.join()
    except Exception as exc:  # pylint: disable=broad-except
        log("   scrape error:", repr(exc)[:300])


def chosen_cities():
    """The cities chosen in the app (Firestore doc config/regions), or the defaults."""
    if FIREBASE_PROJECT:
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents/config/regions?key={FIREBASE_KEY}"
        try:
            with urllib.request.urlopen(url, timeout=30) as res:
                doc = json.load(res)
            values = doc.get("fields", {}).get("cities", {}).get("arrayValue", {}).get("values", [])
            names = [v.get("stringValue", "").strip() for v in values]
            names = [n for n in names if n][:15]
            if names:
                return names
        except Exception as exc:  # pylint: disable=broad-except
            log("could not read the app's region choice, using defaults:", repr(exc)[:200])
    return DEFAULT_CITIES


# Display names for chains whose files carry only a number or nothing as the chain name.
CHAIN_NAMES = {
    "RAMI_LEVY": "רמי לוי", "OSHER_AD": "אושר עד", "YOHANANOF": "יוחננוף", "VICTORY": "ויקטורי",
    "VICTORY_NEW_SOURCE": "ויקטורי", "YAYNO_BITAN_AND_CARREFOUR": "קרפור / יינות ביתן", "HAZI_HINAM": "חצי חינם",
    "TIV_TAAM": "טיב טעם", "MAHSANI_ASHUK": "מחסני השוק", "MAHSANI_ASHUK_NEW_SOURCE": "מחסני השוק",
    "BAREKET": "ברקת", "NETIV_HASED": "נתיב החסד", "ZOL_VEBEGADOL": "זול ובגדול", "KESHET": "קשת טעמים",
    "STOP_MARKET": "סטופ מרקט", "SALACH_DABACH": "סאלח דבאח", "FRESH_MARKET_AND_SUPER_DOSH": "פרש מרקט",
    "SUPER_YUDA": "סופר יודה", "SUPER_PHARM": "סופר-פארם", "GOOD_PHARM": "גוד פארם", "DOR_ALON": "דור אלון",
    "KING_STORE": "קינג סטור", "MAAYAN_2000": "מעיין 2000", "POLIZER": "פוליצר", "SHUK_AHIR": "שוק העיר",
    "SHEFA_BARCART_ASHEM": "שפע ברכת השם", "HET_COHEN": "ח. כהן", "SUPER_SAPIR": "סופר ספיר", "QUIK": "קוויק",
    "MESHMAT_YOSEF_1": "משנת יוסף", "MESHMAT_YOSEF_2": "משנת יוסף", "SHUFERSAL": "שופרסל",
}


def chain_label(scraper, chain_name, sub_name):
    """Prefer the sub-chain (שופרסל דיל), then the chain's own name, as long as it is a real name."""
    for candidate in (sub_name, chain_name):
        candidate = (candidate or "").strip()
        if len(candidate) >= 2 and re.search(r"[A-Za-zא-ת]", candidate):
            return candidate
    return CHAIN_NAMES.get(scraper, scraper)


# ---------- step 1: every chain's store list ----------
def load_stores(scrapers):
    stores = {}  # "chainid-storeid" -> info
    for name in scrapers:
        before = set(files_in(DUMPS, "store"))
        scrape([name], ["STORE_FILE"])
        new = sorted(set(files_in(DUMPS, "store")) - before, key=lambda p: stamp(p.name))
        if not new:
            log(f"   {name}: no store file")
            continue
        text = read_text(new[-1])  # the latest one
        chain_id = digits(first(text, "ChainId", "ChainID"))
        chain_name = first(text, "ChainName", "ChainNm")
        groups = [(first(b, "SubChainName", "SubChainNm"), b) for b in blocks(text, "SubChain")] or [("", text)]
        count = 0
        for sub_name, body in groups:
            for sb in blocks(body, "Store") or blocks(body, "Branch"):
                f = fields(sb)
                sid = digits(f.get("storeid", ""))
                if not sid:
                    continue
                key = f"{chain_id}-{int(sid)}"
                stores[key] = {
                    "id": key, "scraper": name, "chain_id": chain_id, "store_id": int(sid),
                    "chain": chain_label(name, chain_name, sub_name),
                    "name": f.get("storename", "").strip(),
                    "address": f.get("address", "").strip(),
                    "city": city_name(f.get("city", "")),
                }
                count += 1
        log(f"   {name}: {count} stores ({chain_name or chain_id})")
    return stores


def assign_missing_cities(stores, wanted_keys):
    """Some chains leave the city empty; look for a chosen city inside the store name or address."""
    for s in stores.values():
        if s["city"]:
            continue
        hay = city_key(s["name"] + " " + s["address"])
        for key, canon in wanted_keys.items():
            if key and key in hay:
                s["city"] = canon
                break


# ---------- step 2: prices for the chosen stores ----------
def load_prices(selected):
    by_scraper = defaultdict(list)
    for s in selected:
        by_scraper[s["scraper"]].append(s)
    prices = defaultdict(dict)  # barcode -> {store key: price}
    info = {}                   # barcode -> Counter of (name, manufacturer, size)
    priced = set()
    for name, group in by_scraper.items():
        ids = "|".join(sorted({str(s["store_id"]) for s in group}))
        # PriceFull<chain>-<store>-<time> or PriceFull<chain>-<subchain>-<store>-<time>
        regex = rf"(?i)pricefull\d+-(?:\d+-)?0*(?:{ids})-\d{{8,}}"
        before = set(files_in(DUMPS, "pricefull"))
        t0 = time.time()
        scrape([name], ["PRICE_FULL_FILE"], regex=regex)
        new = set(files_in(DUMPS, "pricefull")) - before
        latest = {}
        for p in new:
            text_head = None
            m = re.match(r"(?i)pricefull(\d+)-(?:\d+-)?(\d+)-\d{8,}", p.name)
            if not m:
                continue
            key = f"{m.group(1)}-{int(m.group(2))}"
            if key not in {s["id"] for s in group}:
                continue
            if key not in latest or stamp(p.name) > stamp(latest[key].name):
                latest[key] = p
        items = 0
        for key, path in latest.items():
            try:
                text = read_text(path)
            except Exception as exc:  # pylint: disable=broad-except
                log("   unreadable", path.name, repr(exc)[:120])
                continue
            for block in blocks(text, "Item") or blocks(text, "Product"):
                f = fields(block)
                code = digits(f.get("itemcode", ""))
                # Short codes are a chain's own numbering (mostly fruit, vegetables, deli) and cannot be compared.
                if len(code) < 8:
                    continue
                try:
                    price = float(f.get("itemprice", "") or 0)
                except ValueError:
                    continue
                if price <= 0:
                    continue
                prices[code][key] = round(price, 2)
                nm = re.sub(r"\s+", " ", f.get("itemname", "") or f.get("itemnm", "")).strip()
                mf = re.sub(r"\s+", " ", f.get("manufacturername", "") or f.get("manufacturename", "")).strip()
                qty, unit = f.get("quantity", ""), f.get("unitqty", "") or f.get("unitofmeasure", "")
                try:
                    size = f"{float(qty):g} {unit}".strip() if qty and float(qty) > 0 else ""
                except ValueError:
                    size = ""
                if nm:
                    info.setdefault(code, Counter())[(nm, mf, size)] += 1
                items += 1
            priced.add(key)
        log(f"   {name}: {len(latest)}/{len(group)} stores, {items} prices, {time.time() - t0:.0f}s")
        shutil.rmtree(DUMPS, ignore_errors=True)  # keep the disk small between chains
    return prices, info, priced


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    scrapers = ONLY_SCRAPERS or ScraperFactory.all_scrapers_name()
    cities = chosen_cities()
    wanted = {city_key(c): CANON.get(city_key(c), c) for c in cities}
    log("cities:", ", ".join(wanted.values()))
    log("step 1: store lists for", len(scrapers), "chains")
    shutil.rmtree(DUMPS, ignore_errors=True)
    stores = load_stores(scrapers)
    assign_missing_cities(stores, wanted)
    shutil.rmtree(DUMPS, ignore_errors=True)
    if not stores:
        log("no store lists could be downloaded; keeping the previous data")
        sys.exit(1)

    all_cities = Counter(s["city"] for s in stores.values() if s["city"])
    selected = [s for s in stores.values() if city_key(s["city"]) in wanted]
    log(f"step 2: prices for {len(selected)} stores in the chosen cities (of {len(stores)} stores nationwide)")
    prices, info, priced = load_prices(selected)

    final = sorted((s for s in selected if s["id"] in priced), key=lambda s: (s["city"], s["chain"], s["name"]))
    index = {s["id"]: i for i, s in enumerate(final)}
    if OUT.exists():
        shutil.rmtree(OUT)
    shards = defaultdict(dict)
    catalog = []
    for code, per_store in prices.items():
        row = {str(index[k]): v for k, v in per_store.items() if k in index}
        if not row:
            continue
        shards[code[-2:]][code] = row
        name, manufacturer, size = info[code].most_common(1)[0][0] if code in info else ("", "", "")
        if name:
            catalog.append([code, name, manufacturer, size, len(row)])
    catalog.sort(key=lambda r: (-r[4], r[1]))
    for shard, body in shards.items():
        write(OUT / "prices" / f"{shard}.json", body)
    write(OUT / "catalog.json", [r[:4] for r in catalog])
    write(OUT / "cities.json", [[c, n] for c, n in all_cities.most_common()])
    write(OUT / "meta.json", {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "cities": list(wanted.values()),
        "stores": [{k: s[k] for k in ("id", "chain", "name", "address", "city")} for s in final],
        "missing": sorted({s["chain"] for s in selected if s["id"] not in priced}),
    })
    log(f"done: {len(final)} stores with prices, {len(catalog)} products, {len(shards)} price files")
    if not final:
        sys.exit(1)


if __name__ == "__main__":
    main()
