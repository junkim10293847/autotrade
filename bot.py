"""
국내주식 자동매매 봇 (한국투자증권 Open API) — 존박

규칙 (단순하게)
1. plan.json 에 존박님이 '컨펌'한 종목만 매매한다. 그 외 보유 종목은 절대 건드리지 않는다.
2. 매수: 현재가가 '최대매수가' 이하이면 종목별 예산만큼 지정가로 한 번만 산다.
3. 매도(시장가, 전량): 익절 +10% 이상 / 손절 -7% 이하 / 산 지 14일 지나면 정리.
4. "paused": true 이면 아무것도 하지 않는다. (비상 정지)
5. 모든 행동은 log.md, 주문·성적은 state.json, 휴대폰 화면용 요약은 status.json 에 남긴다.
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
PLAN, STATE, STATUS, LOG = (os.path.join(ROOT, f) for f in ("plan.json", "state.json", "status.json", "log.md"))

URLS = {
    "demo": "https://openapivts.koreainvestment.com:29443",
    "real": "https://openapi.koreainvestment.com:9443",
}
KST = ZoneInfo("Asia/Seoul")


def now():
    return datetime.now(KST)


def log(msg):
    line = f"- {now():%Y-%m-%d %H:%M} KST · {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, body):
    """휴대폰 알림 (ntfy 앱에서 notify.txt 의 주제를 구독)"""
    try:
        topic = open(os.path.join(ROOT, "notify.txt"), encoding="utf-8").read().strip()
        if topic:
            requests.post("https://ntfy.sh/", timeout=10,
                          json={"topic": topic, "title": title, "message": body, "tags": ["chart_with_upwards_trend"]})
    except Exception as e:
        print("알림 실패", e)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def market_open(t=None):
    t = t or now()
    if t.weekday() >= 5:
        return False
    m = t.hour * 60 + t.minute
    return 9 * 60 + 5 <= m <= 15 * 60 + 15  # 장 시작 5분 후 ~ 마감 15분 전


def tick(price):
    """국내 주식 호가 단위"""
    for limit, unit in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < limit:
            return unit
    return 1000


def round_up_tick(price):
    u = tick(price)
    return int(math.ceil(price / u) * u)


class KIS:
    def __init__(self, mode, accounts=None):
        self.mode = mode
        self.base = URLS[mode]
        sfx = mode.upper()
        self.key = os.environ[f"KIS_APP_KEY_{sfx}"]
        self.secret = os.environ[f"KIS_APP_SECRET_{sfx}"]
        acct = (os.environ.get(f"KIS_ACCOUNT_{sfx}") or (accounts or {}).get(mode, "")).replace("-", "").strip()
        if len(acct) < 8:
            raise ValueError(f"{mode} 계좌번호가 없습니다 (Secrets: KIS_ACCOUNT_DEMO / KIS_ACCOUNT_REAL)")
        self.acct, self.prod = acct[:8], (acct[8:10] or "01")
        self.token = self._token()

    def _token(self):
        """토큰은 하루 유효 — 캐시해 두고 20시간마다만 새로 받는다 (발급 횟수 제한·알림 방지)"""
        cache = os.path.join(ROOT, f".kis_token_{self.mode}.json")
        c = load(cache, {})
        if c.get("key") == self.key[:6] and now().timestamp() - c.get("at", 0) < 20 * 3600:
            return c["token"]
        r = requests.post(f"{self.base}/oauth2/tokenP", timeout=15,
                          json={"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        r.raise_for_status()
        tok = r.json()["access_token"]
        save(cache, {"key": self.key[:6], "at": now().timestamp(), "token": tok})
        return tok

    def _h(self, tr_id):
        return {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token}",
                "appkey": self.key, "appsecret": self.secret, "tr_id": tr_id, "custtype": "P"}

    def tr(self, real_id):
        return ("V" + real_id[1:]) if self.mode == "demo" else real_id

    def price(self, code):
        r = requests.get(f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-price", timeout=15,
                         headers=self._h("FHKST01010100"),
                         params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        r.raise_for_status()
        p = r.json().get("output", {}).get("stck_prpr")
        return int(p) if p else None

    def holdings(self):
        r = requests.get(f"{self.base}/uapi/domestic-stock/v1/trading/inquire-balance", timeout=15,
                         headers=self._h(self.tr("TTTC8434R")),
                         params={"CANO": self.acct, "ACNT_PRDT_CD": self.prod, "AFHR_FLPR_YN": "N", "OFL_YN": "",
                                 "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                                 "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                                 "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        r.raise_for_status()
        out = {}
        for row in r.json().get("output1", []) or []:
            qty = int(float(row.get("hldg_qty") or 0))
            if qty > 0:
                out[row["pdno"]] = {"name": row.get("prdt_name", ""), "qty": qty,
                                    "sellable": int(float(row.get("ord_psbl_qty") or 0)),
                                    "avg": float(row.get("pchs_avg_pric") or 0), "now": float(row.get("prpr") or 0),
                                    "rate": float(row.get("evlu_pfls_rt") or 0)}
        return out

    def order(self, side, code, qty, price=None):
        """price=None 이면 시장가"""
        tr_id = self.tr("TTTC0012U" if side == "buy" else "TTTC0011U")
        body = {"CANO": self.acct, "ACNT_PRDT_CD": self.prod, "PDNO": code,
                "ORD_DVSN": "01" if price is None else "00", "ORD_QTY": str(qty),
                "ORD_UNPR": "0" if price is None else str(price), "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""}
        data = requests.post(f"{self.base}/uapi/domestic-stock/v1/trading/order-cash", timeout=15,
                             headers=self._h(tr_id), json=body).json()
        return data.get("rt_cd") == "0", data.get("msg1", "")


def won(v):
    return f"{int(round(v)):,}원"


def run(plan, state, kis):
    tp = float(plan.get("take_profit_pct", 10))
    sl = float(plan.get("stop_loss_pct", -7))
    hold_days = int(plan.get("hold_days", 14))
    cap = float(plan.get("max_total_krw", 0))
    buys, sells = state.setdefault("buys", {}), state.setdefault("sells", {})
    trades = state.setdefault("trades", [])
    held = kis.holdings()
    spent = sum(v.get("krw", 0) for k, v in buys.items() if k not in sells)

    for item in plan.get("stocks", []):
        code, name = item["code"], item.get("name", item["code"])
        h = held.get(code)

        # ---- 매도: 익절 / 손절 / 기간 만료
        if h and code in buys:
            days = (now().date() - datetime.fromisoformat(buys[code]["date"]).date()).days
            reason = None
            if h["rate"] >= float(item.get("take_profit_pct", tp)):
                reason = f"익절 {h['rate']:+.1f}%"
            elif h["rate"] <= float(item.get("stop_loss_pct", sl)):
                reason = f"손절 {h['rate']:+.1f}%"
            elif days >= hold_days:
                reason = f"{hold_days}일 경과 정리 {h['rate']:+.1f}%"
            if reason and h["sellable"] > 0:
                ok, msg = kis.order("sell", code, h["sellable"])
                log(f"**매도** {name} {h['sellable']}주 시장가 ({reason}) → {'접수' if ok else '실패: ' + msg}")
                notify(f"매도 {name}", f"{h['sellable']}주 · {reason}" + ("" if ok else " · 실패: " + msg))
                if ok:
                    pnl = (h["now"] - h["avg"]) * h["sellable"]
                    sells[code] = {"date": now().isoformat(timespec="minutes"), "reason": reason}
                    trades.append({"code": code, "name": name, "date": now().isoformat(timespec="minutes"),
                                   "rate": h["rate"], "pnl": round(pnl), "days": days})
            continue

        # ---- 어제 낸 매수 주문이 체결 안 됐으면 다시 대기 상태로
        if code in buys and code not in sells and not h \
                and datetime.fromisoformat(buys[code]["date"]).date() < now().date():
            log(f"{name} 매수 주문 미체결 → 다시 대기")
            del buys[code]

        # ---- 매수: 한 번도 주문 안 한 종목만
        if code in buys or code in sells or h:
            continue
        price = kis.price(code)
        if not price:
            log(f"{name} 시세 조회 실패, 다음 회차에 재시도")
            continue
        if price > float(item["max_buy_price"]):
            continue
        px = round_up_tick(price * 1.003)  # 체결 잘 되도록 현재가보다 살짝 위
        budget = float(item["budget_krw"])
        qty = int(budget // px)
        if qty < 1:
            log(f"{name} 예산 {won(budget)}으로 1주도 못 삼 (현재가 {won(price)})")
            continue
        if cap and spent + qty * px > cap:
            log(f"{name} 총 투자한도 {won(cap)} 초과라 매수 보류")
            continue
        ok, msg = kis.order("buy", code, qty, px)
        log(f"**매수** {name} {qty}주 @ {won(px)} (예산 {won(budget)}) → {'접수' if ok else '실패: ' + msg}")
        notify(f"매수 {name}", f"{qty}주 @ {won(px)}" + ("" if ok else " · 실패: " + msg))
        if ok:
            buys[code] = {"date": now().isoformat(timespec="minutes"), "qty": qty, "krw": qty * px}
            spent += qty * px

    write_status(plan, state, kis.holdings())


def write_status(plan, state, held):
    """휴대폰 화면용 요약"""
    mine = {s["code"] for s in plan.get("stocks", [])}
    buys = state.get("buys", {})
    pos, cost, value = [], 0.0, 0.0
    for code, h in held.items():
        if code not in mine:
            continue
        d = buys.get(code, {}).get("date")
        days = (now().date() - datetime.fromisoformat(d).date()).days if d else None
        pos.append({"code": code, "name": h["name"], "qty": h["qty"], "avg": h["avg"], "now": h["now"],
                    "rate": h["rate"], "days": days})
        cost += h["avg"] * h["qty"]
        value += h["now"] * h["qty"]
    trades = state.get("trades", [])
    year = str(now().year)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    save(STATUS, {
        "updated": now().isoformat(timespec="minutes"),
        "mode": plan.get("mode", "demo"),
        "positions": pos,
        "total": {"cost": round(cost), "value": round(value), "pnl": round(value - cost),
                  "rate": round((value / cost - 1) * 100, 2) if cost else 0},
        "record": {"trades": len(trades), "wins": wins,
                   "win_rate": round(wins / len(trades) * 100, 1) if trades else None,
                   "realized_year": sum(t["pnl"] for t in trades if t["date"].startswith(year))},
    })


def main():
    plan = load(PLAN, {})
    if plan.get("paused"):
        print("일시정지 상태 — 종료")
        return
    state = load(STATE, {})
    mode = plan.get("mode", "demo")
    if not market_open() and "--force" not in sys.argv:
        if "--check" in sys.argv:  # 장 밖에서 '지금 실행' → 연결만 확인, 주문 없음
            try:
                write_status(plan, state, KIS(mode, plan.get("accounts")).holdings())
                log(f"연결 확인 완료 ({'실전' if mode == 'real' else '모의'}) · 장이 닫혀 주문은 하지 않음")
            except Exception as e:
                log(f"연결 실패 ({'실전' if mode == 'real' else '모의'}): {type(e).__name__}: {e}")
            return
        print("국내 정규장 시간이 아님 — 종료")
        return
    try:
        run(plan, state, KIS(mode, plan.get("accounts")))
    except Exception as e:  # 오류가 나도 기록만 남기고 다음 회차에 재시도
        log(f"오류: {type(e).__name__}: {e}")
    save(STATE, state)


if __name__ == "__main__":
    main()
