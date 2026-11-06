"""중부일보의 경기·인천 부동산 관련 기사를 모아 Gemini로 분류·요약하고 docs/news.json에 저장한다.

수집 범위
  1) 건설·부동산 섹션 기사 목록 (전부 후보)
  2) 전체기사·경제·경기·인천 RSS 중 제목에 부동산 관련 낱말이 있는 기사
  → Gemini가 부동산과 관련 있는 기사만 남기고 분류·요약한다.

- 외부 패키지 없이 파이썬 표준 라이브러리만 쓴다.
- 이미 처리한 기사는 다시 보내지 않는다 (무료 한도 절약: 실행 1회당 Gemini 요청 1회 이하).
- 환경 변수
    GEMINI_API_KEY : Google AI Studio에서 받은 키 (없으면 요약 없이 저장)
    GEMINI_MODEL   : 사용할 모델 (기본 gemini-3.5-flash)
    KEEP_DAYS      : 며칠 치 기사를 보여 줄지 (기본 7)
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(ROOT, "docs", "news.json")
GEO_PATH = os.path.join(ROOT, "data", "geocache.json")
SEEN_PATH = os.path.join(ROOT, "data", "seen.json")

BASE = "https://www.joongboo.com"
LIST_URL = BASE + "/news/articleList.html?sc_sub_section_code=S2N12&view_type=sm&page={page}"
RSS_FEEDS = ["allArticle", "S1N3", "S1N6", "S1N9"]  # 전체, 경제, 경기, 인천
KEYWORDS = re.compile(
    r"재개발|재건축|정비사업|리모델링|택지|신도시|개발|부지|용지|역세권|GTX|철도|전철|노선|"
    r"도로|확장|교통망|IC|분양|청약|집값|아파트|주택|부동산|상권|상가|토지|산업단지|산단|"
    r"도시공사|LH|주상복합|공급|규제지역|토지거래")
UA = "Mozilla/5.0 (realestate-news-dashboard; personal use)"
KST = timezone(timedelta(hours=9))

CATEGORIES = ["토지/개발", "도로확장", "재건축/재개발", "상권개발", "시장동향"]
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
KEEP_DAYS = int(os.environ.get("KEEP_DAYS") or 7)
MAX_PER_RUN = 40  # 한 번에 Gemini에 보낼 최대 기사 수


def log(*a):
    print(*a, flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_url(url):
    """기사 주소를 idxno 기준으로 통일한다 (RSS와 목록 페이지 주소 형식이 달라도 같은 기사로 본다)."""
    m = re.search(r"idxno=(\d+)", url)
    return f"{BASE}/news/articleView.html?idxno={m.group(1)}" if m else url


# ── 1. 기사 후보 수집 ───────────────────────────────────────
def fetch_section_list(pages=2):
    items = []
    for page in range(1, pages + 1):
        try:
            h = http_get(LIST_URL.format(page=page))
        except Exception as e:
            log(f"[경고] 건설·부동산 목록 {page}쪽 실패: {e}")
            continue
        sec = h.split('id="section-list"', 1)
        if len(sec) < 2:
            log(f"[경고] {page}쪽에서 기사 목록을 찾지 못함 (사이트 구조 변경 가능)")
            continue
        body = sec[1].split("</section>", 1)[0]
        for li in re.findall(r"<li>(.*?)</li>", body, re.S):
            m = re.search(r'<h4 class="titles">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
            if not m:
                continue
            lead = re.search(r'<p class="lead[^"]*">(.*?)</p>', li, re.S)
            date = re.search(r"(\d{4})\.(\d{2})\.(\d{2}) (\d{2}:\d{2})", li)
            items.append({
                "url": norm_url(urllib.parse.urljoin(BASE, html.unescape(m.group(1)))),
                "title": clean(m.group(2)),
                "lead": clean(lead.group(1)) if lead else "",
                "time": "{}-{}-{} {}".format(*date.groups()) if date else "",
            })
        time.sleep(1)
    return items


def fetch_rss():
    items = []
    for feed in RSS_FEEDS:
        try:
            root = ET.fromstring(http_get(f"{BASE}/rss/{feed}.xml").encode("utf-8"))
        except Exception as e:
            log(f"[경고] RSS {feed} 실패: {e}")
            continue
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            if not KEYWORDS.search(title):
                continue
            items.append({
                "url": norm_url(it.findtext("link") or ""),
                "title": title,
                "lead": clean(it.findtext("description"))[:400],
                "time": (it.findtext("pubDate") or "")[:16],  # 2026-11-06 16:45
            })
        time.sleep(1)
    return items


def collect_candidates():
    merged = {}
    for a in fetch_section_list() + fetch_rss():
        if a["url"] and a["url"] not in merged:
            merged[a["url"]] = a
    log(f"후보 기사 {len(merged)}건 (건설·부동산 목록 + RSS 키워드)")
    return list(merged.values())


# ── 2. Gemini 판별·분류·요약 ────────────────────────────────
PROMPT = """아래는 경기·인천 지역 신문(중부일보) 기사 목록이다. 기사마다 다음을 정하라.
- relevant: 부동산 투자자에게 의미 있는 기사면 true. 토지·개발사업, 교통 인프라(도로·철도·GTX) 신설·확장,
  재건축·재개발, 상권·상업시설, 집값·분양·청약·규제·공급 정책이 해당한다.
  사고·파업·행사·인사·일반 복지·단순 협약처럼 부동산과 관계가 약하면 false.
