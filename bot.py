"""
국내주식 자동매매 봇 (한국투자증권 Open API) — 존박

규칙 (단순하게)
1. 매도 감시는 계좌의 모든 보유 종목에 적용한다. 신규 매수는 plan.json 에 컨펌된 종목만 한다.
2. 매수: 현재가가 '최대매수가' 이하이면 종목별 예산만큼 지정가로 한 번만 산다.
3. 매도(시장가, 전량): 익절 +10% 이상 / 손절 -7% 이하 / 산 지 14일 지나면 정리.
4. "paused": true 이면 아무것도 하지 않는다. (비상 정지)
5. 모든 행동은 log.md, 주문·성적은 state.json, 휴대폰 화면용 요약은 status.json 에 남긴다.
"""
import json
import math
import os
import sys
import time
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
    """ntfy 주제 URL로 휴대폰 알림 전송"""
    try:
        topic = open(os.path.join(ROOT, "notify.txt"), encoding="utf-8").read().strip()
        if topic:
            requests.post(f"https://ntfy.sh/{topic}", data=str(body).encode("utf-8"), timeout=10,
                          headers={"Title": str(title), "Tags": "chart_with_upwards_trend"})
    except Exception as e:
        print("알림 실패", e)


def api_error_detail(response):
    """HTTP 오류 응답에 포함된 KIS 메시지를 로그에 남긴다."""
    try:
        data = response.json()
        detail = data.get("msg1") or data.get("message") or data.get("error_description") or str(data)
    except Exception:
        detail = (response.text or "").strip()[:500]
    return f"HTTP {response.status_code}: {detail or response.reason}"


