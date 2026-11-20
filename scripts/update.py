#!/usr/bin/env python3
"""Daily updater for Steapex Market Pulse.

Step 1  ดึงพาดหัวข่าวจาก Google News RSS ตามแบรนด์/ตลาด (ไม่ต้องใช้ API key)
Step 2  (ถ้ามี ANTHROPIC_API_KEY) ให้ Claude คัดข่าวที่เกี่ยวข้อง เขียนสรุปภาษาไทย
        และบทเรียนสำหรับ Steapex แล้วอัปเดต items / daily / brands

ใช้เฉพาะ standard library ของ Python 3.9+
ตัวแปรสภาพแวดล้อม:
  ANTHROPIC_API_KEY   (ไม่บังคับ) เปิดโหมดวิเคราะห์ด้วย Claude
  ANTHROPIC_MODEL     (ไม่บังคับ) ค่าเริ่มต้น claude-sonnet-5-5
  DRY_RUN=1           ไม่เขียนไฟล์ แสดงผลลัพธ์อย่างเดียว
  RSS_FIXTURE=path    อ่าน RSS จากไฟล์ (ใช้ทดสอบ)
"""
import datetime as dt
import email.utils
import hashlib
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
UA = "Mozilla/5.0 (compatible; SteapexMarketPulse/1.0; +https://github.com)"
KEEP_HEADLINE_DAYS = 45
KEEP_ITEMS = 500
KEEP_DAILY = 30
MAX_HEADLINES_TO_AI = 90
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


# ---------------------------------------------------------------- RSS
def rss_url(q, lang, days):
    hl, gl, ceid = LOCALES.get(lang, LOCALES["en"])
    query = f"{q} when:{days}d"
    return (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})
    )


def parse_rss(xml_bytes):
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        try:
            d = email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc)
        except Exception:
            d = dt.datetime.now(dt.timezone.utc)
        if title and link:
            out.append({"title": title, "url": link, "source": source or "Google News",
                        "published": d.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def fetch_queries(queries, days, tag):
    got = []
    fixture = os.environ.get("RSS_FIXTURE")
    for q in queries:
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
        if not fixture:
            time.sleep(0.6)
    return got


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
        lines.append(f'[{i}] ({h["tag"]}) {h["published"][:10]} | {h["source"]} | {h["title"]}')
    prompt = (
        f"วันนี้ {today}\nรายการแบรนด์ (id=ชื่อ): {brand_lines}\n"
        "แท็กในวงเล็บคือแบรนด์หรือ 'market' ที่คำค้นพบข่าวนั้น (อาจไม่ตรงเนื้อข่าว ให้ตัดสินจากพาดหัว)\n\n"
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

    fetched = []
    for b in cfg["brands"]:
        fetched += fetch_queries(b["queries"], days, b["id"])
    fetched += fetch_queries(cfg["market_queries"], days, "market")
    log(f"  fetched {len(fetched)} raw headlines")

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
                 "mode": "headlines"})

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
