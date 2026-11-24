#!/usr/bin/env python3
"""Monitor de stock PS5 Pro; UNKNOWN nunca genera alertas."""
import os, re, sys, json
from datetime import datetime, timezone, timedelta
from html import unescape
import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
IN_STOCK, OUT_OF_STOCK, UNKNOWN = "IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Cache-Control": "no-cache"}
EMOJI = {IN_STOCK: "✅", OUT_OF_STOCK: "❌", UNKNOWN: "❔"}
PRODUCT_PATTERNS = ["ps5 pro", "playstation 5 pro", "playstation5 pro", "playstation®5 pro"]
OUT_PATTERNS = ["sold out", "out of stock", "currently unavailable",
                "temporarily out of stock", "coming soon", "notify me when",
                "out-of-stock", "soldout", "get notified", "no longer available"]
IN_PATTERNS = ["add to cart", "add to bag", "addtocart", "add-to-cart", "buy now"]
SCHEMA_IN = {"instock", "limitedavailability", "onlineonly", "preorder"}
SCHEMA_OUT = {"outofstock", "soldout", "discontinued", "backorder"}
OPEN_BOX_RE = re.compile(r"\bopen[\s-]+box\b", re.I)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"WARN no pude leer {path}: {e}")
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def result(retailer, product, status, url, detail):
    return {"retailer": retailer, "product": product, "status": status,
            "url": url, "detail": detail}


def fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8", referer=None):
    """Intenta Chrome TLS; si falla, reintenta con requests/HTTP 1.1."""
    headers = dict(HEADERS, Accept=accept)
    if referer:
        headers["Referer"] = referer
    if cffi_requests is not None:
        try:
            return cffi_requests.get(url, impersonate="chrome", headers=headers, timeout=30)
        except Exception as e:
            log(f"curl_cffi fallo; reintento HTTP/1.1: {str(e)[:100]}")
    return requests.get(url, headers=headers, timeout=25)


def is_open_box(value):
    return bool(OPEN_BOX_RE.search(unescape(str(value or ""))))


def iter_ldjson_products(text):
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                typ = item.get("@type")
                if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
                    yield item


def page_is_open_box(text, url=""):
    if is_open_box(url):
        return True
    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        attrs = dict(re.findall(r"([\w:-]+)\s*=\s*['\"](.*?)['\"]", tag, re.S))
        if (attrs.get("property") or attrs.get("name")) in ("og:title", "title") and is_open_box(attrs.get("content")):
            return True
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if title and is_open_box(title.group(1)):
        return True
    for item in iter_ldjson_products(text):
        name = item.get("name") or ""
        if any(p in name.lower() for p in PRODUCT_PATTERNS) and is_open_box(name):
            return True
    return False


def ldjson_availability(text):
    avails, price = set(), None
    for item in iter_ldjson_products(text):
        name = (item.get("name") or "").lower()
        if name and not any(p in name for p in PRODUCT_PATTERNS):
            continue
        offers = item.get("offers") or {}
        for offer in (offers if isinstance(offers, list) else [offers]):
            if not isinstance(offer, dict):
                continue
            avail = (offer.get("availability") or "").rsplit("/", 1)[-1].lower()
            if avail:
                avails.add(avail)
                price = price or offer.get("price")
    return avails, price


