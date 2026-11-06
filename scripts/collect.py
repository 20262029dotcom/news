"""부동산 뉴스를 지역별로 모아 Gemini로 판별·분류·요약하고 docs/ 아래 JSON으로 저장한다.

지역 (REGIONS)
  gyeonggi : 경기·인천 — 중부일보 건설·부동산 목록 + 전체·경제·경기·인천 RSS(키워드)  → docs/news.json
  seoul    : 서울     — 한국경제·서울경제 부동산 RSS (기사 앞부분은 기사 페이지 설명문) → docs/seoul.json

- 외부 패키지 없이 파이썬 표준 라이브러리만 쓴다.
- 이미 처리한 기사는 다시 보내지 않는다 (무료 한도 절약: 지역마다 실행 1회당 Gemini 요청 1회 이하).
- 환경 변수
    GEMINI_API_KEY : Google AI Studio에서 받은 키 (없으면 새 기사를 처리하지 않음)
    GEMINI_MODEL   : 사용할 모델 (기본 gemini-3.5-flash)
    KEEP_DAYS      : 며칠 치 기사를 보여 줄지 (기본 7)
    ONLY_REGION    : 한 지역만 실행할 때 (gyeonggi 또는 seoul)
"""
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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEO_PATH = os.path.join(ROOT, "data", "geocache.json")
UA = "Mozilla/5.0 (realestate-news-dashboard; personal use)"
KST = timezone(timedelta(hours=9))

CATEGORIES = ["토지/개발", "도로확장", "재건축/재개발", "상권개발", "시장동향"]
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL") or "gemini-3.1-flash-lite"
KEEP_DAYS = int(os.environ.get("KEEP_DAYS") or 7)
MAX_PER_RUN = 60  # 한 번에 Gemini에 보낼 최대 기사 수


