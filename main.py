import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
import uvicorn

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("xauusd-bot")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "state.json")

# Khung thời gian & quản lý lệnh (R:R 1:2 trên H1)
SYMBOL = os.getenv("SYMBOL", "XAU/USD")
TF_TREND = "1d"
TF_ENTRY = "1h"
SL_DISTANCE = float(os.getenv("SL_POINTS", "6.0"))
TP_DISTANCE = float(os.getenv("TP_POINTS", os.getenv("TP2_POINTS", "12.0")))

# Chỉ báo kỹ thuật
EMA_D1_LEN = int(os.getenv("D1_EMA_PERIOD", "20"))
EMA_H1_LEN = int(os.getenv("H1_EMA_PERIOD", "20"))
RSI_LEN = int(os.getenv("RSI_PERIOD", "14"))
ATR_LEN = int(os.getenv("ATR_PERIOD", "14"))
LOOKBACK_H1 = int(os.getenv("BREAKOUT_LOOKBACK", "5"))
RSI_OVERBOUGHT = float(os.getenv("RSI_BUY_MAX", "72.0"))
RSI_OVERSOLD = float(os.getenv("RSI_SELL_MIN", "28.0"))
MIN_ATR_H1 = float(os.getenv("MIN_ATR_H1", "2.0"))

# Lọc tin tức kinh tế
NEWS_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
NEWS_WINDOW_MIN = int(os.getenv("NEWS_WINDOW_MINUTES", "60"))
NEWS_URL = os.getenv(
    "NEWS_FEED_URL",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
)

# Vĩ mô: DXY & US10Y
MACRO_ENABLED = os.getenv("MACRO_FILTER_ENABLED", "true").lower() == "true"
DXY_TICKER = os.getenv("DXY_SYMBOL", "DXY")
US10Y_TICKER = os.getenv("US10Y_SYMBOL", "US10Y")

LOOP_SECONDS = int(os.getenv("POLL_INTERVAL_SEC", "60"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TD_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

app = FastAPI(title="XAUUSD D1-H1 Bot")

LATEST_SNAPSHOT: Dict[str, Any] = {
    "last_check_utc": None,
    "last_closed_h1": None,
    "d1_bias": "UNKNOWN",
    "macro_summary": "Chưa chạy",
    "news_status": "Chưa chạy",
    "last_error": None,
}


def read_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception as exc:
            logger.warning("Không đọc được state.json, khởi tạo mới: %s", exc)
    return {"sent_keys": []}


def write_state(data: Dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Lỗi ghi state.json: %s", exc)


def series_ema(values: List[float], length: int) -> List[Optional[float]]:
    if length <= 0 or len(values) < length:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    alpha = 2.0 / (length + 1)
    prev = seed
    for i in range(length, len(values)):
        cur = (values[i] - prev) * alpha + prev
        out[i] = cur
        prev = cur
    return out


def series_rsi(closes: List[float], length: int = 14) -> List[Optional[float]]:
    if length <= 0 or len(closes) <= length:
        return [None] * len(closes)
    out: List[Optional[float]] = [None] * len(closes)
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, length + 1)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, length + 1)]
    avg_g = sum(gains) / length
    avg_l = sum(losses) / length
    out[length] = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))

    for i in range(length + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0.0)
        l = max(-diff, 0.0)
        avg_g = (avg_g * (length - 1) + g) / length
        avg_l = (avg_l * (length - 1) + l) / length
        out[i] = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    return out


def series_atr(candles: List[Dict[str, Any]], length: int = 14) -> List[Optional[float]]:
    n = len(candles)
    if length <= 0 or n <= length:
        return [None] * n
    trs: List[float] = []
    for i in range(1, n):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    out: List[Optional[float]] = [None] * n
    atr_cur = sum(trs[:length]) / length
    out[length] = atr_cur
    for i in range(length + 1, n):
        tr_val = trs[i - 1]
        atr_cur = (atr_cur * (length - 1) + tr_val) / length
        out[i] = atr_cur
    return out


async def get_ohlc(symbol: str, interval: str, count: int = 120) -> List[Dict[str, Any]]:
    if not TD_API_KEY:
        raise RuntimeError("Thiếu TWELVE_DATA_API_KEY trong file .env")

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": count,
        "apikey": TD_API_KEY,
        "timezone": "UTC",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.get("https://api.twelvedata.com/time_series", params=params)
        res.raise_for_status()
        body = res.json()

    rows = body.get("values")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Không có dữ liệu nến cho {symbol} ({interval}): {body}")

    return [
        {
            "time": str(item["datetime"]),
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
        }
        for item in reversed(rows)
    ]


