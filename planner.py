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
    return {"dd": round(dd, 1), "above60": round(above, 1), "close": c, "tier": tier,
            "score": (100 if tier == 1 else 0) - dd + above}


def make(plan, state, kis):
    universe = plan.get("universe") or UNIVERSE
    # 기존 보유 종목과 현재 대기 중인 종목은 신규 후보에서 제외
    held_codes = set(kis.holdings().keys())
    busy = held_codes | {s["code"] for s in plan.get("stocks", [])}
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
                    "why": f"{'눌림목' if p['tier'] == 1 else '조정 초입'} · 60일 고점 대비 {p['dd']}%, 60일선 {p['above60']:+}%"} for p in picks],
    }


def main():
    plan = bot.load(bot.PLAN, {})
    state = bot.load(bot.STATE, {})
    mode = plan.get("mode", "demo")
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
    key = lambda p: [(s["code"], s["budget_krw"], s["max_buy_price"]) for s in p.get("stocks", [])]
    if key(old) == key(prop):
        bot.log("계획 다시 확인 · 기존 제안과 같음")
        if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            bot.notify("계획 변동 없음", "올라와 있는 계획이 지금도 유효합니다")
        return
    plan["proposal"] = prop
    bot.save(bot.PLAN, plan)
    names = ", ".join(s["name"] for s in prop["stocks"])
    bot.log(f"새 매수 계획 제안: {names}")
    bot.notify("새 매수 계획 — 컨펌해 주세요", f"{names} · 앱에서 확인")


if __name__ == "__main__":
    main()
