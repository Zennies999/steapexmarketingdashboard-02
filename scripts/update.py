#!/usr/bin/env python3
"""Daily updater for Steapex Market Pulse.

Step 1  ดึงพาดหัวข่าวจาก (ก) RSS ของเว็บที่เลือกไว้โดยตรง (config.json → "feeds")
        และ (ข) Google News RSS ตามแบรนด์/ตลาด (ไม่ต้องใช้ API key ทั้งคู่)
Step 2  (ถ้ามี ANTHROPIC_API_KEY) ให้ Claude คัดข่าวที่เกี่ยวข้อง เขียนสรุปภาษาไทย
        และบทเรียนสำหรับ Steapex แล้วอัปเดต items / daily / brands

ใช้เฉพาะ standard library ของ Python 3.9+
ตัวแปรสภาพแวดล้อม:
  ANTHROPIC_API_KEY   (ไม่บังคับ) เปิดโหมดวิเคราะห์ด้วย Claude
  ANTHROPIC_MODEL     (ไม่บังคับ) ค่าเริ่มต้น claude-sonnet-5-5
  DRY_RUN=1           ไม่เขียนไฟล์ แสดงผลลัพธ์อย่างเดียว
  RSS_FIXTURE=path    อ่าน Google News RSS จากไฟล์ (ใช้ทดสอบ)
  FEED_FIXTURE_DIR=d  อ่าน feeds จาก d/<feed id>.xml (ใช้ทดสอบ)
"""
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (compatible; SteapexMarketPulse/1.1; RSS reader; +https://github.com)"
KEEP_HEADLINE_DAYS = 45
KEEP_ITEMS = 500
KEEP_DAILY = 30
MAX_HEADLINES_TO_AI = 120
KINDS = {"news", "product", "campaign", "finance"}
MOMENTUM = {"up", "steady", "down", "watch"}
LOCALES = {
    "en": ("en-US", "US", "US:en"),
    "th": ("th", "TH", "TH:th"),
    "zh": ("zh-CN", "CN", "CN:zh-Hans"),
}


def log(*a):
    print(*a, file=sys.stderr)


def load(name, default):
    p = DATA / name
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save(name, obj):
    if os.environ.get("DRY_RUN"):
        log(f"[dry-run] would write data/{name}")
        return
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def http(url, data=None, headers=None, timeout=40):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_feed(url):
    """ดึง feed ตรง: บางเว็บ (เช่น newsroom ของ adidas) ตอบ 200 แต่เนื้อหาว่าง ถ้า Accept ไม่ตรงที่เซิร์ฟเวอร์ต้องการ
    จึงลองหลายแบบ โดยยังใช้ User-Agent เดิมที่ระบุตัวตนตรงๆ (ไม่แกล้งเป็นเบราว์เซอร์)"""
    variants = [
        {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"},
        {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        {},
    ]
    last = "no response"
    for hdr in variants:
        try:
            raw = http(url, headers=hdr)
            if raw.strip():
                return raw
            last = "empty body (HTTP 200, 0 bytes)"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)[:100]
    raise RuntimeError(last)


# ---------------------------------------------------------------- RSS
def rss_url(q, lang, days):
    hl, gl, ceid = LOCALES.get(lang, LOCALES["en"])
    query = f"{q} when:{days}d"
    return (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})
    )


ATOM = "{http://www.w3.org/2005/Atom}"


def clean_text(x):
    x = html.unescape(re.sub(r"<[^>]+>", "", x or ""))
    return re.sub(r"\s+", " ", x).strip()