def check_one_html(name, url, out_extra=()):
    try:
        r = fetch(url)
        if r.status_code in (403, 429, 503):
            return result(name, url, UNKNOWN, url, f"bloqueado (HTTP {r.status_code})")
        if r.status_code >= 400:
            return result(name, url, UNKNOWN, url, f"HTTP {r.status_code}")
        if page_is_open_box(r.text, getattr(r, "url", url)):
            return result(name, url, OUT_OF_STOCK, url, "ignorado: oferta Open Box")
        body = r.text.lower()
        if not any(p in body for p in PRODUCT_PATTERNS):
            return result(name, url, UNKNOWN, url, "pagina sin datos del producto (bloqueo o shell JS)")

        # Best Buy: exigir vendedor directo antes de aceptar cualquier senal
        # de disponibilidad, incluso JSON-LD. Si no se puede verificar, UNKNOWN.
        if name == "Best Buy":
            direct_seller = any(s in body for s in (
                "sold by best buy", "seller: best buy", '"sellername":"best buy"',
                '"seller":"best buy"'))
            if not direct_seller:
                return result(name, url, UNKNOWN, url,
                              "no se pudo verificar venta directa de Best Buy")

        avails, price = ldjson_availability(r.text)
        if avails:
            ins, outs = avails & SCHEMA_IN, avails & SCHEMA_OUT
            tag = "/".join(sorted(avails)) + (f", ${price}" if price else "")
            if ins and not outs:
                # En Abt el schema/boton de compra no es prueba suficiente:
                # ya se observo una pagina agotada con esas senales presentes.
                if name == "Abt":
                    return result(name, url, UNKNOWN, url,
                                  f"JSON-LD no concluyente para Abt: {tag}")
                return result(name, url, IN_STOCK, url, f"JSON-LD: {tag}")
            if outs and not ins:
                return result(name, url, OUT_OF_STOCK, url, f"JSON-LD: {tag}")

        out_hit = next((p for p in list(out_extra) + OUT_PATTERNS if p in body), None)
        if out_hit:
            return result(name, url, OUT_OF_STOCK, url, f"detecto '{out_hit}'")
        in_hit = next((p for p in IN_PATTERNS if p in body), None)

        # Abt ya dio un falso positivo: el boton Add to cart no basta.
        if name == "Abt":
            detail = f"senal de compra no concluyente: '{in_hit}'" if in_hit else "sin senales claras"
            return result(name, url, UNKNOWN, url, detail)


        if in_hit:
            return result(name, url, IN_STOCK, url, f"detecto '{in_hit}'")
        return result(name, url, UNKNOWN, url, "sin senales claras de disponibilidad")
    except Exception as e:
        return result(name, url, UNKNOWN, url, f"error: {str(e)[:120]}")


def make_html_checker(display_name):
    def checker(cfg):
        return [check_one_html(display_name, u, cfg.get("out_extra", [])) for u in cfg.get("urls", [])]
    return checker


