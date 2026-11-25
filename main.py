import asyncio
import json
import logging
import os
from datetime import datetime, timezone
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
logger = logging.getLogger("xauusd-d1-h1-bot")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "state.json")

# Cấu hình chiến lược cố định: D1 Trend Filter + H1 Entry, SL 6.0, TP 12.0 (R:R 1:2)
SYMBOL = os.getenv("SYMBOL", "XAU/USD")
TREND_TF = "1d"
ENTRY_TF = "1h"
SL_POINTS = float(os.getenv("SL_POINTS", "6.0"))
TP_POINTS = float(os.getenv("TP_POINTS", os.getenv("TP2_POINTS", "12.0")))

D1_EMA_PERIOD = int(os.getenv("D1_EMA_PERIOD", "20"))
H1_EMA_PERIOD = int(os.getenv("H1_EMA_PERIOD", "20"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "5"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

app = FastAPI(title="XAUUSD D1-H1 Signal Bot")


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Không đọc được state.json, tạo trạng thái mới: %s", exc)
    return {"last_signals": {}}


def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Lỗi ghi state.json: %s", exc)


def calc_ema(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period or period <= 0:
        return [None] * len(values)
    result: List[Optional[float]] = [None] * len(values)
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    multiplier = 2.0 / (period + 1)
    ema_prev = sma
    for idx in range(period, len(values)):
        ema_val = (values[idx] - ema_prev) * multiplier + ema_prev
        result[idx] = ema_val
        ema_prev = ema_val
    return result


async def fetch_candles(interval: str, outputsize: int = 120) -> List[Dict[str, Any]]:
    """
    Lấy nến OHLCV cho XAU/USD (mặc định qua Twelve Data nếu có API key).
    Trả về danh sách sắp xếp từ cũ -> mới (nến cuối cùng là nến đang chạy hoặc vừa đóng).
    """
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Thiếu TWELVE_DATA_API_KEY trong file .env")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "timezone": "UTC",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()

    if "values" not in payload or not isinstance(payload["values"], list):
        raise RuntimeError(f"Phản hồi dữ liệu nến không hợp lệ ({interval}): {payload}")

    candles: List[Dict[str, Any]] = []
    for item in reversed(payload["values"]):
        candles.append(
            {
                "datetime": str(item["datetime"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }
        )
    return candles


def detect_d1_trend(d1_candles: List[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Lọc xu hướng chính trên khung D1 bằng EMA(D1_EMA_PERIOD) và cấu trúc đóng nến D1 gần nhất.
    Trả về ("BUY", lý_do), ("SELL", lý_do), hoặc ("NEUTRAL", lý_do).
    """
    if len(d1_candles) < D1_EMA_PERIOD + 2:
        return "NEUTRAL", "Không đủ dữ liệu nến D1"

    closes = [c["close"] for c in d1_candles]
    ema_vals = calc_ema(closes, D1_EMA_PERIOD)

    # Dùng nến D1 đã đóng gần nhất (-2) để tránh nhiễu nến ngày hiện tại đang chạy
    last_closed = d1_candles[-2]
    ema_closed = ema_vals[-2]
    if ema_closed is None:
        return "NEUTRAL", "Chưa đủ kỳ tính EMA D1"

    if last_closed["close"] > ema_closed and last_closed["close"] >= last_closed["open"]:
        return "BUY", f"D1 đóng cửa ({last_closed['close']:.2f}) > EMA{D1_EMA_PERIOD} ({ema_closed:.2f})"

    if last_closed["close"] < ema_closed and last_closed["close"] <= last_closed["open"]:
        return "SELL", f"D1 đóng cửa ({last_closed['close']:.2f}) < EMA{D1_EMA_PERIOD} ({ema_closed:.2f})"

    return "NEUTRAL", f"D1 giằng co quanh EMA{D1_EMA_PERIOD} ({ema_closed:.2f})"


def evaluate_d1_h1_signal(
    d1_candles: List[Dict[str, Any]],
    h1_candles: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Chỉ vào lệnh theo chiều xu hướng D1 khi có nến H1 vừa đóng xác nhận phá vỡ vùng đỉnh/đáy gần nhất
    và đồng thuận với EMA H1. SL cố định 6.0, TP cố định 12.0 (R:R 1:2).
    """
    d1_bias, d1_reason = detect_d1_trend(d1_candles)
    if d1_bias == "NEUTRAL":
        return None

    min_len = max(H1_EMA_PERIOD, BREAKOUT_LOOKBACK) + 3
    if len(h1_candles) < min_len:
        return None

    # Nến H1 vừa đóng cửa là phần tử -2 (-1 là nến H1 hiện tại chưa đóng)
    closed_h1 = h1_candles[-2]
    window = h1_candles[-(BREAKOUT_LOOKBACK + 2):-2]
    if len(window) < BREAKOUT_LOOKBACK:
        return None

    h1_closes = [c["close"] for c in h1_candles]
    h1_ema_list = calc_ema(h1_closes, H1_EMA_PERIOD)
    h1_ema = h1_ema_list[-2]
    if h1_ema is None:
        return None

    recent_high = max(c["high"] for c in window)
    recent_low = min(c["low"] for c in window)

    entry = round(closed_h1["close"], 2)

    if d1_bias == "BUY":
        if closed_h1["close"] > recent_high and closed_h1["close"] > h1_ema:
            sl = round(entry - SL_POINTS, 2)
            tp = round(entry + TP_POINTS, 2)
            return {
                "tf": "H1",
                "filter_tf": "D1",
                "side": "BUY",
                "candle_time": closed_h1["datetime"],
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "rr": "1:2",
                "reason": f"{d1_reason} | H1 phá đỉnh {BREAKOUT_LOOKBACK} nến ({recent_high:.2f}) & > EMA{H1_EMA_PERIOD} ({h1_ema:.2f})",
            }

    if d1_bias == "SELL":
        if closed_h1["close"] < recent_low and closed_h1["close"] < h1_ema:
            sl = round(entry + SL_POINTS, 2)
            tp = round(entry - TP_POINTS, 2)
            return {
                "tf": "H1",
                "filter_tf": "D1",
                "side": "SELL",
                "candle_time": closed_h1["datetime"],
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "rr": "1:2",
                "reason": f"{d1_reason} | H1 phá đáy {BREAKOUT_LOOKBACK} nến ({recent_low:.2f}) & < EMA{H1_EMA_PERIOD} ({h1_ema:.2f})",
            }

    return None


async def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID, bỏ qua gửi tin.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


def format_signal_message(sig: Dict[str, Any]) -> str:
    icon = "🟢" if sig["side"] == "BUY" else "🔴"
    return (
        f"{icon} <b>{SYMBOL} | [D1 -> H1] {sig['side']}</b>\n"
        f"• Nến H1 đóng: <code>{sig['candle_time']}</code>\n"
        f"• Entry: <code>{sig['entry']:.2f}</code>\n"
        f"• SL (1R = {SL_POINTS:.1f}): <code>{sig['sl']:.2f}</code>\n"
        f"• TP (2R = {TP_POINTS:.1f} | R:R {sig['rr']}): <code>{sig['tp']:.2f}</code>\n"
        f"• Lý do: {sig['reason']}"
    )


async def run_scan_cycle() -> None:
    d1_candles, h1_candles = await asyncio.gather(
        fetch_candles(TREND_TF, outputsize=max(D1_EMA_PERIOD + 20, 80)),
        fetch_candles(ENTRY_TF, outputsize=max(H1_EMA_PERIOD + BREAKOUT_LOOKBACK + 20, 120)),
    )
    sig = evaluate_d1_h1_signal(d1_candles, h1_candles)
    if not sig:
        return

    state = load_state()
    last_signals = state.setdefault("last_signals", {})
    signal_key = f"{sig['side']}_{sig['candle_time']}"

    if last_signals.get("h1_key") == signal_key:
        return

    await send_telegram_message(format_signal_message(sig))
    last_signals["h1_key"] = signal_key
    last_signals["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    logger.info("Đã gửi tín hiệu [D1 -> H1] %s tại %s", sig["side"], sig["candle_time"])


async def background_scanner() -> None:
    while True:
        try:
            await run_scan_cycle()
        except Exception as exc:
            logger.error("Lỗi trong chu kỳ quét D1/H1: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SEC)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info(
        "Khởi động bot %s | Filter=%s | Entry=%s | SL=%.1f | TP=%.1f (R:R 1:2)",
        SYMBOL,
        TREND_TF.upper(),
        ENTRY_TF.upper(),
        SL_POINTS,
        TP_POINTS,
    )
    asyncio.create_task(background_scanner())


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "symbol": SYMBOL,
        "trend_timeframe": TREND_TF,
        "entry_timeframe": ENTRY_TF,
        "sl_points": SL_POINTS,
        "tp_points": TP_POINTS,
        "rr": "1:2",
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