def parse_date(raw):
    """RSS (RFC 822) หรือ Atom (ISO 8601) → datetime UTC; อ่านไม่ได้ = ตอนนี้"""
    try:
        if raw and re.match(r"\d{4}-\d{2}-\d{2}", raw.strip()):
            d = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
        return email.utils.parsedate_to_datetime(raw).astimezone(dt.timezone.utc)
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def parse_rss(xml_bytes, default_source="Google News"):
    """รองรับ RSS 2.0 และ Atom"""
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = clean_text(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not link.startswith("http"):
            g = (it.findtext("guid") or "").strip()
            link = g if g.startswith("http") else link
        src_el = it.find("source")
        source = clean_text(src_el.text) if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        d = parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "source": source or default_source,
                        "published": d.strftime("%Y-%m-%dT%H:%M:%SZ")})
    for it in root.iter(ATOM + "entry"):
        title = clean_text(it.findtext(ATOM + "title"))
        link = ""
        for l in it.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href").strip()
                break
        d = parse_date(it.findtext(ATOM + "published") or it.findtext(ATOM + "updated"))
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "source": default_source,
                        "published": d.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def fetch_queries(queries, days, tag, stats=None):
    got = []
    fixture = os.environ.get("RSS_FIXTURE")
    for q in queries:
        if stats is not None:
            stats["queries"] += 1
        try:
            if fixture:
                raw = Path(fixture).read_bytes()
            else:
                raw = http(rss_url(q["q"], q.get("lang", "en"), days))
            for h in parse_rss(raw):
                h["tag"] = tag
                got.append(h)
        except Exception as e:  # network/parse failures must not kill the whole run
            log(f"  ! {tag} / {q['q']}: {e}")
            if stats is not None:
                stats["failed"] += 1
        if not fixture:
            time.sleep(0.6)
    return got


# ---------------------------------------------------------------- direct feeds
def term_regex(term, whole=True):
    """ตัวอักษรละติน/ตัวเลขต้องไม่ติดกับตัวอักษรอื่น (กัน 'Brooks' ชน 'Brooksby'); ไทย/จีนใช้ substring"""
    t = term.strip().lower()
    pre = r"(?<![0-9a-z])" if re.match(r"[0-9a-z]", t[0]) else ""
    post = r"(?![0-9a-z])" if whole and re.match(r"[0-9a-z]", t[-1]) else ""
    return re.compile(pre + re.escape(t) + post)


def build_matchers(cfg):
    aliases = []
    for b in cfg["brands"]:
        terms = b.get("aliases") or [b["name"]]
        aliases.append((b["id"], [term_regex(a) for a in terms if a.strip()]))
    kws = [term_regex(k, whole=False) for k in cfg.get("market_keywords", []) if k.strip()]
    return aliases, kws


def match_brands(text, aliases):
    t = text.lower()
    hits = []
    for bid, rxs in aliases:
        pos = [m.start() for rx in rxs for m in [rx.search(t)] if m]
        if pos:
            hits.append((min(pos), bid))
    return [b for _, b in sorted(hits)]


def fetch_feeds(cfg, days, now):
    """ดึง RSS ของเว็บที่เลือกไว้ → (รายการพาดหัว, สถานะรายแหล่งสำหรับแสดงบนหน้าเว็บ)"""
    aliases, kws = build_matchers(cfg)
    fixture_dir = os.environ.get("FEED_FIXTURE_DIR")
    cutoff = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items, status = [], []
    for f in cfg.get("feeds", []):
        if f.get("enabled") is False:
            continue
        st = {"id": f["id"], "name": f["name"], "stype": f.get("stype", "media"), "ok": False,
              "total": 0, "fresh": 0, "kept": 0, "newest": None}
        try:
            if fixture_dir:
                raw = (Path(fixture_dir) / f"{f['id']}.xml").read_bytes()
            else:
                raw = http_feed(f["url"])
            parsed = parse_rss(raw, f["name"])
            st["ok"] = True
            st["total"] = len(parsed)
            if parsed:
                st["newest"] = max(h["published"] for h in parsed)[:10]
            for h in parsed:
                if h["published"] < cutoff:
                    continue
                st["fresh"] += 1
                fixed = f.get("brand")
                found = match_brands(h["title"], aliases)
                ids = ([fixed] + [b for b in found if b != fixed]) if fixed else found
                if f.get("filter", "relevant") == "relevant" and not fixed:
                    if not ids and not any(rx.search(h["title"].lower()) for rx in kws):
                        continue
                h.update({"tag": ids[0] if ids else "market", "tags": ids,
                          "stype": st["stype"], "via": f["id"]})
                items.append(h)
                st["kept"] += 1
        except Exception as e:  # แหล่งเดียวพังต้องไม่ทำให้ทั้งระบบพัง
            st["error"] = str(e)[:140]
            log(f"  ! feed {f['id']}: {e}")
        status.append(st)
        log(f"  feed {f['id']}: ok={st['ok']} total={st['total']} fresh={st['fresh']} kept={st['kept']}")
        if not fixture_dir:
            time.sleep(0.6)
    return items, status


