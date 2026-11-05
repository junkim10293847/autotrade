"""
주간 매수 계획 자동 작성 — 우량주 눌림목 (존박)

매주 일요일 저녁 실행. 우량주 목록에서 아래 조건을 모두 통과한 종목 중 점수 상위 3개를
plan.json 의 proposal 로 올리고 휴대폰으로 알림을 보낸다. 실제 매수는 앱에서 컨펌해야 시작된다.

조건 (최근 100거래일 기준) — 아래 score() 설명 참고
- 장기 상승 추세: 종가가 60일 평균 위
- 눌림: 60일 최고가 대비 -8% ~ -25%
- 단기 조정 중: 종가가 20일 평균 아래
- 급락 중 아님: 최근 5일 하락이 -12% 이내
점수 = 눌림 폭 + 60일선 위 여유 (깊게 빠졌지만 추세는 살아 있는 종목 우선)
"""
import json
import os
import sys
import time
from datetime import timedelta

import requests

import bot

# 코스피 대형 우량주 약 100개 (plan.json 의 "universe" 로 바꿀 수 있음)
UNIVERSE = {
    "005930": "삼성전자", "000660": "SK하이닉스", "005380": "현대차", "000270": "기아",
    "012330": "현대모비스", "035420": "NAVER", "051910": "LG화학", "006400": "삼성SDI",
    "207940": "삼성바이오로직스", "068270": "셀트리온", "105560": "KB금융", "055550": "신한지주",
    "086790": "하나금융지주", "316140": "우리금융지주", "138040": "메리츠금융지주", "032830": "삼성생명",
    "000810": "삼성화재", "028260": "삼성물산", "066570": "LG전자", "003550": "LG",
    "017670": "SK텔레콤", "030200": "KT", "033780": "KT&G", "018260": "삼성에스디에스",
    "012450": "한화에어로스페이스", "009540": "HD한국조선해양", "034020": "두산에너빌리티",
    "010130": "고려아연", "005490": "POSCO홀딩스", "096770": "SK이노베이션",
    "373220": "LG에너지솔루션", "035720": "카카오", "323410": "카카오뱅크", "259960": "크래프톤",
    "036570": "엔씨소프트", "352820": "하이브", "009150": "삼성전기", "011070": "LG이노텍",
    "034730": "SK", "003670": "포스코퓨처엠", "086280": "현대글로비스", "000720": "현대건설",
    "028050": "삼성E&A", "010140": "삼성중공업", "042660": "한화오션", "329180": "HD현대중공업",
    "267260": "HD현대일렉트릭", "064350": "현대로템", "047810": "한국항공우주", "079550": "LIG넥스원",
    "024110": "기업은행", "006800": "미래에셋증권", "071050": "한국금융지주", "015760": "한국전력",
    "090430": "아모레퍼시픽", "097950": "CJ제일제당", "271560": "오리온", "021240": "코웨이",
    "161390": "한국타이어앤테크놀로지", "128940": "한미약품", "000100": "유한양행", "010950": "S-Oil",
    # v7: 코스피 시가총액 상위 약 100위까지
    "003490": "대한항공", "078930": "GS", "004020": "현대제철", "011780": "금호석유",
    "001040": "CJ", "005830": "DB손해보험", "000150": "두산", "241560": "두산밥캣",
    "010620": "HD현대미포", "267250": "HD현대", "042700": "한미반도체", "000880": "한화",
    "009830": "한화솔루션", "272210": "한화시스템", "029780": "삼성카드", "016360": "삼성증권",
    "039490": "키움증권", "005940": "NH투자증권", "088350": "한화생명", "001450": "현대해상",
    "282330": "BGF리테일", "139480": "이마트", "023530": "롯데쇼핑", "004990": "롯데지주",
    "011170": "롯데케미칼", "051900": "LG생활건강", "032640": "LG유플러스", "034220": "LG디스플레이",
    "006260": "LS", "010120": "LS ELECTRIC", "047050": "포스코인터내셔널", "002380": "KCC",
    "004370": "농심", "007310": "오뚜기", "036460": "한국가스공사", "180640": "한진칼",
    "012750": "에스원", "302440": "SK바이오사이언스", "326030": "SK바이오팜", "011790": "SKC",
    "402340": "SK스퀘어", "035250": "강원랜드", "008770": "호텔신라", "069960": "현대백화점",
}
PICKS = 3


def daily(kis, code):
    end = bot.now().date()
    start = end - timedelta(days=160)
    r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", timeout=20,
                     headers=kis._h("FHKST03010100"),
                     params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                             "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    rows = [x for x in r.json().get("output2", []) or [] if x.get("stck_clpr")]
    return [float(x["stck_clpr"]) for x in rows]  # 최신이 앞