async def get_news_filter() -> Tuple[bool, str]:
    if not NEWS_ENABLED:
        return False, "Tắt"

    now_utc = datetime.now(timezone.utc)
    delta = timedelta(minutes=NEWS_WINDOW_MIN)
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
            dt_raw = str(ev.get("date", "")).strip()
            if not dt_raw:
                continue
            try:
                ev_time = datetime.fromisoformat(dt_raw)
                ev_time = (
                    ev_time.replace(tzinfo=timezone.utc)
                    if ev_time.tzinfo is None
                    else ev_time.astimezone(timezone.utc)
                )
            except ValueError:
                continue

            if abs(ev_time - now_utc) <= delta:
                title = str(ev.get("title", "USD High Impact"))
                return True, f"Chặn: {title} ({ev_time.strftime('%H:%M UTC')})"

        return False, "An toàn (không có tin USD High sát giờ)"
    except Exception as exc:
        logger.warning("Lỗi tải lịch tin tức: %s", exc)
        return False, "Bỏ qua do lỗi nguồn tin"


async def get_macro_view() -> Tuple[str, str]:
    """
    Trả về ("USD_UP", mô_tả), ("USD_DOWN", mô_tả), hoặc ("NEUTRAL", mô_tả)
    dựa trên DXY và US10Y.
    """
    if not MACRO_ENABLED:
        return "NEUTRAL", "Tắt lọc DXY/US10Y"

    notes: List[str] = []
    up_count = 0
    down_count = 0

    for label, ticker in [("DXY", DXY_TICKER), ("US10Y", US10Y_TICKER)]:
        try:
            candles = await get_ohlc(ticker, TF_ENTRY, count=10)
            if len(candles) >= 2:
                prev = candles[-2]
                if prev["close"] > prev["open"]:
                    up_count += 1
                    notes.append(f"{label} tăng ({prev['close']:.2f})")
                elif prev["close"] < prev["open"]:
                    down_count += 1
                    notes.append(f"{label} giảm ({prev['close']:.2f})")
                else:
                    notes.append(f"{label} đi ngang ({prev['close']:.2f})")
        except Exception as exc:
            logger.warning("Không lấy được %s (%s): %s", label, ticker, exc)
            notes.append(f"{label} lỗi/không hỗ trợ")

    text = " | ".join(notes) if notes else "Không có dữ liệu vĩ mô"
    if up_count >= 2:
        return "USD_UP", text
    if down_count >= 2:
        return "USD_DOWN", text
    return "NEUTRAL", text


def get_d1_direction(d1_candles: List[Dict[str, Any]]) -> Tuple[str, str]:
    if len(d1_candles) < EMA_D1_LEN + 2:
        return "NONE", "Chưa đủ nến D1"

    closes = [c["close"] for c in d1_candles]
    emas = series_ema(closes, EMA_D1_LEN)
    last_closed = d1_candles[-2]
    ema_val = emas[-2]
    if ema_val is None:
        return "NONE", "Chưa có EMA D1"

    if last_closed["close"] > ema_val and last_closed["close"] >= last_closed["open"]:
        return "BUY", f"D1 ({last_closed['close']:.2f}) > EMA{EMA_D1_LEN} ({ema_val:.2f})"
    if last_closed["close"] < ema_val and last_closed["close"] <= last_closed["open"]:
        return "SELL", f"D1 ({last_closed['close']:.2f}) < EMA{EMA_D1_LEN} ({ema_val:.2f})"
    return "NONE", f"D1 chưa rõ hướng quanh EMA{EMA_D1_LEN} ({ema_val:.2f})"


def build_h1_signal(
    d1_candles: List[Dict[str, Any]],
    h1_candles: List[Dict[str, Any]],
    macro_dir: str,
    macro_text: str,
) -> Optional[Dict[str, Any]]:
    d1_dir, d1_note = get_d1_direction(d1_candles)
    LATEST_SNAPSHOT["d1_bias"] = f"{d1_dir} ({d1_note})"

    if d1_dir == "NONE":
        return None

    min_bars = max(EMA_H1_LEN, RSI_LEN, ATR_LEN, LOOKBACK_H1) + 3
    if len(h1_candles) < min_bars:
        return None

    closed_h1 = h1_candles[-2]
    LATEST_SNAPSHOT["last_closed_h1"] = closed_h1["time"]

    lookback_slice = h1_candles[-(LOOKBACK_H1 + 2) : -2]
    if len(lookback_slice) < LOOKBACK_H1:
        return None

    h1_closes = [c["close"] for c in h1_candles]
    ema_h1 = series_ema(h1_closes, EMA_H1_LEN)[-2]
    rsi_h1 = series_rsi(h1_closes, RSI_LEN)[-2]
    atr_h1 = series_atr(h1_candles, ATR_LEN)[-2]

    if ema_h1 is None or rsi_h1 is None or atr_h1 is None:
        return None
    if atr_h1 < MIN_ATR_H1:
        return None

    high_ref = max(c["high"] for c in lookback_slice)
    low_ref = min(c["low"] for c in lookback_slice)
    entry = round(closed_h1["close"], 2)

    if d1_dir == "BUY":
        if macro_dir == "USD_UP":
            return None
        if closed_h1["close"] > high_ref and closed_h1["close"] > ema_h1 and rsi_h1 <= RSI_OVERBOUGHT:
            return {
                "side": "BUY",
                "time": closed_h1["time"],
                "entry": entry,
                "sl": round(entry - SL_DISTANCE, 2),
                "tp": round(entry + TP_DISTANCE, 2),
                "rr": "1:2",
                "ind": f"EMA{EMA_H1_LEN}={ema_h1:.2f} | RSI={rsi_h1:.1f} | ATR={atr_h1:.2f}",
                "macro": macro_text,
                "trend": d1_note,
            }

    if d1_dir == "SELL":
        if macro_dir == "USD_DOWN":
            return None
        if closed_h1["close"] < low_ref and closed_h1["close"] < ema_h1 and rsi_h1 >= RSI_OVERSOLD:
            return {
                "side": "SELL",
                "time": closed_h1["time"],
                "entry": entry,
                "sl": round(entry + SL_DISTANCE, 2),
                "tp": round(entry - TP_DISTANCE, 2),
                "rr": "1:2",
                "ind": f"EMA{EMA_H1_LEN}={ema_h1:.2f} | RSI={rsi_h1:.1f} | ATR={atr_h1:.2f}",
                "macro": macro_text,
                "trend": d1_note,
            }

    return None