def norm(t):
    return re.sub(r"[^0-9a-zA-Z฀-๿一-鿿]+", "", t.lower())[:80]


# ---------------------------------------------------------------- Claude
SYSTEM = (
    "คุณคือนักวิเคราะห์ตลาดรองเท้ากีฬาของ Steapex แบรนด์รองเท้าวิ่งน้องใหม่ในไทย "
    "(จุดยืน: รองเท้าวิ่งตัวจริงราคาเข้าถึงได้ สำหรับนักวิ่งมือใหม่ ถนนและเทรล คู่แข่งตรงคือ ONKE และ XTEP) "
    "ตอบเป็นภาษาไทย กระชับ ใช้เฉพาะข้อเท็จจริงที่อยู่ในพาดหัวที่ให้มาเท่านั้น "
    "ห้ามแต่งตัวเลข ชื่อ วันที่ หรือรายละเอียดเพิ่ม ถ้าพาดหัวให้ข้อมูลน้อยให้เขียนสั้นและบอกว่า 'ข้อมูลจากพาดหัวเท่านั้น' "
    "ข้อสังเกตสำหรับ Steapex ต้องแยกจากข้อเท็จจริงและเป็นความเห็น ตอบเป็น JSON ล้วนเท่านั้น ไม่มีข้อความอื่น"
)

SCHEMA_HINT = """โครงสร้าง JSON ที่ต้องส่งกลับ:
{
 "items":[{"refs":[เลขพาดหัว,...],"brand":"id แบรนด์ (ต้องตรงกับรายการแบรนด์)","kind":"news|product|campaign|finance",
           "heat":1-5,"title":"หัวข้อไทย","summary":"สรุปไทย 1-3 ประโยค","insight":"Steapex ควรเรียนรู้อะไร 1-2 ประโยค"}],
 "daily":{"takeaway":"สรุปภาพรวมของวัน 2-4 ประโยค","actions":["ข้อเสนอที่ลงมือได้ 2-4 ข้อ"],
          "trends":[{"title":"","summary":"","heat":1-5,"brands":["id"],"steapex":"","refs":[เลขพาดหัว]}]},
 "brands":{"id แบรนด์":{"momentum":"up|steady|down|watch","headline":"สถานะล่าสุด 1 ประโยค","lesson":"บทเรียนสำหรับ Steapex","actions":["1-2 ข้อ"]}}
}
กฎ: 1) คัดเฉพาะข่าวที่เกี่ยวกับรองเท้ากีฬา/วิ่ง/ธุรกิจแบรนด์ ทิ้งข่าวไม่เกี่ยว (แฟชั่นล้วน บาสเก็ตล้วน โปรโมชันร้านค้าทั่วไป)
2) ข่าวเรื่องเดียวกันจากหลายแหล่งให้รวมเป็นรายการเดียวและใส่ refs ทั้งหมด 3) refs ต้องเป็นเลขที่มีในรายการเท่านั้น
4) brands ให้ใส่เฉพาะแบรนด์ที่มีข่าวใหม่ 5) trends 3-6 ข้อ เรียงตามความร้อนแรง 6) items ไม่เกิน 25 รายการ"""


def call_claude(prompt):
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    body = json.dumps({
        "model": model, "max_tokens": 12000, "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = None
    for attempt in range(3):
        try:
            res = json.loads(http("https://api.anthropic.com/v1/messages", body, headers, timeout=240))
            text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
            return text
        except urllib.error.HTTPError as e:
            last = f"{e.code} {e.read()[:300]!r}"
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(8 * (attempt + 1))
                continue
            break
        except Exception as e:
            last = str(e)
            time.sleep(5)
    raise RuntimeError(f"Claude API failed: {last}")


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s : e + 1])