- category: {cats} 중 하나.
  · 토지/개발: 택지·신도시·산업단지·용지 매각·개발계획
  · 도로확장: 도로·철도·GTX·역세권 등 교통 인프라
  · 재건축/재개발: 재건축·재개발·정비사업·노후계획도시·항만 재개발
  · 상권개발: 상가·상권·복합시설·업무지구
  · 시장동향: 집값·거래량·분양·청약·규제·정책 등 시장 흐름
- region: 기사의 핵심 지역. 경기·인천의 시·군·구 이름으로 (예: "수원시", "성남시 분당구", "인천 남동구").
  여러 곳이면 가장 중심이 되는 한 곳. 경기·인천이 아니거나 특정할 수 없으면 "".
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


def ask_gemini(articles, key):
    payload = [{"id": i, "title": a["title"], "lead": a["lead"][:400]}
               for i, a in enumerate(articles)]
    body = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT.format(
            cats=" | ".join(f'"{c}"' for c in CATEGORIES),
            articles=json.dumps(payload, ensure_ascii=False))}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.2,
            "maxOutputTokens": 16384,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.loads(r.read().decode("utf-8"))
            text = "".join(p.get("text", "") for p in
                           res["candidates"][0]["content"]["parts"])
            return {row["id"]: row for row in json.loads(text)}
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")[:300]
            log(f"[Gemini 오류 {e.code}] {msg}")
            if e.code in (500, 503) and attempt < 2:  # 서버 일시 오류만 다시 시도 (429=한도 초과는 재시도 안 함)
                time.sleep(30 * (attempt + 1))
                continue
            return None
        except Exception as e:
            log(f"[Gemini 응답 처리 실패] {e}")
            return None
    return None


# ── 3. 지역 → 좌표 (OpenStreetMap Nominatim, 결과는 캐시) ──
def geocode(region, cache):
    if not region:
        return None
    if region in cache:
        return cache[region]
    queries = [region] if region.startswith(("인천", "경기")) else ["경기도 " + region, region]
    coords = None
    for q in queries:
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
    cache[region] = coords
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


def main():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    old = load_json(NEWS_PATH, {"items": []})
    shown = {it["url"]: it for it in old.get("items", [])}
    seen = set(load_json(SEEN_PATH, []))  # 이미 처리한 기사 (관련 없다고 판정된 것 포함)
    cache = load_json(GEO_PATH, {})

    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    fresh = [a for a in collect_candidates() if (a["time"] or "9999") >= cutoff]
    todo = [a for a in fresh if a["url"] not in seen][:MAX_PER_RUN]
    log(f"최근 {KEEP_DAYS}일 후보 {len(fresh)}건 중 새로 처리할 기사 {len(todo)}건")

    if todo and not key:
        log("[알림] GEMINI_API_KEY가 없어 이번에는 새 기사를 처리하지 않습니다")
    elif todo:
        result = ask_gemini(todo, key)
        if result is None:
            log("[알림] Gemini 처리 실패 — 다음 실행 때 다시 시도합니다")
        else:
            kept = 0
            for i, a in enumerate(todo):
                r = result.get(i)
                if r is None:
                    continue  # 응답에 빠진 기사는 다음에 다시
                seen.add(a["url"])
                if not r["relevant"]:
                    continue
                shown[a["url"]] = {
                    "url": a["url"], "title": a["title"], "time": a["time"],
                    "category": r["category"], "region": r["region"].strip(),
                    "summary": r["summary"].strip(),
                    "latlng": geocode(r["region"].strip(), cache),
                }
                kept += 1
            log(f"부동산 관련 기사 {kept}건 추가")

    items = [it for it in shown.values() if (it.get("time") or "9999") >= cutoff]
    items.sort(key=lambda it: it.get("time", ""), reverse=True)

    save_json(NEWS_PATH, {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "source": "중부일보",
        "items": items,
    })
    # 처리 기록은 최근 1000건만 남긴다 (기사 번호 idxno가 클수록 최근 기사)
    def idx(u):
        m = re.search(r"idxno=(\d+)", u)
        return int(m.group(1)) if m else 0
    save_json(SEEN_PATH, sorted(seen, key=idx, reverse=True)[:1000])
    save_json(GEO_PATH, cache)
    log(f"저장 완료: 기사 {len(items)}건")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[실패] {e}")
        sys.exit(1)
