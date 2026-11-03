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