def curate(new_headlines, cfg, today):
    brand_ids = {b["id"] for b in cfg["brands"]}
    brand_lines = ", ".join(f'{b["id"]}={b["name"]}' for b in cfg["brands"])
    lines = []
    for i, h in enumerate(new_headlines):
        tg = "/".join(h.get("tags") or [h["tag"]])
        if h.get("stype") == "official":
            tg += ", official"
        lines.append(f'[{i}] ({tg}) {h["published"][:10]} | {h["source"]} | {h["title"]}')
    prompt = (
        f"วันนี้ {today}\nรายการแบรนด์ (id=ชื่อ): {brand_lines}\n"
        "แท็กในวงเล็บคือแบรนด์หรือ 'market' ที่คำค้นหรือแหล่งข่าวจับคู่ไว้ (อาจไม่ตรงเนื้อข่าว ให้ตัดสินจากพาดหัว)\n"
        "แท็ก official = พาดหัวจาก newsroom ของแบรนด์เอง เป็นถ้อยแถลงของแบรนด์ ให้เขียนว่าเป็นข้อมูลจากแบรนด์ ไม่ใช่ข้อเท็จจริงที่ยืนยันโดยอิสระ\n\n"
        "พาดหัวข่าวใหม่:\n" + "\n".join(lines) + "\n\n" + SCHEMA_HINT
    )
    out = extract_json(call_claude(prompt))

    items, used = [], set()
    for it in out.get("items", []):
        refs = [r for r in it.get("refs", []) if isinstance(r, int) and 0 <= r < len(new_headlines)]
        brand = it.get("brand")
        if not refs or brand not in brand_ids or not it.get("title"):
            continue
        used.update(refs)
        srcs, seen = [], set()
        for r in refs:
            h = new_headlines[r]
            if h["url"] not in seen:
                seen.add(h["url"])
                srcs.append({"name": h["source"], "url": h["url"]})
        date = max(new_headlines[r]["published"][:10] for r in refs)
        kind = it.get("kind") if it.get("kind") in KINDS else "news"
        items.append({
            "id": "a-" + hashlib.sha1((brand + it["title"]).encode()).hexdigest()[:10],
            "brand": brand, "date": date, "heat": max(1, min(5, int(it.get("heat") or 2))),
            "kind": kind, "platform": srcs[0]["name"], "title": it["title"].strip(),
            "summary": (it.get("summary") or "").strip(), "insight": (it.get("insight") or "").strip(),
            "sources": srcs[:4], "auto": True,
        })

    trends = []
    for t in out.get("daily", {}).get("trends", []):
        refs = [r for r in t.get("refs", []) if isinstance(r, int) and 0 <= r < len(new_headlines)]
        srcs, seen = [], set()
        for r in refs:
            h = new_headlines[r]
            if h["url"] not in seen:
                seen.add(h["url"])
                srcs.append({"name": h["source"], "url": h["url"]})
        if t.get("title") and srcs:
            trends.append({
                "title": t["title"], "summary": t.get("summary", ""),
                "heat": max(1, min(5, int(t.get("heat") or 3))),
                "brands": [b for b in t.get("brands", []) if b in brand_ids],
                "steapex": t.get("steapex", ""), "sources": srcs[:4],
            })
    d = out.get("daily", {})
    daily = {"id": today, "date": today, "takeaway": d.get("takeaway", ""),
             "actions": [a for a in d.get("actions", []) if isinstance(a, str)][:4],
             "trends": trends, "auto": True}

    brands = {}
    for bid, b in (out.get("brands") or {}).items():
        if bid in brand_ids and isinstance(b, dict):
            brands[bid] = {
                "momentum": b.get("momentum") if b.get("momentum") in MOMENTUM else "steady",
                "headline": b.get("headline", ""), "lesson": b.get("lesson", ""),
                "actions": [a for a in b.get("actions", []) if isinstance(a, str)][:3],
            }
    return items, daily, brands


