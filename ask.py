"""
오늘 시황 질문 답변 (존박)

앱의 '오늘 시황 → 물어보기'에서 질문하면 ask.yml 이 이 파일을 실행한다.
1) 질문에서 종목 이름을 찾는다 (코스피·코스닥 전 종목 목록, 실패하면 우량주 목록)
2) 그 종목의 오늘 시세(등락률·거래량)와 최근 뉴스 제목을 모은다 + 오늘 시장 상황
3) ANTHROPIC_API_KEY 가 있으면 Claude 가 위 자료와 웹 검색으로 이유를 3~5문장으로 정리
   없으면 수치와 뉴스 제목을 정리해서 보여준다
결과는 answers.json 에 쌓인다 (최근 20개). 매매에는 영향 없음.
사용법: python ask.py "<질문>" "<질문 id>"
"""
import io
import json
import os
import re
import sys
import zipfile

import requests

import bot
import market

FILE = "answers.json"
MASTER = {"kospi": ("https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip", 228),
          "kosdaq": ("https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip", 222)}
PARTICLES = ("관련주", "관련", "종목", "주가", "들이", "들은", "들", "주", "가", "이", "는", "은", "의", "를", "을", "도", "만")


def all_names():
    """코스피·코스닥 전 종목 {코드: 이름}. 실패하면 우량주 목록"""
    import planner
    names = {}
    for mkt, (url, tail) in MASTER.items():
        try:
            z = zipfile.ZipFile(io.BytesIO(requests.get(url, timeout=20).content))
            text = z.read(z.namelist()[0]).decode("cp949", errors="ignore")
            for line in text.splitlines():
                head = line[:len(line) - tail]
                code, name = head[0:9].strip(), head[21:].strip()
                if len(code) == 6 and code.isdigit() and name:
                    names[code] = name
        except Exception as e:
            print(mkt, "종목 목록 받기 실패", e)
    return names or dict(planner.UNIVERSE)


def norm(s):
    return re.sub(r"[\s·\-_.()]", "", s).upper()


def tokens(q):
    out = []
    for t in re.split(r"[\s,.?!·]+", q):
        t = norm(t)
        for _ in range(2):  # '바이오주가' → '바이오주' → '바이오'
            for p in PARTICLES:
                if t.endswith(p.upper()) and len(t) > len(p) + 1:
                    t = t[:-len(p)]
                    break
        if len(t) >= 2:
            out.append(t)
    return out


def find_stocks(q, names, limit=3):
    import planner
    qn, toks = norm(q), tokens(q)
    # 1) 이름이 질문에 그대로 있음 (2글자 이름은 단어로 따로 있을 때만: 'SK바이오'의 'SK' 제외)
    exact = [(c, n) for c, n in names.items()
             if (len(norm(n)) >= 3 and norm(n) in qn) or norm(n) in toks]
    if exact:
        exact.sort(key=lambda x: -len(x[1]))
        keep = []
        for c, n in exact:
            if not any(norm(n) in norm(k) for _, k in keep):
                keep.append((c, n))
        return keep[:limit]
    # 2) 이름이 질문 단어로 시작 ('SK바이오' → SK바이오사이언스, SK바이오팜)
    hits = list(dict.fromkeys((c, n) for t in toks for c, n in names.items() if norm(n).startswith(t)))
    hits.sort(key=lambda x: (x[0] not in planner.UNIVERSE, x[1].endswith("우") or x[1][-2:] in ("우B", "2우"), len(x[1])))
    pref = lambda n: n.endswith("우") or n[-2:] in ("우B", "2우", "3우")
    if any(not pref(n) for _, n in hits) and "우선" not in q:
        hits = [h for h in hits if not pref(h[1])]  # 우선주는 묻지 않으면 빼기
    return hits[:limit]


def stock_news(kis, code, n=8):
    try:
        r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/news-title", timeout=15,
                         headers=kis._h("FHKST01011800"),
                         params={"FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_CLS_CODE": "", "FID_INPUT_ISCD": code,
                                 "FID_TITL_CNTT": "", "FID_INPUT_DATE_1": "", "FID_INPUT_HOUR_1": "",
                                 "FID_RANK_SORT_CLS_CODE": "", "FID_INPUT_SRNO": ""})
        rows = r.json().get("output") or []
    except Exception as e:
        print(code, "뉴스 조회 실패", e)
        return []
    out, seen = [], set()
    for x in rows:
        t = "".join(ch if ch.isprintable() else " " for ch in (x.get("hts_pbnt_titl_cntt") or "")).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        d, tm = str(x.get("data_dt", "")), str(x.get("data_tm", ""))
        out.append({"date": f"{d[4:6]}/{d[6:8]}" if len(d) == 8 else "", "time": f"{tm[:2]}:{tm[2:4]}" if len(tm) >= 4 else "",
                    "title": t, "src": x.get("dorg", "")})
        if len(out) >= n:
            break
    return out