async def send_tg(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        logger.warning("Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            url,
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
        )
        res.raise_for_status()


def format_tg_signal(sig: Dict[str, Any], news_text: str) -> str:
    badge = "🟢" if sig["side"] == "BUY" else "🔴"
    return (
        f"{badge} <b>{SYMBOL} | D1 → H1 | {sig['side']}</b>\n"
        f"• Khung vào lệnh: <code>H1</code> (Nến đóng: <code>{sig['time']}</code>)\n"
        f"• Entry: <code>{sig['entry']:.2f}</code>\n"
        f"• SL: <code>{sig['sl']:.2f}</code> (<code>{SL_DISTANCE:.1f}</code>)\n"
        f"• TP: <code>{sig['tp']:.2f}</code> (<code>{TP_DISTANCE:.1f}</code> | R:R <code>{sig['rr']}</code>)\n"
        f"• Xu hướng D1: {sig['trend']}\n"
        f"• Chỉ báo H1: {sig['ind']}\n"
        f"• DXY & US10Y: {sig['macro']}\n"
        f"• Tin tức: {news_text}"
    )


async def scan_once() -> None:
    LATEST_SNAPSHOT["last_check_utc"] = datetime.now(timezone.utc).isoformat()
    LATEST_SNAPSHOT["last_error"] = None

    blocked, news_text = await get_news_filter()
    LATEST_SNAPSHOT["news_status"] = news_text
    if blocked:
        logger.info("Tạm dừng vào lệnh: %s", news_text)
        return

    d1_candles, h1_candles = await asyncio.gather(
        get_ohlc(SYMBOL, TF_TREND, count=max(EMA_D1_LEN + 20, 80)),
        get_ohlc(SYMBOL, TF_ENTRY, count=max(EMA_H1_LEN, RSI_LEN, ATR_LEN, LOOKBACK_H1) + 40),
    )

    macro_dir, macro_text = await get_macro_view()
    LATEST_SNAPSHOT["macro_summary"] = macro_text

    sig = build_h1_signal(d1_candles, h1_candles, macro_dir, macro_text)
    if not sig:
        return

    state = read_state()
    sent_keys: List[str] = state.setdefault("sent_keys", [])
    sig_key = f"{sig['side']}@{sig['time']}"
    if sig_key in sent_keys:
        return

    await send_tg(format_tg_signal(sig, news_text))
    sent_keys.append(sig_key)
    state["sent_keys"] = sent_keys[-100:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_state(state)
    logger.info("Đã gửi tín hiệu Telegram %s", sig_key)


async def scanner_loop() -> None:
    while True:
        try:
            await scan_once()
        except Exception as exc:
            LATEST_SNAPSHOT["last_error"] = str(exc)
            logger.error("Lỗi quét D1/H1: %s", exc)
        await asyncio.sleep(LOOP_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info(
        "Bot sẵn sàng | %s | D1 -> H1 | SL=%.1f | TP=%.1f (R:R 1:2)",
        SYMBOL,
        SL_DISTANCE,
        TP_DISTANCE,
    )
    asyncio.create_task(scanner_loop())


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "symbol": SYMBOL,
        "trend_tf": TF_TREND,
        "entry_tf": TF_ENTRY,
        "sl": SL_DISTANCE,
        "tp": TP_DISTANCE,
        "rr": "1:2",
    }


@app.get("/status")
async def status() -> Dict[str, Any]:
    return LATEST_SNAPSHOT


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
