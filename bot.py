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
ARCHIVE = os.path.join(ROOT, "state_archive.json")

URLS = {
    "demo": "https://openapivts.koreainvestment.com:29443",
    "real": "https://openapi.koreainvestment.com:9443",
}
KST = ZoneInfo("Asia/Seoul")


def now():
    return datetime.now(KST)


_MASK = __import__("re").compile(r"(CANO=|ACNT_PRDT_CD=)[0-9]+")


def log(msg):
    msg = _MASK.sub(lambda m: m.group(1) + "****", str(msg))  # 오류 주소에 들어간 계좌번호 가리기
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
    return 9 * 60 <= m <= 15 * 60 + 15  # 장 시작 ~ 마감 15분 전 (9시 첫 실행에 미리 주문을 건다)


def tick(price):
    """국내 주식 호가 단위"""
    for limit, unit in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < limit:
            return unit
    return 1000


def round_down_tick(price):
    u = tick(price)
    return int(math.floor(price / u) * u)


def today_orders(state, code=None, side=None, active=True):
    """오늘 낸 주문만 (지정가 주문은 장 마감 때 소멸하므로 지난날 주문은 제외)"""
    t = now().date().isoformat()
    out = []
    for key, o in state.get("orders", {}).items():
        if str(o.get("created", ""))[:10] != t:
            continue
        if code and o.get("code") != code or side and o.get("side") != side:
            continue
        if active and o.get("status") not in ("accepted", "partial", "unmatched"):
            continue
        out.append((key, o))
    return out


