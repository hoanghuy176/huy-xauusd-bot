import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("xauusd-tv-only")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "state.json")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
SR_BUFFER = float(os.getenv("SR_BUFFER", "0.5"))
MIN_SL_POINTS = float(os.getenv("MIN_SL_POINTS", "2.5"))
MAX_SL_POINTS = float(os.getenv("MAX_SL_POINTS", "30.0"))

NEWS_WINDOW_MIN = int(os.getenv("NEWS_WINDOW_MINUTES", "60"))
BLOCK_ON_CPI_NFP = os.getenv("BLOCK_ON_CPI_NFP", "true").lower() == "true"
FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

app = FastAPI(title="XAUUSD TradingView + ForexFactory Telegram Bot")

LATEST_STATUS: Dict[str, Any] = {
    "source": "TradingView + ForexFactory (No Twelve Data)",
    "last_update_utc": None,
    "cpi_nfp_status": "Chưa kiểm tra",
    "last_signal": None,
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


def is_cpi_or_nfp(title: str) -> Optional[str]:
    t = title.lower()
    if "cpi" in t or "consumer price index" in t:
        return "CPI"
    if "non-farm" in t or "nonfarm" in t or "nfp" in t:
        return "Non-Farm (NFP)"
    return None


async def check_cpi_nfp_forexfactory() -> Tuple[bool, str]:
    now_utc = datetime.now(timezone.utc)
    window = timedelta(minutes=NEWS_WINDOW_MIN)
    upcoming: List[str] = []

    for url in FF_URLS:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if res.status_code != 200:
                    continue
                events = res.json()

            if not isinstance(events, list):
                continue

            for ev in events:
                if str(ev.get("country", "")).upper() != "USD":
                    continue
                title = str(ev.get("title", "")).strip()
                tag = is_cpi_or_nfp(title)
                if not tag:
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

                diff = ev_dt - now_utc
                if abs(diff) <= window:
                    mins = int(diff.total_seconds() // 60)
                    info = f"{tag}: {title} ({ev_dt.strftime('%Y-%m-%d %H:%M UTC')}, {mins:+d}p)"
                    if BLOCK_ON_CPI_NFP:
                        return True, f"CHẶN LỆNH | {info}"
                    upcoming.append(info)
                elif timedelta(0) < diff <= timedelta(hours=48):
                    upcoming.append(f"{tag}: {title} ({ev_dt.strftime('%m-%d %H:%M UTC')})")
        except Exception as exc:
            logger.warning("Lỗi đọc ForexFactory (%s): %s", url, exc)

    if upcoming:
        return False, "Sắp tới | " + " ; ".join(dict.fromkeys(upcoming)[:3])
    return False, "Không có tin CPI / Non-Farm sát giờ"


def calc_sr_sl_tp(
    side: str,
    entry: float,
    support: Optional[float],
    resistance: Optional[float],
) -> Tuple[float, float, float, float, str]:
    if side == "BUY":
        level = support if support is not None else (entry - 6.0)
        raw_dist = max(abs(entry - level) + SR_BUFFER, 0.5)
        sl_dist = max(MIN_SL_POINTS, min(MAX_SL_POINTS, raw_dist))
        tp_dist = sl_dist * RR_RATIO
        return (
            round(entry - sl_dist, 2),
            round(entry + tp_dist, 2),
            round(sl_dist, 2),
            round(tp_dist, 2),
            f"Hỗ trợ {level:.2f}",
        )

    level = resistance if resistance is not None else (entry + 6.0)
    raw_dist = max(abs(level - entry) + SR_BUFFER, 0.5)
    sl_dist = max(MIN_SL_POINTS, min(MAX_SL_POINTS, raw_dist))
    tp_dist = sl_dist * RR_RATIO
    return (
        round(entry + sl_dist, 2),
        round(entry - tp_dist, 2),
        round(sl_dist, 2),
        round(tp_dist, 2),
        f"Kháng cự {level:.2f}",
    )


async def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        )
        res.raise_for_status()


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "xauusd-tv-only",
        "status": "live",
        "data_source": "TradingView + ForexFactory",
        "endpoints": ["/health", "/status", "/webhook", "/tv-signal"],
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "source": "tradingview_forexfactory",
        "rr": f"1:{RR_RATIO:g}",
    }


