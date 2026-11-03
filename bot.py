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