def score(closes):
    """1순위(strict): 60일선 위 + 고점 대비 -8~-25% + 20일선 아래
       2순위(relaxed): 60일선 -5% 이내 + 고점 대비 -5~-25% — 1순위가 3개 안 되면 채움"""
    if len(closes) < 60:
        return None
    c = closes[0]
    ma20 = sum(closes[:20]) / 20
    ma60 = sum(closes[:60]) / 60
    dd = (c / max(closes[:60]) - 1) * 100
    wk = (c / closes[5] - 1) * 100
    above = (c / ma60 - 1) * 100
    if wk <= -12 or not (-25 <= dd <= -5):
        return None
    if c > ma60 and dd <= -8 and c < ma20:
        tier = 1
    elif above >= -5:
        tier = 2
    else:
        return None
    high = max(closes[:60])
    vs20 = (c / ma20 - 1) * 100
    return {"dd": round(dd, 1), "above60": round(above, 1), "close": c, "tier": tier,
            "score": (100 if tier == 1 else 0) - dd + above,
            "detail": {"close": round(c), "high60": round(high), "ma20": round(ma20), "ma60": round(ma60),
                       "dd": round(dd, 1), "vs20": round(vs20, 1), "vs60": round(above, 1), "wk5": round(wk, 1)}}


def reasons(p):
    """앱에서 종목을 눌렀을 때 보여줄 추천 사유 (쉬운 말로)"""
    d = p["detail"]
    won = lambda v: f"{v:,}원"
    r = []
    if p["tier"] == 1:
        r.append("눌림목: 큰 흐름은 오르는 중인데 최근 일시적으로 빠진 상태라, 반등을 노리기 좋은 자리로 판단했습니다.")
    else:
        r.append("조정 초입: 최근 고점에서 내려오기 시작했지만 아직 큰 흐름이 꺾이지 않은 상태입니다. 눌림목 후보가 부족해 채운 종목이라 확신도는 한 단계 낮습니다.")
    r.append(f"최근 60거래일 최고가 {won(d['high60'])} → 현재 {won(d['close'])}, 고점 대비 {d['dd']}% 내려와 있습니다.")
    r.append(f"60일 평균가 {won(d['ma60'])} 대비 {d['vs60']:+}% — " + ("평균 위에 있어 중기 상승 흐름이 유지되고 있습니다." if d["vs60"] > 0 else "평균 근처라 추세가 아직 무너지지 않았습니다."))
    r.append(f"20일 평균가 {won(d['ma20'])} 대비 {d['vs20']:+}% — " + ("단기적으로는 쉬어가는 중이라 싸게 살 기회입니다." if d["vs20"] < 0 else "단기 평균 위로 다시 올라서는 중입니다."))
    r.append(f"최근 5거래일 {d['wk5']:+}% — 급락(-12% 이상)이 아니어서 악재로 무너지는 중은 아닌 것으로 봤습니다.")
    r.append("주의: 주가 흐름만 본 규칙 추천이라 실적 발표·뉴스는 반영되지 않았습니다.")
    return r