def log(*a):
    print(*a, flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def rss_time(text):
    """RSS 날짜(RFC 822 또는 2026-11-06 16:45:00)를 한국 시간 'YYYY-MM-DD HH:MM'으로."""
    text = (text or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:16]
    try:
        return parsedate_to_datetime(text).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def read_rss(url):
    try:
        root = ET.fromstring(http_get(url).encode("utf-8"))
    except Exception as e:
        log(f"[경고] RSS 실패 {url}: {e}")
        return []
    out = []
    for it in root.iter("item"):
        out.append({
            "url": (it.findtext("link") or "").strip(),
            "title": clean(it.findtext("title")),
            "lead": clean(it.findtext("description"))[:400],
            "time": rss_time(it.findtext("pubDate")),
        })
    return out


def page_description(url):
    """기사 페이지의 설명문(meta description / og:description) 중 긴 것을 기사 앞부분으로 쓴다."""
    try:
        h = http_get(url)[:200000]
    except Exception:
        return ""
    found = []
    for tag in re.findall(r"<meta[^>]+>", h):
        if re.search(r'(name|property)="(og:)?description"', tag):
            m = re.search(r'content="([^"]*)"', tag)
            if m:
                found.append(clean(m.group(1)))
    return max(found, key=len)[:400] if found else ""


# ── 경기·인천: 중부일보 ─────────────────────────────────────
JB = "https://www.joongboo.com"
JB_KEYWORDS = re.compile(
    r"재개발|재건축|정비사업|리모델링|택지|신도시|개발|부지|용지|역세권|GTX|철도|전철|노선|"
    r"도로|확장|교통망|IC|분양|청약|집값|아파트|주택|부동산|상권|상가|토지|산업단지|산단|"
    r"도시공사|LH|주상복합|공급|규제지역|토지거래")


def jb_url(url):
    m = re.search(r"idxno=(\d+)", url)
    return f"{JB}/news/articleView.html?idxno={m.group(1)}" if m else url


def collect_gyeonggi():
    items = []
    for page in (1, 2):  # 건설·부동산 섹션 목록 (전부 후보)
        try:
            h = http_get(f"{JB}/news/articleList.html?sc_sub_section_code=S2N12&view_type=sm&page={page}")
        except Exception as e:
            log(f"[경고] 건설·부동산 목록 {page}쪽 실패: {e}")
            continue
        sec = h.split('id="section-list"', 1)
        if len(sec) < 2:
            log(f"[경고] {page}쪽에서 기사 목록을 찾지 못함 (사이트 구조 변경 가능)")
            continue
        for li in re.findall(r"<li>(.*?)</li>", sec[1].split("</section>", 1)[0], re.S):
            m = re.search(r'<h4 class="titles">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
            if not m:
                continue
            lead = re.search(r'<p class="lead[^"]*">(.*?)</p>', li, re.S)
            date = re.search(r"(\d{4})\.(\d{2})\.(\d{2}) (\d{2}:\d{2})", li)
            items.append({
                "url": jb_url(html.unescape(m.group(1))), "title": clean(m.group(2)),
                "lead": clean(lead.group(1))[:400] if lead else "",
                "time": "{}-{}-{} {}".format(*date.groups()) if date else "",
                "source": "중부일보",
            })
        time.sleep(1)
    for feed in ("allArticle", "S1N3", "S1N6", "S1N9"):  # 전체, 경제, 경기, 인천
        for a in read_rss(f"{JB}/rss/{feed}.xml"):
            if JB_KEYWORDS.search(a["title"]):
                items.append({**a, "url": jb_url(a["url"]), "source": "중부일보"})
        time.sleep(1)
    return items


# ── 서울: 한국경제·서울경제 부동산 ───────────────────────────
SEOUL_FEEDS = [
    ("한국경제", "https://www.hankyung.com/feed/realestate"),
    ("서울경제", "https://www.sedaily.com/rss/realestate"),
]


def collect_seoul():
    items = []
    for name, url in SEOUL_FEEDS:
        for a in read_rss(url):
            items.append({**a, "url": a["url"].split("?")[0], "source": name})
        time.sleep(1)
    return items


def fill_seoul_leads(todo):
    """RSS에 본문 앞부분이 없어서, 처리할 기사만 기사 페이지에서 설명문을 가져온다."""
    for a in todo:
        if not a["lead"]:
            a["lead"] = page_description(a["url"])
            time.sleep(0.5)


REGIONS = {
    "gyeonggi": {
        "name": "경기·인천",
        "default_source": "중부일보",
        "out": os.path.join(ROOT, "docs", "news.json"),
        "seen": os.path.join(ROOT, "data", "seen.json"),
        "collect": collect_gyeonggi,
        "prepare": None,
        "geo_prefix": ("경기도 ", ("인천", "경기")),
        "scope": "경기·인천 지역 신문(중부일보) 기사",
        "relevant": "경기·인천 지역의 부동산 투자자에게 의미 있는 기사",
        "region_rule": ('경기·인천의 시·군·구 이름으로 (예: "수원시", "성남시 분당구", "인천 남동구"). '
                        '경기·인천이 아니거나 특정할 수 없으면 "".'),
    },
    "seoul": {
        "name": "서울",
        "out": os.path.join(ROOT, "docs", "seoul.json"),
        "seen": os.path.join(ROOT, "data", "seen_seoul.json"),
        "collect": collect_seoul,
        "prepare": fill_seoul_leads,
        "geo_prefix": ("서울특별시 ", ("서울",)),
        "scope": "경제신문(한국경제·서울경제) 부동산 기사",
        "relevant": ("서울 부동산 투자자에게 의미 있는 기사. 서울 안의 지역·단지·사업을 다루거나, "
                     "서울 집값·전월세·분양·규제에 직접 영향을 주는 정책이면 true. "
                     "경기·인천·지방만 다루거나 서울과 관계가 약하면 false"),
        "region_rule": ('서울의 자치구 이름으로 (예: "강남구", "용산구", "노원구"). '
                        '서울 전체·여러 구에 걸치거나 특정할 수 없으면 "".'),
    },
}


# ── Gemini 판별·분류·요약 ───────────────────────────────────
PROMPT = """아래는 {scope} 목록이다. 기사마다 다음을 정하라.
- relevant: {relevant}.
  토지·개발사업, 교통 인프라(도로·철도·GTX) 신설·확장, 재건축·재개발, 상권·상업시설,
  집값·분양·청약·규제·공급 정책이 해당한다. 사고·파업·행사·인사·일반 복지·단순 협약·기업 실적·광고성 기사는 false.
- category: {cats} 중 하나.
  · 토지/개발: 택지·신도시·산업단지·용지 매각·개발계획
  · 도로확장: 도로·철도·GTX·역세권 등 교통 인프라
  · 재건축/재개발: 재건축·재개발·정비사업·노후계획도시·항만 재개발
  · 상권개발: 상가·상권·복합시설·업무지구
  · 시장동향: 집값·거래량·전월세·분양·청약·규제·정책 등 시장 흐름
- region: 기사의 핵심 지역. {region_rule} 여러 곳이면 가장 중심이 되는 한 곳.
- summary: 핵심 내용 3줄. 각 줄 30자 안팎의 개조식, 제목과 앞부분에 있는 사실만 쓴다. 줄 사이는 \\n.
  relevant가 false면 "".
- id는 입력 그대로 돌려준다.

기사 목록(JSON):
{articles}"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "relevant": {"type": "BOOLEAN"},
            "category": {"type": "STRING", "enum": CATEGORIES},
            "region": {"type": "STRING"},
            "summary": {"type": "STRING"},
        },
        "required": ["id", "relevant", "category", "region", "summary"],
    },
}


def ask_gemini(articles, key, cfg):
    payload = [{"id": i, "title": a["title"], "lead": a["lead"][:400]}
               for i, a in enumerate(articles)]
    text = PROMPT.format(scope=cfg["scope"], relevant=cfg["relevant"],
                         region_rule=cfg["region_rule"],
                         cats=" | ".join(f'"{c}"' for c in CATEGORIES),
                         articles=json.dumps(payload, ensure_ascii=False))
    body = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.2,
            "maxOutputTokens": 16384,
        },
    }
    data = json.dumps(body).encode("utf-8")
    # 기본 모델이 바쁘면(500/503) 한 번 더 기다렸다가, 그래도 안 되면 보조 모델로 바꾼다
    for model, waits in ((MODEL, (0, 40)), (FALLBACK_MODEL, (0,))):
        for wait in waits:
            time.sleep(wait)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    res = json.loads(r.read().decode("utf-8"))
                out = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
                log(f"요약 모델: {model}")
                return {row["id"]: row for row in json.loads(out)}
            except urllib.error.HTTPError as e:
                msg = json.loads(e.read().decode("utf-8", "replace") or "{}").get("error", {}).get("message", "")
                log(f"[Gemini 오류 {e.code} · {model}] {msg[:200]}")
                if e.code not in (500, 503):  # 429(하루 한도 초과)·키 오류 등은 바로 중단
                    return None
            except Exception as e:
                log(f"[Gemini 응답 처리 실패 · {model}] {e}")
                return None
    return None


# ── 지역 → 좌표 (OpenStreetMap Nominatim, 결과는 캐시) ──────
def geocode(region, cfg, cache):
    if not region:
        return None
    prefix, own = cfg["geo_prefix"]
    key = region if region.startswith(own) else prefix + region
    if key in cache:
        return cache[key]
    coords = None
    for q in dict.fromkeys([key, region]):
        url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1"
               "&countrycodes=kr&accept-language=ko&q=" + urllib.parse.quote(q))
        try:
            d = json.loads(http_get(url))
            if d:
                coords = [round(float(d[0]["lat"]), 5), round(float(d[0]["lon"]), 5)]
        except Exception as e:
            log(f"[좌표 실패] {q}: {e}")
        time.sleep(1.1)  # Nominatim 이용 정책: 초당 1건 이하
        if coords:
            break
    cache[key] = coords
    return coords


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def run_region(code, key, cache):
    cfg = REGIONS[code]
    log(f"\n=== {cfg['name']} ===")
    shown = {it["url"]: it for it in load_json(cfg["out"], {"items": []}).get("items", [])}
    for it in shown.values():  # 출처가 없는 예전 기사 보완
        it.setdefault("source", cfg.get("default_source", ""))
    seen = load_json(cfg["seen"], {})  # 처리한 기사 주소 → 처리 날짜 (관련 없다고 판정된 것 포함)
    if isinstance(seen, list):  # 예전 형식(주소 목록) 호환
        seen = {u: datetime.now(KST).strftime("%Y-%m-%d") for u in seen}

    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    cands = {}
    for a in cfg["collect"]():
        if a["url"] and a["url"] not in cands:
            cands[a["url"]] = a
    fresh = sorted((a for a in cands.values() if (a["time"] or "9999") >= cutoff),
                   key=lambda a: a["time"], reverse=True)
    todo = [a for a in fresh if a["url"] not in seen][:MAX_PER_RUN]
    log(f"후보 {len(cands)}건, 최근 {KEEP_DAYS}일 {len(fresh)}건 중 새로 처리할 기사 {len(todo)}건")

    if todo and not key:
        log("[알림] GEMINI_API_KEY가 없어 이번에는 새 기사를 처리하지 않습니다")
    elif todo:
        if cfg["prepare"]:
            cfg["prepare"](todo)
        result = ask_gemini(todo, key, cfg)
        if result is None:
            log("[알림] Gemini 처리 실패 — 다음 실행 때 다시 시도합니다")
        else:
            today = datetime.now(KST).strftime("%Y-%m-%d")
            kept = 0
            for i, a in enumerate(todo):
                r = result.get(i)
                if r is None:
                    continue  # 응답에 빠진 기사는 다음에 다시
                seen[a["url"]] = today
                if not r["relevant"]:
                    continue
                region = r["region"].strip()
                shown[a["url"]] = {
                    "url": a["url"], "title": a["title"], "time": a["time"],
                    "source": a.get("source", ""), "category": r["category"],
                    "region": region, "summary": r["summary"].strip(),
                    "latlng": geocode(region, cfg, cache),
                }
                kept += 1
            log(f"부동산 관련 기사 {kept}건 추가")

    items = [it for it in shown.values() if (it.get("time") or "9999") >= cutoff]
    items.sort(key=lambda it: it.get("time", ""), reverse=True)
    save_json(cfg["out"], {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "region": cfg["name"],
        "source": " · ".join(dict.fromkeys(it.get("source", "") for it in items if it.get("source"))),
        "items": items,
    })
    # 처리 기록은 보관 기간 + 7일만 남긴다
    keep_from = (datetime.now(KST) - timedelta(days=KEEP_DAYS + 7)).strftime("%Y-%m-%d")
    save_json(cfg["seen"], {u: d for u, d in seen.items() if d >= keep_from})
    log(f"저장 완료: 기사 {len(items)}건")


def main():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    only = os.environ.get("ONLY_REGION", "").strip()
    cache = load_json(GEO_PATH, {})
    failed = []
    for code in REGIONS:
        if only and code != only:
            continue
        try:
            run_region(code, key, cache)
        except Exception as e:  # 한 지역이 실패해도 다른 지역은 계속
            log(f"[실패] {REGIONS[code]['name']}: {e}")
            failed.append(code)
        save_json(GEO_PATH, cache)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
