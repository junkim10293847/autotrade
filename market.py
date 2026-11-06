"""
오늘 시황 — 코스피·코스닥 (존박)

봇이 10분마다 돌 때 함께 실행해 market.json 에 저장한다. 앱 첫 화면 '오늘 시황'에서 본다.
- 지수: 현재값, 전일 대비, 시가·고가·저가, 상승/하락 종목 수, 거래대금
- 수급: 외국인·기관·개인 순매수 (조회되는 경우)
- 장중 흐름: 실행 때마다 찍은 지수 (그날 것만)
- 주요 뉴스: 시장 관련 제목 몇 개 (실전 키가 있을 때)
- 한 줄 요약: 숫자를 보고 규칙으로 만든 문장 (예측이 아니라 현재 상태 설명)
매매에는 영향을 주지 않는다. 실패해도 봇에는 영향 없음.
"""
import json
import math
import os
import time

import bot

FILE = "market.json"
INDEX = {"kospi": ("0001", "코스피"), "kosdaq": ("1001", "코스닥")}
# 시장별 투자자 매매동향 조회 코드 (응답이 비면 다음 후보를 시도)
FLOW_CODES = {"kospi": [("0001", "S001"), ("999", "S001")], "kosdaq": [("1001", "T000"), ("999", "T001")]}
NEWS_WORDS = ("코스피", "코스닥", "증시", "지수", "외국인", "환율", "금리", "나스닥", "뉴욕", "반도체", "서킷", "사이드카", "연준", "유가")


def num(x, k):
    try:
        return float(str(x.get(k, "") or 0).replace(",", ""))
    except ValueError:
        return 0.0


def index(kis, iscd):
    r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/inquire-index-price", timeout=15,
                     headers=kis._h("FHPUP02100000"),
                     params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd})
    j = r.json()
    o = j.get("output") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    now = num(o, "bstp_nmix_prpr")
    if not now:
        raise RuntimeError(f"HTTP {r.status_code} {j.get('rt_cd')} {j.get('msg_cd')} {j.get('msg1')}")
    chg = num(o, "bstp_nmix_prdy_vrss")
    if str(o.get("prdy_vrss_sign")) in ("4", "5") and chg > 0:
        chg = -chg
    prev = now - chg
    low = num(o, "bstp_nmix_lwpr")
    high = num(o, "bstp_nmix_hgpr")
    return {"now": round(now, 2), "chg": round(chg, 2), "rate": round(chg / prev * 100, 2) if prev else 0,
            "open": num(o, "bstp_nmix_oprc"), "high": high, "low": low,
            "low_rate": round((low / prev - 1) * 100, 2) if prev and low else None,
            "high_rate": round((high / prev - 1) * 100, 2) if prev and high else None,
            "up": int(num(o, "ascn_issu_cnt")), "down": int(num(o, "down_issu_cnt")), "flat": int(num(o, "stnr_issu_cnt")),
            "limit_up": int(num(o, "uplm_issu_cnt")), "limit_down": int(num(o, "lslm_issu_cnt")),
            "amount_eok": round(num(o, "acml_tr_pbmn") / 100),  # 백만원 → 억원
            "prev_amount_eok": round(num(o, "prdy_tr_pbmn") / 100),
            "year_high": num(o, "dryy_bstp_nmix_hgpr"), "from_year_high": num(o, "dryy_hgpr_vrss_prpr_rate")}