def news(kis, code, n=5):
    """종목 최근 뉴스 제목 (한국투자 뉴스 API · 실전 서버만 지원)"""
    try:
        r = kis.get_read(f"{kis.base}/uapi/domestic-stock/v1/quotations/news-title", timeout=15,
                         headers=kis._h("FHKST01011800"),
                         params={"FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_CLS_CODE": "", "FID_INPUT_ISCD": code,
                                 "FID_TITL_CNTT": "", "FID_INPUT_DATE_1": "", "FID_INPUT_HOUR_1": "",
                                 "FID_RANK_SORT_CLS_CODE": "", "FID_INPUT_SRNO": ""})
        j = r.json()
        rows = j.get("output") or j.get("output1") or []
        out, seen = [], set()
        for x in rows:
            t = (x.get("hts_pbnt_titl_cntt") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            d = x.get("data_dt", "")
            out.append({"date": f"{d[4:6]}/{d[6:8]}" if len(d) == 8 else "", "title": t, "src": x.get("dorg", "")})
            if len(out) >= n:
                break
        return out
    except Exception as e:
        print(code, "뉴스 조회 실패", e)
        return None


def make(plan, state, kis):
    universe = plan.get("universe") or UNIVERSE
    # 기존 보유 종목도 조건을 충족하면 추가 매수 후보로 제안한다.
    # 이미 확정 대기 목록에 들어 있는 종목만 중복 제안에서 제외한다.
    busy = {s["code"] for s in plan.get("stocks", [])}
    # 최근 7일 안에 미체결로 정리한 종목은 다시 제안하지 않는다
    week_ago = (bot.now().date() - timedelta(days=7)).isoformat()
    busy |= {c for c, d in (state.get("dropped") or {}).items() if str(d.get("date", "")) >= week_ago}
    cands = []
    for code, name in universe.items():
        if code in busy:
            continue
        try:
            s = score(daily(kis, code))
        except Exception as e:
            print(name, "조회 실패", e)
            continue
        if s:
            cands.append({"code": code, "name": name, **s})
        time.sleep(0.12)  # 조회 속도 제한
    cands.sort(key=lambda x: -x["score"])
    picks = cands[:PICKS]
    if not picks:
        return None

    # 뉴스는 모의 서버가 지원하지 않아 실전 키로 조회 (조회만, 주문 없음)
    try:
        nk = bot.KIS("real", plan.get("accounts"))
    except Exception:
        nk = kis
    for p in picks:
        p["news"] = news(nk, p["code"])
        time.sleep(0.12)

    cap = float(plan.get("max_total_krw", 2000000))
    # 제안 예산은 전체 한도의 1/3로 제시한다. 실제 주문 시 봇이 계좌 보유원가와
    # 당일 미체결 예약액을 다시 계산해 남은 한도 안에서만 매수한다.
    each = int((cap / PICKS) // 10000 * 10000)
    if each < 50000:
        return None
    return {
        "created": bot.now().isoformat(timespec="minutes"),
        "note": f"우량주 {len(universe)}개 중 최근 눌린 {len(picks)}종목 (규칙 자동 선정)",
        "stocks": [{"code": p["code"], "name": p["name"], "budget_krw": each,
                    "max_buy_price": bot.round_up_tick(p["close"] * 1.01),
                    "why": f"{'눌림목' if p['tier'] == 1 else '조정 초입'} · 60일 고점 대비 {p['dd']}%, 60일선 {p['above60']:+}%",
                    "reasons": reasons(p), "news": p.get("news")} for p in picks],
    }


def main():
    plan = bot.load(bot.PLAN, {})
    state = bot.load(bot.STATE, {})
    mode = plan.get("mode", "demo")
    auto = "--auto" in sys.argv
    if auto:
        # 매매 실행 직후 단계: 매도 체결로 한도가 생겼을 때만 새 계획을 만든다
        need = state.get("need_plan")
        if not need or plan.get("paused"):
            return
        state.pop("need_plan", None)
        bot.save(bot.STATE, state)
        status = bot.load(bot.STATUS, {})
        cap = float(plan.get("max_total_krw", 2000000))
        room = float((status.get("total") or {}).get("remaining", cap))
        sold = ", ".join(dict.fromkeys(need.get("sold", [])))
        if room < cap / PICKS * 0.5:
            bot.log(f"{sold} 정리 후 남은 투자 가능액 {bot.won(room)} · 한도가 부족해 새 계획은 만들지 않음")
            return
        bot.log(f"{sold} 정리로 투자 가능액 {bot.won(room)} 생김 → 새 매수 계획 자동 작성")
    try:
        # 종목 중복 확인은 실제 주문이 이루어지는 계좌와 동일한 계좌 기준으로 한다.
        # 모의투자 중에는 모의 계좌, 실전 중에는 실전 계좌의 보유 종목을 조회한다.
        kis = bot.KIS(mode, plan.get("accounts"))
        prop = make(plan, state, kis)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        bot.log(f"계획 작성 실패: {detail}")
        bot.write_api_health(False, f"매수 계획 생성 실패 · {detail}", mode)
        bot.notify("계획 작성 실패", detail[:350] + " · 앱의 최근 기록 확인")
        return
    if not prop:
        bot.log("이번 주는 조건에 맞는 종목이 없어 계획을 올리지 않음")
        bot.notify("이번 주 매수 계획 없음", "조건에 맞는 종목이 없어 쉬어갑니다")
        return
    old = plan.get("proposal") or {}
    key = lambda p: [(s["code"], s["budget_krw"], s["max_buy_price"], bool(s.get("reasons"))) for s in p.get("stocks", [])]
    if key(old) == key(prop):
        bot.log("계획 다시 확인 · 기존 제안과 같음")
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            bot.notify("계획 변동 없음", "올라와 있는 계획이 지금도 유효합니다")
        return
    plan["proposal"] = prop
    bot.save(bot.PLAN, plan)
    names = ", ".join(s["name"] for s in prop["stocks"])
    bot.log(f"새 매수 계획 제안: {names}")
    if auto:
        bot.notify("매도 완료 · 새 매수 계획이 올라왔습니다", f"{names} · 앱의 매수 예정·검토에서 컨펌해 주세요")
    else:
        bot.notify("새 매수 계획 — 컨펌해 주세요", f"{names} · 앱에서 확인")


if __name__ == "__main__":
    main()
