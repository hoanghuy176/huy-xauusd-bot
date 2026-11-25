import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("xauusd-tv-webhook")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "state.json")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
ATR_BUFFER_MULT = float(os.getenv("ATR_BUFFER_MULT", "0.25"))
MIN_SL_POINTS = float(os.getenv("MIN_SL_POINTS", "3.0"))
MAX_SL_POINTS = float(os.getenv("MAX_SL_POINTS", "25.0"))

NEWS_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
NEWS_WINDOW_MIN = int(os.getenv("NEWS_WINDOW_MINUTES", "60"))
NEWS_URL = os.getenv(
    "NEWS_FEED_URL",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
)

app = FastAPI(title="XAUUSD TradingView Webhook Bot")

LATEST_STATUS: Dict[str, Any] = {
    "last_webhook_utc": None,
    "last_signal": None,
    "news_status": "Chưa kiểm tra",
    "last_error": None,
}


def read_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Không đọc được state.json: %s", exc)
    return {"sent_keys": []}


def write_state(data: Dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Lỗi ghi state.json: %s", exc)


async def check_usd_high_news() -> tuple[bool, str]:
    if not NEWS_ENABLED:
        return False, "Tắt lọc tin"
    now_utc = datetime.now(timezone.utc)
    window = timedelta(minutes=NEWS_WINDOW_MIN)
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            res = await client.get(NEWS_URL, headers={"User-Agent": "Mozilla/5.0"})
            res.raise_for_status()
            events = res.json()

        if not isinstance(events, list):
            return False, "Không có dữ liệu lịch"

        for ev in events:
            if str(ev.get("country", "")).upper() != "USD":
                continue
            if str(ev.get("impact", "")).lower() != "high":
                continue
            raw_date = str(ev.get("date", "")).strip()
            if not raw_date:
                continue
            try:
                ev_dt = datetime.fromisoformat(raw_date)
                ev_dt = (
                    ev_dt.replace(tzinfo=timezone.utc)
                    if ev_dt.tzinfo is None
                    else ev_dt.astimezone(timezone.utc)
                )
            except ValueError:
                continue

            if abs(ev_dt - now_utc) <= window:
                title = str(ev.get("title", "USD High Impact"))
                return True, f"Chặn tin: {title} ({ev_dt.strftime('%H:%M UTC')})"

        return False, "An toàn (không có tin USD High sát giờ)"
    except Exception as exc:
        logger.warning("Lỗi tải lịch tin: %s", exc)
        return False, "Bỏ qua do lỗi nguồn tin"


def compute_sl_tp(
    side: str,
    entry: float,
    atr: float,
    swing_high: Optional[float],
    swing_low: Optional[float],
) -> tuple[float, float, float, float, str]:
    buffer = max(atr, 0.0) * ATR_BUFFER_MULT

    if side == "BUY":
        anchor = swing_low if swing_low is not None else (entry - max(MIN_SL_POINTS, atr * 1.5))
        raw_dist = max((entry - anchor) + buffer, 0.5)
        sl_dist = max(MIN_SL_POINTS, min(MAX_SL_POINTS, raw_dist))
        tp_dist = sl_dist * RR_RATIO
        return round(entry - sl_dist, 2), round(entry + tp_dist, 2), round(sl_dist, 2), round(tp_dist, 2), f"Hỗ trợ {anchor:.2f}"

    anchor = swing_high if swing_high is not None else (entry + max(MIN_SL_POINTS, atr * 1.5))
    raw_dist = max((anchor - entry) + buffer, 0.5)
    sl_dist = max(MIN_SL_POINTS, min(MAX_SL_POINTS, raw_dist))
    tp_dist = sl_dist * RR_RATIO
    return round(entry + sl_dist, 2), round(entry - tp_dist, 2), round(sl_dist, 2), round(tp_dist, 2), f"Kháng cự {anchor:.2f}"


async def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        logger.warning("Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        )
        res.raise_for_status()


@app.get("/")
async def root() -> Dict[str, Any]:
    return {"service": "xauusd-tv-webhook", "status": "live", "endpoints": ["/health", "/status", "/webhook"]}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "mode": "tradingview_webhook", "rr": f"1:{RR_RATIO:g}"}


@app.get("/status")
async def status() -> Dict[str, Any]:
    return LATEST_STATUS


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    LATEST_STATUS["last_webhook_utc"] = datetime.now(timezone.utc).isoformat()
    LATEST_STATUS["last_error"] = None

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON"})

    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Payload must be JSON object"})

    if WEBHOOK_SECRET and str(payload.get("secret", "")).strip() != WEBHOOK_SECRET:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Unauthorized secret"})

    side = str(payload.get("side", payload.get("action", ""))).upper().strip()
    if side not in ("BUY", "SELL"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "side must be BUY or SELL"})

    try:
        entry = float(payload["price"])
        atr = float(payload.get("atr", 4.0))
        swing_high = float(payload["swing_high"]) if payload.get("swing_high") is not None else None
        swing_low = float(payload["swing_low"]) if payload.get("swing_low") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"Missing/invalid numeric fields: {exc}"})

    symbol = str(payload.get("symbol", "XAUUSD"))
    bar_time = str(payload.get("time", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
    d1_info = str(payload.get("d1_trend", "D1 EMA20"))
    macro_info = str(payload.get("macro", "DXY / US10Y"))
    ind_info = str(payload.get("indicators", f"ATR={atr:.2f}"))

    blocked, news_msg = await check_usd_high_news()
    LATEST_STATUS["news_status"] = news_msg
    if blocked:
        return JSONResponse(status_code=200, content={"ok": True, "skipped": news_msg})

    sl, tp, sl_dist, tp_dist, sr_ref = compute_sl_tp(side, entry, atr, swing_high, swing_low)
    sig_key = f"{symbol}:{side}:{bar_time}"

    state = read_state()
    sent_keys: List[str] = state.setdefault("sent_keys", [])
    if sig_key in sent_keys:
        return JSONResponse(status_code=200, content={"ok": True, "skipped": "Duplicate signal"})

    badge = "🟢" if side == "BUY" else "🔴"
    msg = (
        f"{badge} <b>{symbol} | TradingView → {side}</b>\n"
        f"• Nến: <code>{bar_time}</code>\n"
        f"• Entry: <code>{entry:.2f}</code>\n"
        f"• SL: <code>{sl:.2f}</code> (<code>{sl_dist:.2f}</code> giá | {sr_ref})\n"
        f"• TP: <code>{tp:.2f}</code> (<code>{tp_dist:.2f}</code> giá | R:R <code>1:{RR_RATIO:g}</code>)\n"
        f"• Xu hướng D1: {d1_info}\n"
        f"• Chỉ báo: {ind_info}\n"
        f"• Vĩ mô: {macro_info}\n"
        f"• Tin tức: {news_msg}"
    )

    await send_telegram(msg)
    sent_keys.append(sig_key)
    state["sent_keys"] = sent_keys[-100:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state)

    LATEST_STATUS["last_signal"] = {
        "key": sig_key,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": f"1:{RR_RATIO:g}",
    }
    return JSONResponse(status_code=200, content={"ok": True, "signal": LATEST_STATUS["last_signal"]})


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