def flows(kis, key):
    for a, b in FLOW_CODES[key]:
        try:
            r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market", timeout=15,
                             headers=kis._h("FHPTJ04030000"), params={"FID_INPUT_ISCD": a, "FID_INPUT_ISCD_2": b})
            o = r.json().get("output") or {}
            if isinstance(o, list):
                o = o[0] if o else {}
            if any(o.get(k) not in (None, "") for k in ("frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "prsn_ntby_tr_pbmn")):
                # 백만원 → 억원
                return {"foreign": round(num(o, "frgn_ntby_tr_pbmn") / 100), "inst": round(num(o, "orgn_ntby_tr_pbmn") / 100),
                        "person": round(num(o, "prsn_ntby_tr_pbmn") / 100)}
        except Exception as e:
            print(key, a, b, "수급 조회 실패", e)
    return None


def headlines(kis, n=6):
    try:
        r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/news-title", timeout=15,
                         headers=kis._h("FHKST01011800"),
                         params={"FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_CLS_CODE": "", "FID_INPUT_ISCD": "",
                                 "FID_TITL_CNTT": "", "FID_INPUT_DATE_1": "", "FID_INPUT_HOUR_1": "",
                                 "FID_RANK_SORT_CLS_CODE": "", "FID_INPUT_SRNO": ""})
        j = r.json()
        rows = j.get("output") or j.get("output1") or []
    except Exception as e:
        print("뉴스 조회 실패", e)
        return None
    out, seen = [], set()
    for x in rows:
        t = (x.get("hts_pbnt_titl_cntt") or "").strip()
        if not t or t in seen or not any(w in t for w in NEWS_WORDS):
            continue
        seen.add(t)
        tm = str(x.get("data_tm", ""))
        out.append({"time": f"{tm[:2]}:{tm[2:4]}" if len(tm) >= 4 else "", "title": t, "src": x.get("dorg", "")})
        if len(out) >= n:
            break
    return out


def quote(kis, code):
    """종목 오늘 시세: 현재가, 전일 대비, 시가·고가·저가, 52주 고저, 거래량"""
    r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/inquire-price", timeout=15,
                     headers=kis._h("FHKST01010100"), params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    o = r.json().get("output") or {}
    now = num(o, "stck_prpr")
    if not now:
        return None
    chg = num(o, "prdy_vrss")
    if str(o.get("prdy_vrss_sign")) in ("4", "5") and chg > 0:
        chg = -chg
    prev = now - chg
    pct = lambda v: round((v / prev - 1) * 100, 2) if prev and v else None
    hi, lo = num(o, "stck_hgpr"), num(o, "stck_lwpr")
    return {"now": now, "chg": chg, "rate": pct(now), "prev": prev, "open": num(o, "stck_oprc"),
            "high": hi, "low": lo, "high_rate": pct(hi), "low_rate": pct(lo),
            "w52_high": num(o, "w52_hgpr"), "w52_low": num(o, "w52_lwpr"), "vol": int(num(o, "acml_vol")),
            "vol_ratio": num(o, "prdy_vrss_vol_rate")}  # 전일 대비 거래량 (%)


def strong(kis, idx_rate, n=5):
    """우량주 목록 중 코스피보다 강한 종목: 지수 대비 초과 등락률이 크고 거래량이 늘어난 순
    점수 = (종목 등락률 - 코스피 등락률) + 2 × log2(거래량 배수)  · 보기용, 자동매매와 무관"""
    import planner
    pause = 0.06 if kis.mode == "real" else 0.5
    rows = []
    for code, name in planner.UNIVERSE.items():
        try:
            q = quote(kis, code)
        except Exception:
            q = None
        time.sleep(pause)
        if not q or q["rate"] is None:
            continue
        rel = q["rate"] - idx_rate
        if rel <= 0:
            continue
        vr = q["vol_ratio"] or 100
        score = rel + 2 * math.log2(max(vr, 25) / 100)
        rows.append({"code": code, "name": name, "now": q["now"], "rate": q["rate"], "rel": round(rel, 2),
                     "vol_ratio": round(vr), "low_rate": q["low_rate"], "high_rate": q["high_rate"],
                     "score": round(score, 2), "hot": q["rate"] >= 10})
    rows.sort(key=lambda r: -r["score"])
    return {"checked": len(planner.UNIVERSE), "beat": len(rows), "top": rows[:n]}


def eok(v):
    s = f"{abs(v):,.0f}억" if abs(v) < 10000 else f"{abs(v) / 10000:,.1f}조"
    return ("+" if v > 0 else "-" if v < 0 else "") + s


def summary(m):
    """숫자로 오늘 시장 상태를 2~4문장으로 설명 (예측 아님)"""
    k, q = m.get("kospi"), m.get("kosdaq")
    if not k:
        return []
    r = k["rate"]
    mood = ("폭락장" if r <= -5 else "급락장" if r <= -3 else "약세" if r <= -1 else "보합권" if r < 1
            else "강세" if r < 3 else "급등장")
    lines = [f"{mood} · 코스피 {r:+.2f}%" + (f", 코스닥 {q['rate']:+.2f}%" if q else "")]
    lo = k.get("low_rate")
    if lo is not None and lo - r <= -2:
        lines.append(f"장중 저점 {lo:+.1f}%에서 {r - lo:.1f}%p 회복한 상태")
    hi = k.get("high_rate")
    if hi is not None and hi > 0 and hi - r >= 2:
        lines.append(f"장중 고점 {hi:+.1f}%에서 {hi - r:.1f}%p 밀린 상태")
    tot = k["up"] + k["down"] + k["flat"]
    if tot:
        lines.append(f"코스피 {tot}종목 중 상승 {k['up']} · 하락 {k['down']}"
                     + (f" (하한가 {k['limit_down']})" if k["limit_down"] else "") + (f" (상한가 {k['limit_up']})" if k["limit_up"] else ""))
    f = (m.get("flows") or {}).get("kospi")
    if f:
        big = max((("외국인", f["foreign"]), ("기관", f["inst"]), ("개인", f["person"])), key=lambda x: abs(x[1]))
        lines.append(f"수급: 외국인 {eok(f['foreign'])} · 기관 {eok(f['inst'])} · 개인 {eok(f['person'])} → {big[0]} {'매도' if big[1] < 0 else '매수'} 주도")
    if k.get("prev_amount_eok") and k["amount_eok"] > k["prev_amount_eok"] * 1.5 and bot.now().hour >= 11:
        lines.append(f"거래대금 {eok(k['amount_eok']).lstrip('+')}로 전일보다 크게 늘어남")
    return lines


def warn(msg):
    """시황 실패는 하루 3번까지만 봇 기록에 남긴다 (원인 확인용)"""
    print(msg)
    st = bot.load(bot.STATE, {})
    key = "market_warn_" + bot.now().date().isoformat()
    if st.get(key, 0) < 3:
        st[key] = st.get(key, 0) + 1
        bot.save(bot.STATE, st)
        bot.log(msg[:300])


def main():
    plan = bot.load(bot.PLAN, {})
    if bot.now().strftime("%H%M") < "0900":
        print("장 시작 전 · 시황 갱신 안 함")
        return
    old = bot.load(FILE, {})
    today = bot.now().date().isoformat()
    errs, kis, m = [], None, None
    # 시세는 실전 서버가 정확해서 실전 키 먼저, 안 되면 모의 서버로 (주문 없음)
    for mode in dict.fromkeys(["real", plan.get("mode", "demo"), "demo"]):
        try:
            k = bot.KIS(mode)
            cur = {"updated": bot.now().isoformat(timespec="minutes"), "date": today, "src": mode}
            for key, (iscd, _) in INDEX.items():
                try:
                    cur[key] = index(k, iscd)
                except Exception as e:
                    errs.append(f"{mode} {key}: {type(e).__name__}: {e}")
                    cur[key] = None
            if cur.get("kospi"):
                kis, m = k, cur
                break
        except Exception as e:
            errs.append(f"{mode} 연결: {type(e).__name__}: {e}")
    if not m:
        warn("시황 지수 조회 실패 · " + " / ".join(errs))
        return
    m["flows"] = {k: flows(kis, k) for k in INDEX}
    # 보유·계획 종목 오늘 시세
    status = bot.load(bot.STATUS, {})
    names = {p["code"]: p.get("name", p["code"]) for p in status.get("positions", []) if p.get("code")}
    held = list(names)
    for s_ in plan.get("stocks", []):
        names.setdefault(s_["code"], s_.get("name", s_["code"]))
    m["quotes"] = {}
    for code in list(names)[:12]:
        try:
            q = quote(kis, code)
            if q:
                m["quotes"][code] = {"name": names[code], "held": code in held, **q}
        except Exception as e:
            print(code, "시세 조회 실패", e)
    m["news"] = headlines(kis) or (old.get("news") if old.get("date") == today else [])
    # 장중 흐름: 오늘 찍은 값만 이어 붙임
    pts = old.get("intraday", []) if old.get("date") == today else []
    t = bot.now().strftime("%H:%M")
    if not pts or pts[-1]["t"] != t:
        pts.append({"t": t, "kospi": m["kospi"]["rate"], "kosdaq": (m.get("kosdaq") or {}).get("rate")})
    m["intraday"] = pts[-60:]
    # 지난 날 마감 기록 (최근 20거래일)
    days = [d for d in old.get("days", []) if d["date"] != today]
    days.append({"date": today, "kospi": m["kospi"]["now"], "kospi_rate": m["kospi"]["rate"],
                 "kosdaq": (m.get("kosdaq") or {}).get("now"), "kosdaq_rate": (m.get("kosdaq") or {}).get("rate")})
    m["days"] = days[-20:]
    try:
        m["strong"] = strong(kis, m["kospi"]["rate"])
    except Exception as e:
        print("강한 종목 조회 실패", e)
        m["strong"] = old.get("strong") if old.get("date") == today else None
    m["summary"] = summary(m)
    bot.save(FILE, m)
    print("시황 저장:", " / ".join(m["summary"]))


if __name__ == "__main__":
    main()