# ---------------------------------------------------------------- main
def main():
    cfg = load("config.json", None)
    if not cfg:
        sys.exit("data/config.json not found")
    now = dt.datetime.now(dt.timezone.utc)
    # วันที่ตามเวลาไทย
    today = (now + dt.timedelta(hours=7)).strftime("%Y-%m-%d")

    store = load("headlines.json", {"updatedAt": None, "items": []})
    known = {h["url"] for h in store["items"]} | {norm(h["title"]) for h in store["items"]}
    first_run = not store["items"]
    days = cfg["lookback_days_first_run"] if first_run else cfg["lookback_days_daily"]
    log(f"fetching headlines (last {days} days, first_run={first_run}) …")

    # 1) RSS ของเว็บที่เลือกไว้ (มาก่อน เพื่อให้ลิงก์ต้นทางตรงๆ ชนะลิงก์ผ่าน Google News เมื่อข่าวซ้ำกัน)
    feed_days = cfg.get("feed_lookback_days_first_run", 30) if first_run else cfg.get("feed_lookback_days_daily", 7)
    log(f"fetching {len(cfg.get('feeds', []))} direct feeds (last {feed_days} days) …")
    fetched, feed_status = fetch_feeds(cfg, feed_days, now)
    n_feed = len(fetched)

    # 2) Google News ตามแบรนด์/ตลาด
    gstats = {"queries": 0, "failed": 0}
    for b in cfg["brands"]:
        fetched += fetch_queries(b["queries"], days, b["id"], gstats)
    fetched += fetch_queries(cfg["market_queries"], days, "market", gstats)
    log(f"  fetched {len(fetched)} raw headlines ({n_feed} from direct feeds)")

    new = []
    for h in fetched:
        k = norm(h["title"])
        if h["url"] in known or k in known:
            continue
        known.add(h["url"]); known.add(k)
        h["added"] = today
        new.append(h)
    new.sort(key=lambda h: h["published"], reverse=True)
    log(f"  {len(new)} new after de-dup")

    # เก็บพาดหัวย้อนหลัง
    cutoff = (now - dt.timedelta(days=KEEP_HEADLINE_DAYS)).strftime("%Y-%m-%d")
    items_hl = [h for h in store["items"] + new if h["published"][:10] >= cutoff]
    items_hl.sort(key=lambda h: h["published"], reverse=True)
    store = {"updatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items_hl[:1500]}
    save("headlines.json", store)

    meta = load("meta.json", {})
    meta.update({"updatedAt": store["updatedAt"], "newHeadlines": len(new),
                 "mode": "headlines", "sources": feed_status,
                 "googleNews": {**gstats, "raw": len(fetched) - n_feed}})

    if os.environ.get("ANTHROPIC_API_KEY") and new:
        batch = new[:MAX_HEADLINES_TO_AI]
        log(f"asking Claude to curate {len(batch)} headlines …")
        try:
            items, daily, brands = curate(batch, cfg, today)
            cur = load("items.json", [])
            have = {i["id"] for i in cur}
            cur = [i for i in items if i["id"] not in have] + cur
            cur.sort(key=lambda i: (i["date"], i.get("heat", 0)), reverse=True)
            save("items.json", cur[:KEEP_ITEMS])

            ds = [d for d in load("daily.json", []) if d["id"] != today]
            if daily["trends"] or daily["takeaway"]:
                ds.insert(0, daily)
            ds.sort(key=lambda d: d["date"], reverse=True)
            save("daily.json", ds[:KEEP_DAILY])

            bs = load("brands.json", {})
            bs.update(brands)
            save("brands.json", bs)
            meta.update({"mode": "ai", "curatedItems": len(items)})
            log(f"  + {len(items)} items, {len(daily['trends'])} trends, {len(brands)} brand updates")
        except Exception as e:
            log(f"  ! curation failed, keeping headlines only: {e}")
            meta["note"] = f"AI curation failed: {str(e)[:160]}"
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        log("no ANTHROPIC_API_KEY → headlines-only mode")

    save("meta.json", meta)
    log("done")


if __name__ == "__main__":
    main()