def find_order(state, order_no):
    for o in state.get("orders", {}).values():
        if str(o.get("order_no")) == str(order_no):
            return o
    return {}


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

    _last_order = 0.0

    def _pace(self):
        """주문 API 사이 간격 확보 (모의 서버는 초당 처리 한도가 낮아 연속 주문 시 500 오류)"""
        gap = 1.2 if self.mode == "demo" else 0.3
        wait = KIS._last_order + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        KIS._last_order = time.time()

    def order(self, side, code, qty, price=None):
        """주문 접수 결과와 주문번호를 반환한다. 접수는 체결을 의미하지 않는다."""
        self._pace()
        tr_id = self.tr("TTTC0012U" if side == "buy" else "TTTC0011U")
        body = {"CANO": self.acct, "ACNT_PRDT_CD": self.prod, "PDNO": code,
                "ORD_DVSN": "01" if price is None else "00", "ORD_QTY": str(qty),
                "ORD_UNPR": "0" if price is None else str(price), "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""}
        r = requests.post(f"{self.base}/uapi/domestic-stock/v1/trading/order-cash", timeout=15,
                          headers=self._h(tr_id), json=body)
        if r.status_code >= 500:
            # 접수 여부가 불확실하므로 재전송하지 않는다 (중복 주문 방지). 다음 실행 때 체결내역으로 확인 후 다시 판단.
            return {"ok": False, "msg": f"증권사 서버 일시 오류({r.status_code}) · 다음 실행 때 다시 시도", "order_no": "", "branch": "", "raw": {}}
        r.raise_for_status()
        data = r.json()
        out = data.get("output") or {}
        return {"ok": data.get("rt_cd") == "0", "msg": data.get("msg1", ""),
                "order_no": str(out.get("ODNO") or out.get("odno") or ""),
                "branch": str(out.get("KRX_FWDG_ORD_ORGNO") or out.get("ord_gno_brno") or ""),
                "raw": data}

    def cancel(self, order_no, branch):
        """미체결 주문 전량 취소 (정정취소 API)"""
        self._pace()
        body = {"CANO": self.acct, "ACNT_PRDT_CD": self.prod, "KRX_FWDG_ORD_ORGNO": branch or "",
                "ORGN_ODNO": order_no, "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"}
        r = requests.post(f"{self.base}/uapi/domestic-stock/v1/trading/order-rvsecncl", timeout=15,
                          headers=self._h(self.tr("TTTC0013U")), json=body)
        r.raise_for_status()
        data = r.json()
        return data.get("rt_cd") == "0", data.get("msg1", "")

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


def sync_open_orders(kis, state, plan, held):
    """증권사에 실제로 걸려 있는 오늘 주문과 봇 기록을 맞춘다.
    - 기록에서 빠진 미체결 주문(동시 실행 등으로 유실)은 다시 기록 → 중복 주문·손절 막힘 방지
    - 계획에서 빠진 종목(앱에서 넘김 등)의 미체결 매수 주문은 취소
    - 같은 종목에 미체결 매수 주문이 2개 이상이면 마지막 것만 남기고 취소"""
    rows = kis.daily_fills()
    refs = {str(r.get("orgn_odno") or "").strip() for r in rows} - {""}
    open_rows = [r for r in rows if int(_num(r, "rmn_qty")) > 0 and r.get("cncl_yn") != "Y"
                 and not str(r.get("orgn_odno") or "").strip() and str(r.get("odno")) not in refs]
    orders = state.setdefault("orders", {})
    known = {str(o.get("order_no")): o for o in orders.values()}
    tday = now().strftime("%Y%m%d")
    related = ({s["code"] for s in plan.get("stocks", [])} | set(state.get("watch", {})) | set(state.get("buys", {}))
               | {o.get("code") for o in orders.values()})
    for r in open_rows:
        no = str(r.get("odno"))
        if no in known or str(r.get("pdno")) not in related:
            continue  # 봇과 관계없는 종목 주문(직접 낸 주문)은 기록하지 않는다
        side = "sell" if str(r.get("sll_buy_dvsn_cd")) == "01" else "buy"
        code = str(r.get("pdno"))
        h = held.get(code, {})
        o = {"order_no": no, "branch": str(r.get("ord_gno_brno") or ""), "side": side, "code": code,
             "name": r.get("prdt_name") or h.get("name") or code, "ordered_qty": int(_num(r, "ord_qty")),
             "limit_price": _num(r, "ord_unpr"), "filled_qty": int(_num(r, "tot_ccld_qty")),
             "filled_amount": _num(r, "tot_ccld_amt"), "remaining_qty": int(_num(r, "rmn_qty")),
             "created": now().isoformat(timespec="minutes"), "unit_cost": float(h.get("avg", 0)),
             "reason": "익절 예약 (복구)" if side == "sell" else "증권사 주문 복구",
             "status": "partial" if _num(r, "tot_ccld_qty") else "accepted", "last_seen": None}
        orders[f"{tday}:{no}"] = known[no] = o
        log(f"기록에 없던 미체결 주문 복구 · {o['name']} {'매도' if side == 'sell' else '매수'} {o['remaining_qty']}주 @ {won(o['limit_price'])}")
    plan_codes = {s["code"] for s in plan.get("stocks", [])}
    bot_codes = {o.get("code") for o in orders.values() if o.get("reason") != "증권사 주문 복구"} | set(state.get("watch", {}))
    by_code = {}
    for r in open_rows:
        if str(r.get("sll_buy_dvsn_cd")) != "01":
            by_code.setdefault(str(r.get("pdno")), []).append(r)
    for code, rs in by_code.items():
        rs.sort(key=lambda r: str(r.get("ord_tmd") or "") + str(r.get("odno")))
        if code not in plan_codes and code not in bot_codes:
            continue  # 봇이 다룬 적 없는 종목(직접 낸 주문)은 건드리지 않는다
        drop = rs if code not in plan_codes else rs[:-1]
        for r in drop:
            o = known.get(str(r.get("odno")), {})
            ok, msg = kis.cancel(str(r.get("odno")), str(r.get("ord_gno_brno") or o.get("branch") or ""))
            if ok:
                o["status"] = "canceled"
            why = "계획에서 빠진 종목" if code not in plan_codes else "중복 매수 주문"
            log(f"{o.get('name', code)} 미체결 매수 {int(_num(r, 'rmn_qty'))}주 취소 ({why}) → {'완료' if ok else '실패: ' + msg}")


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
                # 교체 매도가 아니면(익절·손절·기간 정리) 한도가 생겼으므로 같은 실행 끝에 새 계획을 자동으로 만든다
                if order.get("reason") != "교체" and not str(order.get("reason", "")).startswith("청산"):
                    need = state.setdefault("need_plan", {"since": now().isoformat(timespec="minutes"), "sold": []})
                    need["sold"].append(order.get("name"))
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
    try:
        sync_open_orders(kis, state, plan, held)
    except Exception as e:
        log(f"미체결 주문 대조 실패 · 다음 실행 때 재시도: {type(e).__name__}: {e}")

    # 청산 시각이 지났으면 다른 일은 하지 않고 전량 매도만
    if exit_due(plan):
        liquidate(plan, state, kis)
        write_status(plan, state, kis.holdings(), kis)
        return

    # 아직 보유하지 않은 계획 종목은 (교체 대기·주문 걸림과 상관없이) 매번 현재가를 기록한다 → 앱에서 지정가까지 남은 차이 표시
    watch0 = state.setdefault("watch", {})
    for item in plan.get("stocks", []):
        c = item["code"]
        if c in held:
            continue
        try:
            px_now = kis.price(c)
        except Exception:
            px_now = None
        if px_now:
            watch0[c] = {**watch0.get(c, {}), "price": px_now, "checked": now().isoformat(timespec="minutes")}
        time.sleep(0.3)

    # 교체 선택 시 기존 보유분 매도가 끝날 때까지 신규 매수는 보류
    replacement = plan.get("replacement") or {}
    replace_codes = set(replacement.get("sell_codes", [])) if replacement.get("pending") else set()
    # B 방식: 새 계획에도 들어 있는 보유 종목은 교체 매도하지 않고 그대로 보유 (부족분만 추가 매수)
    replace_codes -= {s_["code"] for s_ in plan.get("stocks", [])}
    replacement_waiting = False
    for code in list(replace_codes):
        h = held.get(code)
        if not h:
            continue
        replacement_waiting = True
        pending = state.setdefault("replacement_sell_orders", {})
        last = pending.get(code, {})
        # 걸어 둔 익절 예약 주문이 주식을 묶고 있으면 먼저 취소한다 (교체 매도가 막히지 않게)
        tp_orders = [(k, o) for k, o in today_orders(state, code, "sell") if str(o.get("reason", "")).startswith("익절 예약")]
        other_sell = [o for k, o in today_orders(state, code, "sell") if not str(o.get("reason", "")).startswith("익절 예약")]
        if last.get("date") == now().date().isoformat() or other_sell:
            continue
        for k, o in tp_orders:
            ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
            if ok:
                o["status"] = "canceled"
            log(f"{h['name']} 교체 매도를 위해 익절 예약 취소 → {'완료' if ok else '실패: ' + msg}")
        qty = h["qty"] if tp_orders else h["sellable"]
        if qty > 0:
            h = {**h, "sellable": qty}
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

    trail_mode = plan.get("tp_mode") == "trail"
    trail = float(plan.get("trail_pct", 2))
    peaks = state.setdefault("peaks", {})
    for c in list(peaks):  # 팔린 종목의 추적 기록 정리
        if c not in held:
            peaks.pop(c, None)
    # 모든 계좌 보유 종목을 감시한다. 과거 매수일이 저장되지 않은 종목은 14일 조건만 적용하지 않는다.
    #  - 익절: 증권사에 +익절가 지정가 매도 주문을 미리 걸어 둔다 (닿는 순간 체결)
    #  - 손절·기간 정리: 봇이 확인할 때 걸어 둔 익절 주문을 취소하고 시장가로 판다
    for code, h in held.items():
        if code in replace_codes:
            continue
        meta = buys.get(code, {})
        d = meta.get("date")
        days = (now().date() - datetime.fromisoformat(d).date()).days if d else None
        cut = None
        if h["rate"] <= sl:
            cut = f"손절 {h['rate']:+.1f}%"
        elif days is not None and days >= hold_days:
            cut = f"{hold_days}일 경과 정리 {h['rate']:+.1f}%"
        if cut and cut.startswith("손절") and str(plan.get("stop_hold_until", "")) >= now().date().isoformat():
            # 급락장 손절 보류: 앱에서 정한 날까지 손절 매도를 하지 않고 지켜본다 (익절은 그대로)
            held_log = state.setdefault("stop_hold_logged", {})
            if held_log.get(code) != now().date().isoformat():
                held_log[code] = now().date().isoformat()
                log(f"{h['name']} {cut} 이지만 손절 보류 중 ({plan.get('stop_hold_until')}까지) · 매도하지 않고 지켜봄")
            cut = None
        open_sells = today_orders(state, code, "sell")
        tp_open = [(k, o) for k, o in open_sells if str(o.get("reason", "")).startswith("익절 예약")]
        if cut:
            if any(o.get("reason") and not str(o.get("reason")).startswith("익절 예약") for _, o in open_sells):
                continue  # 이미 손절/정리 시장가 주문이 나가 있음
            for key, o in open_sells:  # 걸어 둔 익절 주문 취소
                ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
                if ok:
                    o["status"] = "canceled"
                log(f"{h['name']} 익절 예약 취소 ({cut}) → {'완료' if ok else '실패: ' + msg}")
                time.sleep(0.5)
            qty = h["qty"] if open_sells else h["sellable"]
            if qty <= 0:
                continue
            result = kis.order("sell", code, qty)
            ok, msg = result["ok"], result["msg"]
            log(f"**매도** {h['name']} {qty}주 시장가 ({cut}) → {'접수' if ok else '실패: ' + msg}")
            notify(f"매도 {h['name']}", f"{qty}주 · {cut}" + ("" if ok else " · 실패: " + msg))
            if ok:
                track_order(state, result, "sell", code, h["name"], qty, None,
                            reason=cut, unit_cost=h["avg"], days=days, rate=h["rate"])
            continue
        if plan.get("tp_mode") == "off":
            # 익절 없음: 손절·청산일로만 판다. 예전에 걸어 둔 익절 예약이 있으면 취소
            for k, o in tp_open:
                ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
                if ok:
                    o["status"] = "canceled"
                log(f"{h['name']} 익절 없음 설정 · 익절 예약 취소 → {'완료' if ok else '실패: ' + msg}")
            continue
        if trail_mode:
            # 추적 익절(C): 증권사 익절 예약 대신 봇이 최고가를 기억했다가, 최고가에서 trail% 빠지면 시장가 매도
            for k, o in tp_open:  # 예약 방식에서 바꿨다면 남아 있는 익절 예약 주문 취소
                ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
                if ok:
                    o["status"] = "canceled"
                log(f"{h['name']} 추적 익절로 전환 · 익절 예약 취소 → {'완료' if ok else '실패: ' + msg}")
            pk = peaks.get(code)
            if pk is None and h["rate"] >= tp:
                pk = peaks[code] = {"start": now().isoformat(timespec="minutes"), "peak": h["now"]}
                log(f"{h['name']} 익절선 +{tp:g}% 도달 ({h['rate']:+.1f}%) → 추적 시작 · 최고가에서 {trail:g}% 빠지면 매도")
                notify(f"추적 익절 시작 {h['name']}", f"{h['rate']:+.1f}% · 최고가에서 {trail:g}% 빠지면 매도")
            if pk is not None:
                pk["peak"] = max(pk["peak"], h["now"])
                line = pk["peak"] * (1 - trail / 100)
                pk["line"] = round(line)
                sell_qty = h["qty"] if tp_open else h["sellable"]
                if h["now"] <= line and sell_qty > 0 and not today_orders(state, code, "sell"):
                    peak_rate = (pk["peak"] / h["avg"] - 1) * 100 if h["avg"] else 0
                    why = f"추적 익절 (최고 {peak_rate:+.1f}% → {h['rate']:+.1f}%)"
                    result = kis.order("sell", code, sell_qty)
                    ok, msg = result["ok"], result["msg"]
                    log(f"**매도** {h['name']} {sell_qty}주 시장가 ({why}) → {'접수' if ok else '실패: ' + msg}")
                    notify(f"매도 {h['name']}", f"{sell_qty}주 · {why}" + ("" if ok else " · 실패: " + msg))
                    if ok:
                        track_order(state, result, "sell", code, h["name"], sell_qty, None,
                                    reason=why, unit_cost=h["avg"], days=days, rate=h["rate"])
            continue
        # 익절 예약(A): 아직 주문이 안 걸린 수량(sellable)만큼 익절가 지정가 매도를 건다
        if h["sellable"] > 0 and h["avg"] > 0:
            px = round_up_tick(h["avg"] * (1 + tp / 100))
            if h["now"] >= px:  # 이미 익절가 위면 바로 시장가로
                px, why = None, f"익절 {h['rate']:+.1f}%"
            else:
                why = f"익절 예약 +{tp:g}% ({won(px)})"
            result = kis.order("sell", code, h["sellable"], px)
            ok, msg = result["ok"], result["msg"]
            log(f"{'**매도**' if px is None else '익절 예약'} {h['name']} {h['sellable']}주 {'시장가' if px is None else '@ ' + won(px)} → {'접수' if ok else '실패: ' + msg}")
            if px is None:
                notify(f"매도 {h['name']}", f"{h['sellable']}주 · {why}")
            if ok:
                track_order(state, result, "sell", code, h["name"], h["sellable"], px,
                            reason=why, unit_cost=h["avg"], days=days, rate=h["rate"])
            time.sleep(0.3)

    # 청산일: 정한 날 정한 시각 이후에는 봇이 산 종목을 모두 시장가로 팔고, 그날은 새로 사지 않는다
    if exit_day(plan):
        if not state.get("exit_logged") == now().date().isoformat():
            state["exit_logged"] = now().date().isoformat()
            log(f"청산일({plan['exit_by']}) · 오늘은 새로 사지 않고 {plan.get('exit_time', '14:30')}에 전부 매도 예정")
        write_status(plan, state, kis.holdings(), kis)
        return

    # 교체 매도가 남아 있으면 신규 매수 금지
    if replacement.get("pending") or replacement_waiting:
        write_status(plan, state, kis.holdings(), kis)
        return

    # 투자한도는 계획 예산이 아니라 실제 보유원가 + 당일 미체결 예약금액 기준
    held_now = kis.holdings()
    invested = sum(h["avg"] * h["qty"] for h in held_now.values())
    # 미체결 매수 주문은 보유 여부와 무관하게 투자한도에서 예약액으로 차감한다.
    active_buy_orders = [o for o in state.get("orders", {}).values()
                         if o.get("side") == "buy" and o.get("status") in ("accepted", "partial", "unmatched")
                         and str(o.get("created", ""))[:10] == now().date().isoformat()]  # 지난날 주문은 소멸
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
    today = now().date().isoformat()
    watch = state.setdefault("watch", {})

    orders_all = state.get("orders", {})
    unfilled_limit = int(plan.get("unfilled_days", 2))
    dropped = []
    for item in plan.get("stocks", []):
        code = item["code"]
        # 오늘 낸 매수 주문이 살아 있으면 체결을 기다린다 (지정가 주문은 장 마감 때 자동 소멸)
        active_list = today_orders(state, code, "buy")
        # 앱에서 지정가를 올렸으면 오늘 걸어 둔 미체결 주문을 취소하고 새 지정가로 다시 건다
        new_lim = round_down_tick(float(item["max_buy_price"]))
        for key, o in active_list:
            if int(o.get("filled_qty", 0) or 0) == 0 and o.get("order_no") and float(o.get("limit_price", 0)) < new_lim:
                ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
                log(f"{item.get('name', code)} 지정가 변경 → 기존 주문 취소 {'완료' if ok else '실패: ' + msg}")
                if ok:
                    o["status"] = "canceled"
                    plan_attempts.pop(f"{plan_key}:{code}", None)
        active_buy = bool(today_orders(state, code, "buy"))
        attempt_key = f"{plan_key}:{code}"
        att = plan_attempts.get(attempt_key)
        if att and not active_buy:
            # 지난날 주문이 한 주도 체결되지 않고 소멸했으면 같은 계획으로 다시 감시·주문한다.
            o = find_order(state, att.get("order_no"))
            if str(att.get("date", ""))[:10] < today and int(o.get("filled_qty", 0) or 0) == 0:
                plan_attempts.pop(attempt_key, None)
                att = None
                # 방법 2: 지정가 주문이 여러 날 계속 안 사지면 계획에서 빼고, 묶였던 돈으로 새 계획을 만든다
                misses = unfilled_days(state, code, plan)
                if code not in held_now and misses >= unfilled_limit:
                    nm = item.get("name", code)
                    dropped.append(code)
                    state.setdefault("dropped", {})[code] = {"date": today, "name": nm, "days": misses}
                    watch[code] = {**watch.get(code, {}), "state": "dropped", "checked": now().isoformat(timespec="minutes")}
                    need = state.setdefault("need_plan", {"since": now().isoformat(timespec="minutes"), "sold": []})
                    need["sold"].append(f"{nm}(미체결)")
                    log(f"{nm} 지정가 {won(round_down_tick(float(item['max_buy_price'])))} 매수가 {misses}일 연속 미체결 → 계획에서 정리 · 묶였던 돈은 새 계획에 사용")
                    notify(f"미체결 정리 {nm}", f"{misses}일 동안 지정가에 안 사져서 계획에서 뺐습니다 · 새 계획을 만듭니다")
                    continue
                log(f"{item.get('name', code)} 지난 주문 미체결({misses}일째) → 오늘 다시 감시")
        if active_buy:
            watch[code] = {**watch.get(code, {}), "state": "ordered", "checked": now().isoformat(timespec="minutes")}
            continue
        if att:
            watch[code] = {**watch.get(code, {}), "state": "done", "checked": now().isoformat(timespec="minutes")}
            continue
        # 같은 계획 안에서의 재매수는 위 attempt 기록이 막는다. 예전에 사고판 종목도 새 계획이면 다시 살 수 있다.
        eligible.append(item)
    if dropped:
        plan["stocks"] = [s_ for s_ in plan.get("stocks", []) if s_["code"] not in dropped]
        save(PLAN, plan)
    # 남은 한도를 미매수 후보 수로 나눠 특정 종목 하나가 현금을 독점하지 않도록 한다.
    per_stock_room = min(cap / 3, available / max(1, len(eligible))) if eligible else 0

    for item in eligible:
        code, name = item["code"], item.get("name", item["code"])
        price = kis.price(code)
        if not price:
            log(f"{name} 시세 조회 실패, 다음 회차에 재시도")
            continue
        prev_state = watch.get(code, {}).get("state")
        watch[code] = {**watch.get(code, {}), "price": price, "checked": now().isoformat(timespec="minutes"), "state": "above"}
        # 미리 주문: 현재가와 상관없이 지정가 매수 주문을 증권사에 걸어 둔다.
        # 현재가가 이미 지정가 이하면 즉시 체결, 아니면 가격이 닿는 순간 증권사가 체결한다.
        px = round_down_tick(float(item["max_buy_price"]))
        budget = min(float(item.get("budget_krw", 0)), per_stock_room, available)
        if code in held_now:
            # 이미 보유한 종목은 새 계획 예산에서 보유 금액을 뺀 부족분만 산다
            have_krw = held_now[code]["avg"] * held_now[code]["qty"]
            budget = min(budget, float(item.get("budget_krw", 0)) - have_krw)
            if budget < px:
                watch[code] = {**watch.get(code, {}), "state": "enough"}
                plan_attempts[f"{plan_key}:{code}"] = {"date": now().isoformat(timespec="minutes"), "order_no": None, "qty": 0, "enough": True}
                log(f"{name} 이미 {won(have_krw)} 보유 · 새 계획 예산 {won(float(item.get('budget_krw', 0)))} 대비 부족분이 1주 미만이라 추가 매수 없음")
                continue
        qty = int(budget // px)
        plan_budget = float(item.get("budget_krw", 0))
        if code not in held_now and plan_budget and budget < plan_budget * 0.5:
            # 남은 한도가 계획 예산의 절반도 안 되면 자투리 매수를 하지 않고, 보유 종목이 팔려 한도가 생길 때까지 기다린다
            if prev_state != "no_budget":
                log(f"{name} 매수 보류 · 남은 투자 가능액 {won(available)} (계획 예산 {won(plan_budget)}의 절반 미만) · 한도가 생기면 매수")
            watch[code] = {**watch.get(code, {}), "state": "no_budget", "room": round(available)}
            continue
        if qty < 1:
            if prev_state != "no_budget":
                log(f"{name} 매수 보류 · 남은 투자 가능액 {won(available)}, 현재가 {won(price)}")
            watch[code] = {**watch.get(code, {}), "state": "no_budget", "room": round(available)}
            continue
        cost = qty * px
        result = kis.order("buy", code, qty, px)
        ok, msg = result["ok"], result["msg"]
        log(f"**매수 주문 걸기** {name} {qty}주 @ {won(px)} 지정가 (현재가 {won(price)}, 예산 {won(budget)}) → {'접수' if ok else '실패: ' + msg}")
        if not ok:
            notify(f"매수 주문 실패 {name}", msg)
        watch[code]["state"] = "ordered" if ok else "failed"
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

    if plan.get("topup", True) and not eligible_waiting(watch, eligible):
        top_up(plan, state, kis, held_now, available, cap, plan_key)
    write_status(plan, state, kis.holdings(), kis)


def eligible_waiting(watch, eligible):
    """아직 주문이 안 나간 계획 종목이 있으면(가격·시세 문제 등) 잔돈 채우기를 미룬다. 한도 부족 대기는 제외."""
    return any(watch.get(i["code"], {}).get("state") not in ("ordered", "no_budget", "done", "enough", "dropped")
               for i in eligible)


def unfilled_days(state, code, plan):
    """이 계획에서 이 종목 지정가 매수가 한 주도 안 사지고 지나간 날 수"""
    since = str(plan.get("confirmed_at") or "")
    try:
        since = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(KST).isoformat(timespec="minutes")
    except ValueError:
        since = ""
    today = now().date().isoformat()
    days = {str(o.get("created", ""))[:10] for o in state.get("orders", {}).values()
            if o.get("side") == "buy" and o.get("code") == code and str(o.get("created", "")) >= since
            and str(o.get("created", ""))[:10] < today and int(o.get("filled_qty", 0) or 0) == 0}
    return len(days)


def top_up(plan, state, kis, held_now, available, cap, plan_key):
    """방법 1 · 잔돈 채우기: 새 계획을 만들기엔 작은 남은 돈을 보유 중인 계획 종목에 1주 단위로 더 산다.
    - 남은 돈이 한도의 1/6(새 계획 기준) 이상이면 새 계획 몫이므로 건드리지 않는다
    - 매수가(최대매수가) 이하이고, 손실이 손절선의 절반보다 얕고, 추적 익절 중이 아닌 종목만
    - 한 종목 합계는 한도의 1/3을 넘지 않는다 · 1주 가격이 싼 종목부터 · 종목당 하루 한 번"""
    sl = float(plan.get("stop_loss_pct", -7))
    if available < 10000 or available >= cap / 3 * 0.5:
        return
    today = now().date().isoformat()
    done = state.setdefault("topups", {})
    peaks = state.get("peaks", {})
    cands = []
    for item in plan.get("stocks", []):
        code = item["code"]
        h = held_now.get(code)
        if not h or code in peaks or done.get(code) == today or today_orders(state, code, "buy"):
            continue
        lim = round_down_tick(float(item["max_buy_price"]))
        if h["now"] > lim or h["rate"] <= sl / 2:
            continue
        room = cap / 3 - h["avg"] * h["qty"]
        if room >= lim:
            cands.append((lim, code, item.get("name", code), room, h))
    for lim, code, name, room, h in sorted(cands):
        qty = int(min(available, room) // lim)
        if qty < 1:
            continue
        result = kis.order("buy", code, qty, lim)
        ok, msg = result["ok"], result["msg"]
        log(f"**잔돈 채우기** {name} {qty}주 @ {won(lim)} 지정가 (남은 돈 {won(available)}) → {'접수' if ok else '실패: ' + msg}")
        done[code] = today
        if ok:
            track_order(state, result, "buy", code, name, qty, lim, reason="잔돈 채우기")
            meta = state.setdefault("buys", {}).setdefault(code, {"date": now().isoformat(timespec="minutes"), "qty": 0, "krw": 0})
            meta["pending"] = True
            meta["requested_qty"] = int(qty)
            meta["requested_krw"] = float(qty * lim)
            meta["last_order_date"] = now().isoformat(timespec="minutes")
            available -= qty * lim


def exit_day(plan):
    d = str(plan.get("exit_by") or "")
    return bool(d) and now().date().isoformat() >= d


def exit_due(plan):
    d = str(plan.get("exit_by") or "")
    if not d:
        return False
    today = now().date().isoformat()
    return today > d or (today == d and now().strftime("%H:%M") >= str(plan.get("exit_time", "14:30")))


def liquidate(plan, state, kis):
    """청산: 미체결 매수 취소 → 익절 예약 등 매도 주문 취소 → 보유 전량 시장가 매도 (종목당 하루 한 번)"""
    today = now().date().isoformat()
    done = state.setdefault("exit_sells", {})
    for code, o in [(o.get("code"), o) for _, o in today_orders(state, side="buy")]:
        ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
        if ok:
            o["status"] = "canceled"
        log(f"청산 · {o.get('name', code)} 미체결 매수 취소 → {'완료' if ok else '실패: ' + msg}")
    held = kis.holdings()
    for code, h in held.items():
        if done.get(code) == today:
            continue
        for _, o in today_orders(state, code, "sell"):
            if str(o.get("reason", "")).startswith("청산"):
                continue
            ok, msg = kis.cancel(o.get("order_no"), o.get("branch"))
            if ok:
                o["status"] = "canceled"
        time.sleep(0.5)
        qty = int(h["qty"])
        if qty <= 0:
            continue
        result = kis.order("sell", code, qty)
        ok = result["ok"]
        log(f"**청산 매도** {h['name']} {qty}주 시장가 ({plan['exit_by']} 출금 일정) → {'접수' if ok else '실패: ' + result['msg']}")
        if ok:
            done[code] = today
            track_order(state, result, "sell", code, h["name"], qty, None, reason="청산 (출금 일정)",
                        unit_cost=h["avg"], rate=h["rate"])
            notify(f"청산 매도 {h['name']}", f"{qty}주 시장가 · 수익률 {h['rate']:+.2f}%")
    if held and all(done.get(c) == today for c in held):
        if state.get("exit_notified") != today:
            state["exit_notified"] = today
            notify("청산 주문 완료", "봇이 산 종목을 모두 매도 주문했습니다 · 2영업일 뒤 출금 가능")


def own_holdings(raw, state):
    """실전 계좌에는 존박이 직접 산 종목이 있을 수 있으므로 봇이 산 종목만 관리한다"""
    mine = set(state.get("buys", {})) | {o.get("code") for o in state.get("orders", {}).values()
                                          if o.get("side") == "buy" and int(o.get("filled_qty", 0) or 0) > 0}
    return {c: h for c, h in raw.items() if c in mine}


def recover_missing(state, held):
    """기록 저장이 실패해 매수 기록이 빠진 보유 종목을 찾아 복구 (매수일은 처음 발견한 날로)"""
    buys = state.setdefault("buys", {})
    orders = state.setdefault("orders", {})
    for code, h in held.items():
        if code in buys:
            continue
        stamp = now().isoformat(timespec="minutes")
        buys[code] = {"date": stamp, "qty": h["qty"], "krw": round(h["avg"] * h["qty"]), "inferred": True}
        orders[f"{now():%Y%m%d}:recovered-{code}"] = {
            "order_no": f"recovered-{code}", "branch": "", "side": "buy", "code": code, "name": h["name"],
            "ordered_qty": h["qty"], "limit_price": h["avg"], "filled_qty": h["qty"],
            "filled_amount": round(h["avg"] * h["qty"]), "remaining_qty": 0, "created": stamp,
            "reason": "기록 복구 (체결 기록이 저장되지 않았던 매수)", "status": "filled", "last_seen": stamp}
        log(f"{h['name']} 보유 중인데 매수 기록이 없어 복구 · {h['qty']}주 평균 {won(h['avg'])} · 14일 계산은 오늘부터")


def write_status(plan, state, held, kis):
    """계좌 전체 보유 현황과 실제 계좌 잔액을 휴대폰 화면용으로 저장"""
    recover_missing(state, held)
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
                    "pnl": round(eval_amount - buy_amount), "tracked": bool(d),
                    "buy_date": str(d)[:10] if d else None})
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
    # 미체결 매수 주문에 묶인 돈: 오늘 낸 주문(재주문 포함)의 요청 금액
    reserved = sum(max(0, int(o.get("remaining_qty", 0) or 0)) * float(o.get("limit_price", 0) or 0)
                   for _, o in today_orders(state, side="buy"))
    remaining = max(0, cap - cost - reserved)
    realized = sum(t.get("pnl", 0) for t in actual_trades)
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
        # 매매 내역 (최근 매도 20건): 매수일·매도일·보유일·손익
        "history": [{"code": t.get("code"), "name": t.get("name"),
                     "sell_date": str(t.get("date", ""))[:10],
                     "buy_date": ((datetime.fromisoformat(t["date"]) - timedelta(days=int(t["days"]))).date().isoformat()
                                  if t.get("days") is not None and t.get("date") else None),
                     "days": t.get("days"), "qty": t.get("qty"), "price": t.get("price"),
                     "pnl": t.get("pnl", 0), "reason": t.get("reason")}
                    for t in reversed(actual_trades[-20:])],
        # 매수 대기 종목 감시 상태 (현재가·마지막 확인 시각·상태)
        "watch": {c: w for c, w in state.get("watch", {}).items()
                  if c in {st["code"] for st in plan.get("stocks", [])}},
        "trail": {"mode": plan.get("tp_mode", "limit"), "pct": float(plan.get("trail_pct", 2)),
                  "peaks": {c: v for c, v in state.get("peaks", {}).items() if c in held}},
        "record": {"trades": len(actual_trades), "wins": wins,
                   "win_rate": round(wins / len(actual_trades) * 100, 1) if actual_trades else None,
                   "realized_year": sum(t.get("pnl", 0) for t in actual_trades if str(t.get("date", "")).startswith(year)),
                   "realized_total": round(realized), "estimated_legacy_count": sum(1 for t in trades if t.get("estimated"))},
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
    if state and state.get("mode", "demo") != mode:
        # 모의 ↔ 실전 전환: 이전 기록은 보관하고 새로 시작 (기록이 섞이지 않게)
        old = state.get("mode", "demo")
        arch = load(ARCHIVE, {})
        arch[f"{old}_{now():%Y%m%d_%H%M}"] = state
        save(ARCHIVE, arch)
        log(f"{'실전' if mode == 'real' else '모의'} 전환 · 이전 {'실전' if old == 'real' else '모의'} 기록은 보관하고 새로 시작")
        state = {}
        # 이전 모드의 매수 계획으로 실제 돈이 나가지 않도록 계획 종목을 비운다 → 새 계획을 컨펌해야 매수 시작
        if plan.get("stocks"):
            log("전환 전 매수 계획(" + ", ".join(s.get("name", s["code"]) for s in plan["stocks"]) + ")은 비움 · 새 계획 컨펌 후 매수")
        plan["stocks"] = []
        plan.pop("proposal", None)
        plan["replacement"] = {"pending": False, "sell_codes": [], "mode": "keep"}
        save(PLAN, plan)
    state["mode"] = mode
    try:
        kis = KIS(mode, plan.get("accounts"))
        if mode == "real" and not plan.get("manage_all_real"):
            raw_holdings = kis.holdings
            kis.holdings = lambda: own_holdings(raw_holdings(), state)
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