@app.get("/status")
async def status() -> Dict[str, Any]:
    blocked, news_msg = await check_cpi_nfp_forexfactory()
    LATEST_STATUS["cpi_nfp_status"] = news_msg
    LATEST_STATUS["blocked_by_news"] = blocked
    LATEST_STATUS["last_update_utc"] = datetime.now(timezone.utc).isoformat()
    return LATEST_STATUS


async def process_tv_payload(payload: Dict[str, Any]) -> JSONResponse:
    LATEST_STATUS["last_update_utc"] = datetime.now(timezone.utc).isoformat()
    LATEST_STATUS["last_error"] = None

    if WEBHOOK_SECRET and str(payload.get("secret", "")).strip() != WEBHOOK_SECRET:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Unauthorized secret"})

    side = str(payload.get("side", "")).upper().strip()
    if side not in ("BUY", "SELL"):
        return JSONResponse(status_code=400, content={"ok": False, "error": "side must be BUY or SELL"})

    try:
        entry = float(payload["price"])
        support = float(payload["support"]) if payload.get("support") is not None else None
        resistance = float(payload["resistance"]) if payload.get("resistance") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"Invalid numeric fields: {exc}"})

    symbol = str(payload.get("symbol", "XAUUSD"))
    bar_time = str(payload.get("time", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
    xau_d1 = str(payload.get("xau_d1", "N/A"))
    xau_h1 = str(payload.get("xau_h1", "N/A"))
    macd_info = str(payload.get("macd", "N/A"))
    dxy_info = str(payload.get("dxy_d1_h1", "N/A"))
    us10y_info = str(payload.get("us10y_d1_h1", "N/A"))

    blocked, news_status = await check_cpi_nfp_forexfactory()
    LATEST_STATUS["cpi_nfp_status"] = news_status
    if blocked:
        return JSONResponse(status_code=200, content={"ok": True, "skipped": news_status})

    sl, tp, sl_dist, tp_dist, sr_label = calc_sr_sl_tp(side, entry, support, resistance)
    sig_key = f"{symbol}:{side}:{bar_time}"

    state = read_state()
    sent_keys: List[str] = state.setdefault("sent_keys", [])
    if sig_key in sent_keys:
        return JSONResponse(status_code=200, content={"ok": True, "skipped": "Duplicate signal"})

    badge = "🟢" if side == "BUY" else "🔴"
    msg = (
        f"{badge} <b>{symbol} | {side} (R:R 1:{RR_RATIO:g})</b>\n"
        f"• Nguồn biểu đồ: <b>TradingView</b> | Nến: <code>{bar_time}</code>\n"
        f"• <b>Entry:</b> <code>{entry:.2f}</code>\n"
        f"• <b>SL:</b> <code>{sl:.2f}</code> (<code>{sl_dist:.2f}</code> giá | {sr_label})\n"
        f"• <b>TP:</b> <code>{tp:.2f}</code> (<code>{tp_dist:.2f}</code> giá)\n"
        f"• <b>XAUUSD D1:</b> {xau_d1}\n"
        f"• <b>XAUUSD H1:</b> {xau_h1}\n"
        f"• <b>MACD:</b> {macd_info}\n"
        f"• <b>DXY (D1/H1):</b> {dxy_info}\n"
        f"• <b>US10Y (D1/H1):</b> {us10y_info}\n"
        f"• <b>CPI / Non-Farm (ForexFactory):</b> {news_status}"
    )

    try:
        await send_telegram(msg)
    except Exception as exc:
        LATEST_STATUS["last_error"] = str(exc)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

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


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Payload must be JSON object"})
    return await process_tv_payload(payload)


@app.post("/tv-signal")
async def tv_signal(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Payload must be JSON object"})
    return await process_tv_payload(payload)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