def check_walmart(cfg):
    results = []
    for url in cfg.get("urls", []):
        try:
            r = fetch(url)
            if r.status_code in (403, 429, 503) or r.status_code >= 400:
                results.append(result("Walmart", url, UNKNOWN, url, f"HTTP {r.status_code}"))
                continue
            if page_is_open_box(r.text, getattr(r, "url", url)):
                results.append(result("Walmart", url, OUT_OF_STOCK, url, "ignorado: oferta Open Box"))
                continue
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if not m:
                results.append(result("Walmart", url, UNKNOWN, url, "sin datos __NEXT_DATA__"))
                continue
            data = json.loads(m.group(1))
            prod = ((((data.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("data") or {}).get("product") or {}
            status_raw = (prod.get("availabilityStatus") or "").upper()
            name = prod.get("name") or url
            price = ((prod.get("priceInfo") or {}).get("currentPrice") or {}).get("priceString", "")
            seller = (prod.get("sellerDisplayName") or prod.get("sellerName") or "").strip()
            if is_open_box(name):
                results.append(result("Walmart", name, OUT_OF_STOCK, url, "ignorado: oferta Open Box"))
            elif not seller:
                results.append(result("Walmart", name, UNKNOWN, url, "vendedor no identificado"))
            elif seller.casefold() != "walmart.com":
                results.append(result("Walmart", name, UNKNOWN, url, f"ignorado: vendido por tercero ({seller})"))
            elif status_raw in ("IN_STOCK", "OUT_OF_STOCK"):
                status = IN_STOCK if status_raw == "IN_STOCK" else OUT_OF_STOCK
                detail = f"Vendido por {seller}; availabilityStatus={status_raw}" + (f", {price}" if price else "")
                results.append(result("Walmart", name, status, url, detail))
            else:
                results.append(result("Walmart", name, UNKNOWN, url, f"availabilityStatus no concluyente: {status_raw or 'vacio'}"))
        except Exception as e:
            results.append(result("Walmart", url, UNKNOWN, url, f"error: {str(e)[:120]}"))
    return results


def check_newegg(cfg):
    results = []
    for item in cfg.get("items", []):
        page = f"https://www.newegg.com/p/{item}"
        try:
            r = fetch(f"https://www.newegg.com/product/api/ProductRealtime?ItemNumber={item}", "application/json", page)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                results.append(result("Newegg", item, UNKNOWN, page, f"API bloqueada (HTTP {r.status_code})"))
                continue
            main = r.json().get("MainItem") or {}
            instock = main.get("Instock")
            if instock is None:
                results.append(result("Newegg", item, UNKNOWN, page, "API sin campo Instock"))
                continue
            title = main.get("Title") or item
            if is_open_box(title):
                results.append(result("Newegg", title, OUT_OF_STOCK, page, "ignorado: oferta Open Box"))
            else:
                results.append(result("Newegg", title, IN_STOCK if instock else OUT_OF_STOCK, page, f"Instock={instock}"))
        except Exception as e:
            results.append(result("Newegg", item, UNKNOWN, page, f"error: {str(e)[:120]}"))
    return results


CHECKERS = {"bestbuy": make_html_checker("Best Buy"), "walmart": check_walmart,
            "newegg": check_newegg, "samsclub": make_html_checker("Sam's Club"),
            "abt": make_html_checker("Abt"), "amazon": make_html_checker("Amazon"),
            "gamestop": make_html_checker("GameStop"),
            "playstation_direct": make_html_checker("PlayStation Direct")}


def run_all(config):
    results = []
    for name, cfg in config.get("retailers", {}).items():
        if not cfg.get("enabled", True):
            continue
        fn = CHECKERS.get(name)
        if not fn:
            log(f"WARN retailer desconocido: {name}")
            continue
        try:
            results.extend(fn(cfg))
        except Exception as e:
            results.append(result(name, "-", UNKNOWN, "", f"error inesperado: {e}"))
    return results


def tg_request(method, params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_chat_ids():
    raw = (os.environ.get("TELEGRAM_CHAT_IDS", "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    return [x.strip() for x in raw.split(",") if x.strip()]


def send_telegram(message):
    ids = get_chat_ids()
    if not ids:
        log("WARN falta TELEGRAM_CHAT_IDS")
        return False
    ok = 0
    for chat_id in ids:
        try:
            tg_request("sendMessage", {"chat_id": chat_id, "text": message,
                       "parse_mode": "HTML", "disable_web_page_preview": "false"})
            ok += 1
        except Exception as e:
            log(f"ERROR Telegram para {chat_id}: {e}")
    log(f"Telegram: enviado a {ok}/{len(ids)} chat(s)")
    return ok > 0


def cmd_get_chat_id():
    data = tg_request("getUpdates", {})
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    if not seen:
        print("No hay mensajes recientes. Mandale un mensaje al bot y volve a correr esto.")
    for cid, who in seen.items():
        print(f"chat_id={cid} ({who})")


def main():
    args = sys.argv[1:]
    config = load_json(CONFIG_PATH, {})
    if "--test-telegram" in args:
        ok = send_telegram("🔔 Test del checker de PS5 Pro. Si ves esto, Telegram esta bien configurado ✅")
        print("OK, mensaje enviado." if ok else "FALLO (revisa token/chat id).")
        return
    if "--get-chat-id" in args:
        cmd_get_chat_id()
        return

    report_only = "--once-report" in args
    results = run_all(config)
    log("Resumen del chequeo:")
    for item in results:
        print(f"  {EMOJI.get(item['status'], '?')} {item['retailer']:<20} {item['status']:<13} {item['detail']}")
    if report_only:
        return

    state = load_json(STATE_PATH, {})
    now = datetime.now(timezone.utc)
    recheck_h = config.get("recheck_alert_hours", 6)
    alerts = []
    for item in results:
        key = f"{item['retailer']}::{item['product']}"
        prev = state.get(key, {})
        if item["status"] == IN_STOCK:
            last_alert = prev.get("last_alert")
            transitioned = prev.get("status") != IN_STOCK
            stale = False
            if last_alert:
                try:
                    stale = now - datetime.fromisoformat(last_alert) > timedelta(hours=recheck_h)
                except Exception:
                    stale = True
            if transitioned or stale or not last_alert:
                alerts.append(item)
                prev["last_alert"] = now.isoformat()
        prev["status"] = item["status"]
        prev["checked"] = now.isoformat()
        state[key] = prev
    save_json(STATE_PATH, state)

    if alerts:
        lines = ["🚨 <b>¡PS5 Pro CON STOCK!</b> 🚨", ""]
        for item in alerts:
            link = f'<a href="{item["url"]}">{item["retailer"]}</a>' if item["url"] else item["retailer"]
            lines.append(f"✅ {link} — {item['product']}\n<i>{item['detail']}</i>")
        lines += ["", "Corre a comprarla 🏃"]
        if send_telegram("\n".join(lines)):
            log(f"Enviadas {len(alerts)} alerta(s) por Telegram")
    else:
        log("Sin stock nuevo")
        if config.get("always_notify", False):
            lines = ["😴 No stock anywhere (por ahora)", ""]
            lines.extend(f"{EMOJI.get(x['status'], '?')} {x['retailer']} — {x['detail']}" for x in results)
            if not results:
                lines.append("⚠️ Nada configurado para chequear")
            send_telegram("\n".join(lines))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