def facts_text(stocks, mk):
    lines = []
    if mk.get("kospi"):
        lines.append(f"[시장] {bot.now():%m/%d %H:%M} 코스피 {mk['kospi']['rate']:+.2f}%"
                     + (f", 코스닥 {mk['kosdaq']['rate']:+.2f}%" if mk.get("kosdaq") else "")
                     + (" · " + " / ".join(mk.get("summary", [])[1:3]) if mk.get("summary") else ""))
    for s in stocks:
        q = s.get("quote") or {}
        if q:
            lines.append(f"[{s['name']}({s['code']})] 현재가 {q['now']:,.0f}원, 전일 대비 {q['rate']:+.2f}%, "
                         f"시가 {q['open']:,.0f} 고가 {q['high']:,.0f} 저가 {q['low']:,.0f}, "
                         f"거래량 전일 대비 {q.get('vol_ratio') or 0:.0f}%")
        for n in s.get("news", []):
            lines.append(f"  - {n['date']} {n['time']} {n['title']} ({n['src']})")
    if not stocks:
        for n in mk.get("news", [])[:6]:
            lines.append(f"  - {n['time']} {n['title']} ({n['src']})")
    return "\n".join(lines)


def ask_claude(question, facts):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, []
    model = os.environ.get("ASK_MODEL") or "claude-sonnet-5-5"
    system = ("너는 한국 주식 시황을 설명하는 도우미다. 사용자 이름은 존박. 아래 [자료]의 수치와 뉴스 제목, 필요하면 웹 검색으로 "
              "질문에 한국어로 3~5문장으로 답한다. 확인된 사실과 추정을 구분하고, 이유를 모르면 모른다고 말한다. "
              "매수·매도 권유는 하지 않는다. 마크다운 머리글 없이 짧은 문단으로.")
    body = {"model": model, "max_tokens": 900, "system": system,
            "messages": [{"role": "user", "content": f"질문: {question}\n\n[자료]\n{facts}"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
                       "user_location": {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"}}]}
    hdr = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    r = requests.post("https://api.anthropic.com/v1/messages", headers=hdr, json=body, timeout=120)
    if r.status_code == 400:  # 웹 검색 도구를 못 쓰면 자료만으로
        body.pop("tools")
        r = requests.post("https://api.anthropic.com/v1/messages", headers=hdr, json=body, timeout=120)
    r.raise_for_status()
    text, srcs = [], []
    for b in r.json().get("content", []):
        if b.get("type") == "text":
            text.append(b.get("text", ""))
            for c in b.get("citations") or []:
                if c.get("url") and c["url"] not in [s["url"] for s in srcs]:
                    srcs.append({"url": c["url"], "title": c.get("title") or c["url"]})
    return "".join(text).strip(), srcs[:5]


def plain_answer(stocks, mk):
    if not stocks:
        s = mk.get("summary") or []
        out = (" · ".join(s[:3]) + ". ") if s else ""
        return out + "질문에서 종목 이름을 찾지 못해 오늘 시장 뉴스를 함께 보여드립니다."
    parts = []
    for s in stocks:
        q = s.get("quote")
        if q:
            k = (mk.get("kospi") or {}).get("rate")
            rel = f", 코스피({k:+.2f}%)보다 {q['rate'] - k:+.1f}%p" if k is not None else ""
            parts.append(f"{s['name']}은(는) 지금 {q['now']:,.0f}원으로 전일 대비 {q['rate']:+.2f}%{rel}, "
                         f"거래량은 전일의 {q.get('vol_ratio') or 0:.0f}%입니다.")
        else:
            parts.append(f"{s['name']} 시세를 가져오지 못했습니다.")
        if s.get("news"):
            parts.append("최근 뉴스 제목에서 이유를 확인해 보세요.")
    return " ".join(parts)


def main():
    question = (sys.argv[1] if len(sys.argv) > 1 else "").strip()[:300]
    qid = sys.argv[2] if len(sys.argv) > 2 else bot.now().strftime("%Y%m%d%H%M%S")
    if not question:
        return
    plan = bot.load(bot.PLAN, {})
    kis = None
    for mode in dict.fromkeys(["real", plan.get("mode", "demo")]):
        try:
            kis = bot.KIS(mode)
            break
        except Exception as e:
            print(mode, "연결 실패", e)
    mk = bot.load(market.FILE, {})
    stocks = []
    for code, name in find_stocks(question, all_names()):
        s = {"code": code, "name": name}
        if kis:
            try:
                s["quote"] = market.quote(kis, code)
            except Exception as e:
                print(name, "시세 실패", e)
            s["news"] = stock_news(kis, code)
        stocks.append(s)
    facts = facts_text(stocks, mk)
    answer, srcs, how = None, [], "자료 정리"
    try:
        answer, srcs = ask_claude(question, facts)
        if answer:
            how = "Claude 요약"
    except Exception as e:
        print("Claude 요약 실패", e)
        how = "자료 정리 (AI 요약 실패)"
    if not answer:
        answer = plain_answer(stocks, mk)
    item = {"id": qid, "q": question, "at": bot.now().isoformat(timespec="minutes"), "how": how, "answer": answer,
            "stocks": [{"code": s["code"], "name": s["name"],
                        "rate": (s.get("quote") or {}).get("rate"), "now": (s.get("quote") or {}).get("now"),
                        "vol_ratio": (s.get("quote") or {}).get("vol_ratio"), "news": s.get("news", [])[:6]} for s in stocks],
            "market_news": [] if stocks else (mk.get("news") or [])[:5], "sources": srcs}
    data = bot.load(FILE, {"items": []})
    data["items"] = [item] + [x for x in data.get("items", []) if x.get("id") != qid][:19]
    bot.save(FILE, data)
    print("답변 저장:", answer[:120])


if __name__ == "__main__":
    main()