def write_api_health(ok, message, mode=None):
    """API 연결 결과를 status.json에 남기되 기존 손익/주문 데이터는 보존한다."""
    status = load(STATUS, {})
    status["api"] = {"ok": bool(ok), "message": str(message)[:600],
                     "checked": now().isoformat(timespec="seconds"), "mode": mode}
    save(STATUS, status)


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

    def get_read(self, url, *, timeout=20, **kwargs):
        """잔고·시세 등 조회 GET만 재시도. 주문 POST는 절대 자동 재전송하지 않는다."""
        last = None
        for attempt in range(4):
            try:
                r = requests.get(url, timeout=timeout, **kwargs)
                if r.status_code == 429 or r.status_code >= 500:
                    last = RuntimeError(api_error_detail(r))
                    if attempt < 3:
                        time.sleep((2, 4, 8)[attempt])
                        continue
                    raise last
                if not r.ok:
                    raise RuntimeError(api_error_detail(r))
                return r
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                if attempt < 3:
                    time.sleep((2, 4, 8)[attempt])
                    continue
                raise RuntimeError(f"조회 재시도 4회 실패: {type(e).__name__}: {e}") from e
        raise RuntimeError(f"조회 재시도 실패: {last}")

    def price(self, code):
        r = self.get_read(f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-price", timeout=15,
                          headers=self._h("FHKST01010100"),
                          params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        p = r.json().get("output", {}).get("stck_prpr")
        return int(p) if p else None

    def holdings(self):
        r = self.get_read(f"{self.base}/uapi/domestic-stock/v1/trading/inquire-balance", timeout=20,
                          headers=self._h(self.tr("TTTC8434R")),
                          params={"CANO": self.acct, "ACNT_PRDT_CD": self.prod, "AFHR_FLPR_YN": "N", "OFL_YN": "",
                                  "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                                  "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                                  "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        payload = r.json()
        if payload.get("rt_cd") != "0":
            raise RuntimeError(f"잔고 조회 실패(KIS {payload.get('msg_cd', '')}): {payload.get('msg1', '응답 코드 오류')}")
        summary_rows = payload.get("output2", []) or []
        summary = summary_rows[0] if summary_rows else {}
        self.account_summary = {
            "cash": float(summary.get("ord_psbl_cash") or summary.get("dnca_tot_amt") or 0),
            "deposit": float(summary.get("dnca_tot_amt") or 0),
            "account_value": float(summary.get("tot_evlu_amt") or 0),
        }
        out = {}
        for row in payload.get("output1", []) or []:
            qty = int(float(row.get("hldg_qty") or 0))
            if qty > 0:
                out[row["pdno"]] = {"name": row.get("prdt_name", ""), "qty": qty,
                                    "sellable": int(float(row.get("ord_psbl_qty") or 0)),
                                    "avg": float(row.get("pchs_avg_pric") or 0), "now": float(row.get("prpr") or 0),
                                    "rate": float(row.get("evlu_pfls_rt") or 0)}
        return out

    def order(self, side, code, qty, price=None):
        """주문 접수 결과와 주문번호를 반환한다. 접수는 체결을 의미하지 않는다."""
        tr_id = self.tr("TTTC0012U" if side == "buy" else "TTTC0011U")
        body = {"CANO": self.acct, "ACNT_PRDT_CD": self.prod, "PDNO": code,
                "ORD_DVSN": "01" if price is None else "00", "ORD_QTY": str(qty),
                "ORD_UNPR": "0" if price is None else str(price), "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""}
        r = requests.post(f"{self.base}/uapi/domestic-stock/v1/trading/order-cash", timeout=15,
                          headers=self._h(tr_id), json=body)
        r.raise_for_status()
        data = r.json()
        out = data.get("output") or {}
        return {"ok": data.get("rt_cd") == "0", "msg": data.get("msg1", ""),
                "order_no": str(out.get("ODNO") or out.get("odno") or ""),
                "branch": str(out.get("KRX_FWDG_ORD_ORGNO") or out.get("ord_gno_brno") or ""),
                "raw": data}

    def daily_fills(self, start_date=None, end_date=None):
        """당일 포함 3개월 이내 주문/체결 조회. output1은 주문별 누적 체결정보."""
        today = now().date()
        start_date = start_date or today.strftime("%Y%m%d")
        end_date = end_date or today.strftime("%Y%m%d")
        tr_id = self.tr("TTTC0081R")
        params = {"CANO": self.acct, "ACNT_PRDT_CD": self.prod,
                  "INQR_STRT_DT": start_date, "INQR_END_DT": end_date,
                  "SLL_BUY_DVSN_CD": "00", "CCLD_DVSN": "00", "INQR_DVSN": "00",
                  "INQR_DVSN_3": "00", "PDNO": "", "ORD_GNO_BRNO": "", "ODNO": "",
                  "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
                  "EXCG_ID_DVSN_CD": "KRX"}
        rows, fk, nk, cont = [], "", "", ""
        for _ in range(10):
            q = dict(params, CTX_AREA_FK100=fk, CTX_AREA_NK100=nk)
            r = self.get_read(f"{self.base}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                              timeout=20, headers=self._h(tr_id), params=q)
            data = r.json()
            if data.get("rt_cd") != "0":
                raise RuntimeError(f"체결 조회 실패: {data.get('msg1', 'KIS API 오류')}")
            rows.extend(data.get("output1") or [])
            fk = (data.get("ctx_area_fk100") or "").strip()
            nk = (data.get("ctx_area_nk100") or "").strip()
            cont = r.headers.get("tr_cont", "")
            if cont not in ("M", "F") or not (fk or nk):
                break
        return rows


def _num(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                pass
    return 0.0


def track_order(state, result, side, code, name, qty, price, reason=None, unit_cost=0, days=None, rate=None):
    if not result.get("ok"):
        return False
    order_no = result.get("order_no")
    orders = state.setdefault("orders", {})
    if not order_no:
        # 주문번호 누락은 자동 재주문을 막고 수동 확인이 필요한 상태로 보존한다.
        key = f"{now():%Y%m%d%H%M%S}:{side}:{code}"
        orders[key] = {"order_no": "", "branch": "", "side": side, "code": code, "name": name,
                       "ordered_qty": int(qty), "limit_price": float(price or 0), "filled_qty": 0,
                       "filled_amount": 0, "remaining_qty": int(qty), "created": now().isoformat(timespec="minutes"),
                       "reason": reason, "unit_cost": float(unit_cost or 0), "days": days, "order_rate": rate,
                       "status": "unmatched", "last_seen": None}
        log(f"{name} 주문번호 누락 · 중복 주문 방지를 위해 확인 대기 상태로 저장")
        return True
    key = f"{now():%Y%m%d}:{order_no}"
    orders[key] = {"order_no": order_no, "branch": result.get("branch", ""), "side": side,
                   "code": code, "name": name, "ordered_qty": int(qty), "limit_price": float(price or 0),
                   "filled_qty": 0, "filled_amount": 0, "remaining_qty": int(qty),
                   "created": now().isoformat(timespec="minutes"), "reason": reason,
                   "unit_cost": float(unit_cost or 0), "days": days, "order_rate": rate,
                   "status": "accepted", "last_seen": None}
    log(f"{name} 주문번호 {order_no} 저장 · 체결 확인 대기")
    return True


def reconcile_fills(kis, state):
    """주문별 누적 체결량의 증가분만 반영하여 중복 집계를 방지한다."""
    orders = state.setdefault("orders", {})
    if not orders:
        return
    created_dates = [str(o.get("created", ""))[:10].replace("-", "") for o in orders.values() if o.get("created")]
    start_date = min(created_dates) if created_dates else now().strftime("%Y%m%d")
    rows = kis.daily_fills(start_date=start_date)
    by_no = {}
    for row in rows:
        no = str(row.get("odno") or row.get("ODNO") or "")
        if no:
            by_no[no] = row
    trades = state.setdefault("trades", [])
    for key, order in orders.items():
        row = by_no.get(str(order.get("order_no", "")))
        if not row:
            continue
        total_filled = int(_num(row, "tot_ccld_qty", "ccld_qty", "TOT_CCLD_QTY"))
        total_amount = _num(row, "tot_ccld_amt", "TOT_CCLD_AMT")
        avg_price = _num(row, "avg_prvs", "ccld_unpr", "AVG_PRVS")
        if total_filled <= 0:
            continue
        if avg_price <= 0 and total_amount > 0:
            avg_price = total_amount / total_filled
        if avg_price <= 0:
            continue
        # 응답에 누적 체결금액이 없을 때는 누적 평균가로 재구성
        if total_amount <= 0:
            total_amount = avg_price * total_filled
        old_qty = int(order.get("filled_qty", 0))
        old_amount = float(order.get("filled_amount", 0))
        if total_filled < old_qty:
            # 증권사 응답이 역행한 경우 기존 기록을 덮지 않는다.
            continue
        delta_qty = total_filled - old_qty
        delta_amount = total_amount - old_amount
        if delta_qty > 0:
            delta_price = delta_amount / delta_qty if delta_amount > 0 else avg_price
            side = order.get("side")
            if side == "sell":
                pnl = (delta_price - float(order.get("unit_cost", 0))) * delta_qty
                trades.append({"code": order.get("code"), "name": order.get("name"),
                               "date": now().isoformat(timespec="minutes"), "side": "sell",
                               "qty": delta_qty, "price": round(delta_price, 4),
                               "pnl": round(pnl), "days": order.get("days"),
                               "reason": order.get("reason"), "estimated": False,
                               "order_no": order.get("order_no")})
                log(f"실제 매도 체결 {order.get('name')} {delta_qty}주 @ {won(delta_price)} · 실현손익 {won(pnl)}")
                notify(f"매도 체결 {order.get('name')}", f"{delta_qty}주 · 실현손익 {won(pnl)}")
            else:
                buys = state.setdefault("buys", {})
                meta = buys.setdefault(order.get("code"), {"date": order.get("created"), "qty": 0, "krw": 0, "pending": True})
                meta["qty"] = int(meta.get("qty", 0)) + delta_qty
                meta["krw"] = float(meta.get("krw", 0)) + delta_amount
                meta["pending"] = int(row.get("rmn_qty") or 0) > 0
                log(f"실제 매수 체결 {order.get('name')} {delta_qty}주 @ {won(delta_price)}")
                notify(f"매수 체결 {order.get('name')}", f"{delta_qty}주 @ {won(delta_price)}")
            order["filled_qty"] = total_filled
            order["filled_amount"] = total_amount
        remaining = int(_num(row, "rmn_qty", "RMN_QTY"))
        order["remaining_qty"] = remaining
        order["status"] = "filled" if remaining == 0 and total_filled >= int(order.get("ordered_qty", 0)) else ("partial" if total_filled else "accepted")
        order["last_seen"] = now().isoformat(timespec="minutes")


def won(v):
    return f"{int(round(v)):,}원"


def run(plan, state, kis):
    tp = float(plan.get("take_profit_pct", 10))
    sl = float(plan.get("stop_loss_pct", -7))
    hold_days = int(plan.get("hold_days", 14))
    cap = float(plan.get("max_total_krw", 2000000))
    buys = state.setdefault("buys", {})
    trades = state.setdefault("trades", [])
    held = kis.holdings()
    try:
        reconcile_fills(kis, state)
    except Exception as e:
        log(f"체결내역 조회 지연 · 기존 주문 추적 유지: {type(e).__name__}: {e}")

    # 교체 선택 시 기존 보유분 매도가 끝날 때까지 신규 매수는 보류
    replacement = plan.get("replacement") or {}
    replace_codes = set(replacement.get("sell_codes", [])) if replacement.get("pending") else set()
    replacement_waiting = False
    for code in list(replace_codes):
        h = held.get(code)
        if not h:
            continue
        replacement_waiting = True
        pending = state.setdefault("replacement_sell_orders", {})
        last = pending.get(code, {})
        active_order = any(o.get("code") == code and o.get("side") == "sell" and o.get("status") in ("accepted", "partial", "unmatched") for o in state.get("orders", {}).values())
        if last.get("date") == now().date().isoformat() or active_order:
            continue
        if h["sellable"] > 0:
            result = kis.order("sell", code, h["sellable"])
            ok, msg = result["ok"], result["msg"]
            log(f"**교체 매도** {h['name']} {h['sellable']}주 시장가 → {'접수' if ok else '실패: ' + msg}")
            if ok:
                pending[code] = {"date": now().date().isoformat(), "qty": h["sellable"], "order_no": result.get("order_no")}
                track_order(state, result, "sell", code, h["name"], h["sellable"], None,
                            reason="교체", unit_cost=h["avg"], rate=h["rate"])
                state.setdefault("sell_orders", {})[code] = {"date": now().date().isoformat(), "qty": h["sellable"], "recorded": False}
                notify(f"교체 매도 {h['name']}", f"{h['sellable']}주 매도 주문 접수")
    if replacement.get("pending") and not any(c in held for c in replace_codes):
        replacement["pending"] = False
        plan["replacement"] = replacement
        save(PLAN, plan)
        log("기존 종목 교체 매도 완료 · 신규 매수 재개")
        notify("종목 교체 완료", "기존 보유 종목 정리가 확인되어 신규 매수를 재개합니다")

    # 모든 계좌 보유 종목을 감시한다. 과거 매수일이 저장되지 않은 종목은 14일 조건만 적용하지 않는다.
    for code, h in held.items():
        if code in replace_codes:
            continue
        meta = buys.get(code, {})
        d = meta.get("date")
        days = (now().date() - datetime.fromisoformat(d).date()).days if d else None
        reason = None
        if h["rate"] >= tp:
            reason = f"익절 {h['rate']:+.1f}%"
        elif h["rate"] <= sl:
            reason = f"손절 {h['rate']:+.1f}%"
        elif days is not None and days >= hold_days:
            reason = f"{hold_days}일 경과 정리 {h['rate']:+.1f}%"
        if reason and h["sellable"] > 0:
            # 같은 날짜에 동일 종목의 매도 주문을 중복 제출하지 않음
            sent = state.setdefault("sell_orders", {}).get(code, {})
            if sent.get("date") == now().date().isoformat() or any(o.get("code") == code and o.get("side") == "sell" and o.get("status") in ("accepted", "partial", "unmatched") for o in state.get("orders", {}).values()):
                continue
            result = kis.order("sell", code, h["sellable"])
            ok, msg = result["ok"], result["msg"]
            log(f"**매도** {h['name']} {h['sellable']}주 시장가 ({reason}) → {'접수' if ok else '실패: ' + msg}")
            notify(f"매도 {h['name']}", f"{h['sellable']}주 · {reason}" + ("" if ok else " · 실패: " + msg))
            if ok:
                track_order(state, result, "sell", code, h["name"], h["sellable"], None,
                            reason=reason, unit_cost=h["avg"], days=days, rate=h["rate"])
                state["sell_orders"][code] = {"date": now().date().isoformat(), "qty": h["sellable"], "recorded": False,
                                               "order_no": result.get("order_no")}

    # 교체 매도가 남아 있으면 신규 매수 금지
    if replacement.get("pending") or replacement_waiting:
        write_status(plan, state, kis.holdings(), kis)
        return

    # 투자한도는 계획 예산이 아니라 실제 보유원가 + 당일 미체결 예약금액 기준
    held_now = kis.holdings()
    invested = sum(h["avg"] * h["qty"] for h in held_now.values())
    # 미체결 매수 주문은 보유 여부와 무관하게 투자한도에서 예약액으로 차감한다.
    active_buy_orders = [o for o in state.get("orders", {}).values()
                         if o.get("side") == "buy" and o.get("status") in ("accepted", "partial", "unmatched")]
    pending_cost = sum(max(0, int(o.get("remaining_qty", 0) or 0)) * float(o.get("limit_price", 0) or 0)
                       for o in active_buy_orders)
    # 구버전 state에 주문 상세가 없을 때만 buys의 예약액을 보완 사용한다.
    if not active_buy_orders:
        pending_cost = sum(float(v.get("requested_krw", v.get("krw", 0)) or 0)
                           for v in buys.values() if v.get("pending", False)
                           and str(v.get("date", ""))[:10] == now().date().isoformat())
    available = max(0, cap - invested - pending_cost)
    eligible = []
    plan_key = str(plan.get("confirmed_at") or plan.get("created") or "legacy")
    plan_attempts = state.setdefault("plan_buy_attempts", {})
    for item in plan.get("stocks", []):
        code = item["code"]
        active_buy = any(o.get("code") == code and o.get("side") == "buy"
                         and o.get("status") in ("accepted", "partial", "unmatched")
                         for o in state.get("orders", {}).values())
        # 같은 확정 계획에서는 종목당 한 번만 주문한다. 보유 종목도 추가 매수 후보로 허용.
        attempt_key = f"{plan_key}:{code}"
        if active_buy or attempt_key in plan_attempts:
            continue
        meta = buys.get(code, {})
        if code not in held_now and int(meta.get("qty", 0) or 0) > 0:
            # 이미 매수 체결 이력이 있는 미보유 종목은 같은 계획으로 재매수하지 않는다.
            continue
        eligible.append(item)
    # 남은 한도를 미매수 후보 수로 나눠 특정 종목 하나가 현금을 독점하지 않도록 한다.
    per_stock_room = min(cap / 3, available / max(1, len(eligible))) if eligible else 0

    for item in eligible:
        code, name = item["code"], item.get("name", item["code"])
        price = kis.price(code)
        if not price:
            log(f"{name} 시세 조회 실패, 다음 회차에 재시도")
            continue
        if price > float(item["max_buy_price"]):
            continue
        px = round_up_tick(price * 1.003)
        budget = min(float(item.get("budget_krw", 0)), per_stock_room, available)
        qty = int(budget // px)
        if qty < 1:
            log(f"{name} 매수 보류 · 남은 투자 가능액 {won(available)}, 현재가 {won(price)}")
            continue
        cost = qty * px
        result = kis.order("buy", code, qty, px)
        ok, msg = result["ok"], result["msg"]
        log(f"**매수** {name} {qty}주 @ {won(px)} (예산 {won(budget)}) → {'접수' if ok else '실패: ' + msg}")
        notify(f"매수 {name}", f"{qty}주 @ {won(px)}" + ("" if ok else " · 실패: " + msg))
        if ok:
            state.setdefault("sell_orders", {}).pop(code, None)
            # 기존 보유 종목의 추가 매수에서도 누적 매수수량·금액과 최초 매수일을 보존한다.
            meta = buys.setdefault(code, {"date": now().isoformat(timespec="minutes"), "qty": 0, "krw": 0})
            meta["pending"] = True
            meta["requested_qty"] = int(qty)
            meta["requested_krw"] = float(cost)
            meta["last_order_date"] = now().isoformat(timespec="minutes")
            track_order(state, result, "buy", code, name, qty, px)
            plan_attempts[f"{plan_key}:{code}"] = {"date": now().isoformat(timespec="minutes"),
                                                     "order_no": result.get("order_no"), "qty": int(qty)}
            available -= cost

    write_status(plan, state, kis.holdings(), kis)


def write_status(plan, state, held, kis):
    """계좌 전체 보유 현황과 실제 계좌 잔액을 휴대폰 화면용으로 저장"""
    buys = state.get("buys", {})
    pos, cost, value = [], 0.0, 0.0
    for code, h in held.items():
        meta = buys.get(code, {})
        d = meta.get("date")
        days = (now().date() - datetime.fromisoformat(d).date()).days if d else None
        buy_amount = h["avg"] * h["qty"]
        eval_amount = h["now"] * h["qty"]
        pos.append({"code": code, "name": h["name"], "qty": h["qty"], "avg": h["avg"], "now": h["now"],
                    "rate": h["rate"], "days": days, "cost": round(buy_amount), "value": round(eval_amount),
                    "pnl": round(eval_amount - buy_amount), "tracked": bool(d)})
        cost += buy_amount
        value += eval_amount
    trades = state.get("trades", [])
    year = str(now().year)
    actual_trades = [t for t in trades if t.get("side") == "sell" and not t.get("estimated", False)]
    wins = sum(1 for t in actual_trades if t.get("pnl", 0) > 0)
    account = getattr(kis, "account_summary", {})
    cash = float(account.get("cash", 0))  # 증권사 주문가능 현금
    cap = float(plan.get("max_total_krw", 2000000))
    held_codes = set(held.keys())
    today = now().date().isoformat()
    reserved = sum(float(v.get("krw", 0)) for code, v in buys.items()
                   if code not in held_codes and v.get("pending", True)
                   and str(v.get("date", ""))[:10] == today)
    remaining = max(0, cap - cost - reserved)
    realized = sum(t.get("pnl", 0) for t in actual_trades)
    # 추천 분류(눌림목/조정 초입)별 승률 — 컨펌 당시 계획의 분류를 종목별로 기억해 둔다
    tiers = state.setdefault("tiers", {})
    for st in plan.get("stocks", []):
        w = str(st.get("why", ""))
        if w.startswith("눌림목") or w.startswith("조정 초입"):
            tiers[st["code"]] = "눌림목" if w.startswith("눌림목") else "조정 초입"
    by_tier = {}
    for t in actual_trades:
        k = t.get("tier") or tiers.get(t.get("code")) or "이전 계획"
        g = by_tier.setdefault(k, {"trades": 0, "wins": 0})
        g["trades"] += 1
        g["wins"] += 1 if t.get("pnl", 0) > 0 else 0
    for g in by_tier.values():
        g["win_rate"] = round(g["wins"] / g["trades"] * 100) if g["trades"] else None
    save(STATUS, {
        "updated": now().isoformat(timespec="minutes"), "mode": plan.get("mode", "demo"),
        "api": {"ok": True, "message": "계좌 잔고 조회 정상", "checked": now().isoformat(timespec="seconds"),
                "mode": plan.get("mode", "demo")},
        "positions": pos,
        "total": {"cost": round(cost), "value": round(value), "pnl": round(value - cost),
                  "rate": round((value / cost - 1) * 100, 2) if cost else 0,
                  "cash": round(cash), "assets": round(float(account.get("account_value") or (value + cash))),
                  "limit": round(cap), "invested": round(cost), "reserved": round(reserved),
                  "remaining": round(remaining)},
        "record": {"trades": len(actual_trades), "wins": wins,
                   "win_rate": round(wins / len(actual_trades) * 100, 1) if actual_trades else None,
                   "realized_year": sum(t.get("pnl", 0) for t in actual_trades if str(t.get("date", "")).startswith(year)),
                   "realized_total": round(realized), "estimated_legacy_count": sum(1 for t in trades if t.get("estimated")),
                   "by_tier": by_tier},
        # 주문 접수와 실제 체결을 구분해 화면에 전달한다. 체결가는 누적 체결금액/수량 기준.
        "orders": sorted([
            {"order_no": str(o.get("order_no", "")), "side": o.get("side"),
             "code": o.get("code"), "name": o.get("name"),
             "ordered_qty": int(o.get("ordered_qty", 0) or 0),
             "filled_qty": int(o.get("filled_qty", 0) or 0),
             "filled_amount": round(float(o.get("filled_amount", 0) or 0)),
             "avg_fill_price": round(float(o.get("filled_amount", 0) or 0) / int(o.get("filled_qty", 0)), 2) if int(o.get("filled_qty", 0) or 0) else 0,
             "remaining_qty": int(o.get("remaining_qty", o.get("ordered_qty", 0)) or 0),
             "status": o.get("status", "accepted"), "reason": o.get("reason"),
             "balance_qty": int(held.get(o.get("code"), {}).get("qty", 0)),
             "balance_avg": float(held.get(o.get("code"), {}).get("avg", 0)),
             "limit_price": float(o.get("limit_price", 0) or 0),
             "created": o.get("created"), "last_seen": o.get("last_seen")}
            for o in state.get("orders", {}).values()
        ], key=lambda x: str(x.get("created") or ""), reverse=True)[:100],
    })


def main():
    plan = load(PLAN, {})
    if plan.get("paused"):
        print("일시정지 상태 — 종료")
        return
    state = load(STATE, {})
    mode = plan.get("mode", "demo")
    try:
        kis = KIS(mode, plan.get("accounts"))
        if not market_open() and "--force" not in sys.argv:
            if "--check" in sys.argv:
                # 기존 코드의 중복 잔고 조회를 제거: 한 번 조회한 결과를 그대로 사용
                held = kis.holdings()
                write_status(plan, state, held, kis)
                log(f"API 연결 정상 ({'실전' if mode == 'real' else '모의'}) · 잔고 {len(held)}종목 확인 · 장외 주문 없음")
                notify("자동매매 API 연결 정상", f"{'실전' if mode == 'real' else '모의'} 계좌 잔고 조회 성공 · 보유 {len(held)}종목")
            else:
                print("국내 정규장 시간이 아님 — 종료")
            return
        run(plan, state, kis)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        log(f"API/매매 오류: {detail}")
        write_api_health(False, detail, mode)
        notify("자동매매 API 오류", detail[:350] + " · 앱의 최근 기록 확인")
    save(STATE, state)


if __name__ == "__main__":
    main()
