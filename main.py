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
logger = logging.getLogger("xauusd-auto-scanner")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "state.json")

# Khung thời gian & target động theo Hỗ trợ/Kháng cự (R:R 1:2)
SYMBOL = os.getenv("SYMBOL", "XAU/USD")
TF_TREND = "1day"
TF_ENTRY = "1h"
RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
SR_LOOKBACK = int(os.getenv("SR_LOOKBACK", "20"))
SR_BUFFER = float(os.getenv("SR_BUFFER", "0.5"))
MIN_SL_POINTS = float(os.getenv("MIN_SL_POINTS", "2.5"))
MAX_SL_POINTS = float(os.getenv("MAX_SL_POINTS", "30.0"))

# Tham số MACD & phá vỡ H1
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "5"))

# Lọc tin CPI & Non-Farm từ ForexFactory
NEWS_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "true").lower() == "true"
NEWS_WINDOW_MIN = int(os.getenv("NEWS_WINDOW_MINUTES", "60"))
BLOCK_ON_CPI_NFP = os.getenv("BLOCK_ON_CPI_NFP", "true").lower() == "true"
FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

# Vĩ mô DXY & US10Y
MACRO_ENABLED = os.getenv("MACRO_FILTER_ENABLED", "true").lower() == "true"
DXY_TICKER = os.getenv("DXY_SYMBOL", "DXY")
US10Y_TICKER = os.getenv("US10Y_SYMBOL", "US10Y")

LOOP_SECONDS = int(os.getenv("POLL_INTERVAL_SEC", "120"))

TD_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

app = FastAPI(title="XAUUSD Auto Scanner (D1/H1 + MACD + S/R + DXY/US10Y + CPI/NFP)")

LATEST_STATUS: Dict[str, Any] = {
    "last_check_utc": None,
    "last_closed_h1": None,
    "xau_d1_h1": "Chưa có dữ liệu",
    "macd": "Chưa có dữ liệu",
    "dxy_d1_h1": "Chưa có dữ liệu",
    "us10y_d1_h1": "Chưa có dữ liệu",
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


def series_macd(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    ema_fast = series_ema(closes, fast)
    ema_slow = series_ema(closes, slow)
    n = len(closes)
    macd_line: List[Optional[float]] = [None] * n
    valid_macd: List[float] = []
    valid_indices: List[int] = []

    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            val = ema_fast[i] - ema_slow[i]
            macd_line[i] = val
            valid_macd.append(val)
            valid_indices.append(i)

    signal_line: List[Optional[float]] = [None] * n
    hist_line: List[Optional[float]] = [None] * n
    if len(valid_macd) >= signal:
        sig_vals = series_ema(valid_macd, signal)
        for idx_pos, bar_idx in enumerate(valid_indices):
            s_val = sig_vals[idx_pos]
            if s_val is not None and macd_line[bar_idx] is not None:
                signal_line[bar_idx] = s_val
                hist_line[bar_idx] = macd_line[bar_idx] - s_val

    return macd_line, signal_line, hist_line


async def fetch_ohlc(symbol: str, interval: str, count: int = 120) -> List[Dict[str, Any]]:
    if not TD_API_KEY:
        raise RuntimeError("Thiếu TWELVE_DATA_API_KEY trong Environment trên Render")

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

    values = body.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"Lỗi dữ liệu {symbol} ({interval}): {body}")

    return [
        {
            "time": str(item["datetime"]),
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
        }
        for item in reversed(values)
    ]


def is_cpi_or_nfp(title: str) -> Optional[str]:
    t = title.lower()
    if "cpi" in t or "consumer price index" in t:
        return "CPI"
    if "non-farm" in t or "nonfarm" in t or "nfp" in t:
        return "Non-Farm (NFP)"
    return None


async def check_cpi_nfp_forexfactory() -> Tuple[bool, str]:
    if not NEWS_ENABLED:
        return False, "Tắt lọc tin CPI/NFP"

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


def summarize_d1_h1(d1_candles: List[Dict[str, Any]], h1_candles: List[Dict[str, Any]]) -> Tuple[str, str]:
    d1_str = "N/A"
    d1_dir = "NONE"
    if len(d1_candles) >= 2:
        d1_prev = d1_candles[-2]
        if d1_prev["close"] > d1_prev["open"]:
            d1_dir = "UP"
            d1_str = f"D1 Tăng ({d1_prev['close']:.2f})"
        elif d1_prev["close"] < d1_prev["open"]:
            d1_dir = "DOWN"
            d1_str = f"D1 Giảm ({d1_prev['close']:.2f})"
        else:
            d1_str = f"D1 Đi ngang ({d1_prev['close']:.2f})"

    h1_str = "N/A"
    if len(h1_candles) >= 2:
        h1_prev = h1_candles[-2]
        move = "H1 Tăng" if h1_prev["close"] >= h1_prev["open"] else "H1 Giảm"
        h1_str = f"{move} (C:{h1_prev['close']:.2f}, H:{h1_prev['high']:.2f}, L:{h1_prev['low']:.2f})"

    return f"{d1_str} | {h1_str}", d1_dir


async def get_macro_d1_h1(label: str, ticker: str) -> Tuple[str, str]:
    if not MACRO_ENABLED:
        return "Tắt", "NEUTRAL"
    try:
        d1_c, h1_c = await asyncio.gather(
            fetch_ohlc(ticker, TF_TREND, count=10),
            fetch_ohlc(ticker, TF_ENTRY, count=10),
        )
        d1_p = d1_c[-2]
        h1_p = h1_c[-2]
        d1_tag = "Tăng" if d1_p["close"] >= d1_p["open"] else "Giảm"
        h1_tag = "Tăng" if h1_p["close"] >= h1_p["open"] else "Giảm"
        text = f"D1 {d1_tag} ({d1_p['close']:.2f}) | H1 {h1_tag} ({h1_p['close']:.2f})"
        if d1_p["close"] > d1_p["open"] and h1_p["close"] > h1_p["open"]:
            return text, "UP"
        if d1_p["close"] < d1_p["open"] and h1_p["close"] < h1_p["open"]:
            return text, "DOWN"
        return text, "NEUTRAL"
    except Exception as exc:
        logger.warning("Lỗi lấy %s (%s): %s", label, ticker, exc)
        return "Không lấy được dữ liệu", "NEUTRAL"


def calc_sr_sl_tp(
    side: str,
    entry: float,
    support: float,
    resistance: float,
) -> Tuple[float, float, float, float, str]:
    if side == "BUY":
        raw_dist = max(abs(entry - support) + SR_BUFFER, 0.5)
        sl_dist = max(MIN_SL_POINTS, min(MAX_SL_POINTS, raw_dist))
        tp_dist = sl_dist * RR_RATIO
        return (
            round(entry - sl_dist, 2),
            round(entry + tp_dist, 2),
            round(sl_dist, 2),
            round(tp_dist, 2),
            f"Hỗ trợ {support:.2f}",
        )

    raw_dist = max(abs(resistance - entry) + SR_BUFFER, 0.5)
    sl_dist = max(MIN_SL_POINTS, min(MAX_SL_POINTS, raw_dist))
    tp_dist = sl_dist * RR_RATIO
    return (
        round(entry + sl_dist, 2),
        round(entry - tp_dist, 2),
        round(sl_dist, 2),
        round(tp_dist, 2),
        f"Kháng cự {resistance:.2f}",
    )


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


async def scan_once() -> None:
    LATEST_STATUS["last_check_utc"] = datetime.now(timezone.utc).isoformat()
    LATEST_STATUS["last_error"] = None

    blocked, news_status = await check_cpi_nfp_forexfactory()
    LATEST_STATUS["cpi_nfp_status"] = news_status

    d1_candles, h1_candles = await asyncio.gather(
        fetch_ohlc(SYMBOL, TF_TREND, count=60),
        fetch_ohlc(SYMBOL, TF_ENTRY, count=max(MACD_SLOW + MACD_SIGNAL + SR_LOOKBACK + BREAKOUT_LOOKBACK + 20, 100)),
    )

    xau_summary, xau_d1_dir = summarize_d1_h1(d1_candles, h1_candles)
    LATEST_STATUS["xau_d1_h1"] = xau_summary

    closed_h1 = h1_candles[-2]
    prev_h1 = h1_candles[-3]
    LATEST_STATUS["last_closed_h1"] = closed_h1["time"]

    h1_closes = [c["close"] for c in h1_candles]
    macd_line, signal_line, hist_line = series_macd(h1_closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

    m_now, s_now, h_now = macd_line[-2], signal_line[-2], hist_line[-2]
    m_prev, s_prev = macd_line[-3], signal_line[-3]
    if None in (m_now, s_now, h_now, m_prev, s_prev):
        return

    macd_desc = f"MACD={m_now:.2f} | Signal={s_now:.2f} | Hist={h_now:.2f}"
    LATEST_STATUS["macd"] = macd_desc

    dxy_text, dxy_dir = await get_macro_d1_h1("DXY", DXY_TICKER)
    us10y_text, us10y_dir = await get_macro_d1_h1("US10Y", US10Y_TICKER)
    LATEST_STATUS["dxy_d1_h1"] = dxy_text
    LATEST_STATUS["us10y_d1_h1"] = us10y_text

    if blocked:
        logger.info("Đang chặn lệnh do lịch tin: %s", news_status)
        return

    sr_slice = h1_candles[-(SR_LOOKBACK + 2) : -2]
    brk_slice = h1_candles[-(BREAKOUT_LOOKBACK + 2) : -2]
    if len(sr_slice) < SR_LOOKBACK or len(brk_slice) < BREAKOUT_LOOKBACK:
        return

    support = min(c["low"] for c in sr_slice)
    resistance = max(c["high"] for c in sr_slice)
    brk_high = max(c["high"] for c in brk_slice)
    brk_low = min(c["low"] for c in brk_slice)

    macd_bull_cross = (m_prev <= s_prev) and (m_now > s_now)
    macd_bear_cross = (m_prev >= s_prev) and (m_now < s_now)

    entry = round(closed_h1["close"], 2)
    side: Optional[str] = None

    # D1 tăng + MACD cắt lên Signal + phá vỡ vùng đỉnh H1 gần nhất + DXY/US10Y không ép tăng mạnh đồng thời
    if (
        xau_d1_dir == "UP"
        and macd_bull_cross
        and closed_h1["close"] > brk_high
        and not (dxy_dir == "UP" and us10y_dir == "UP")
    ):
        side = "BUY"
    # D1 giảm + MACD cắt xuống Signal + phá vỡ vùng đáy H1 gần nhất + DXY/US10Y không ép giảm mạnh đồng thời
    elif (
        xau_d1_dir == "DOWN"
        and macd_bear_cross
        and closed_h1["close"] < brk_low
        and not (dxy_dir == "DOWN" and us10y_dir == "DOWN")
    ):
        side = "SELL"

    if not side:
        return

    sl, tp, sl_dist, tp_dist, sr_label = calc_sr_sl_tp(side, entry, support, resistance)
    sig_key = f"{SYMBOL}:{side}:{closed_h1['time']}"

    state = read_state()
    sent_keys: List[str] = state.setdefault("sent_keys", [])
    if sig_key in sent_keys:
        return

    badge = "🟢" if side == "BUY" else "🔴"
    msg = (
        f"{badge} <b>{SYMBOL} | {side} (R:R 1:{RR_RATIO:g})</b>\n"
        f"• Nến H1 đóng: <code>{closed_h1['time']}</code>\n"
        f"• <b>Entry:</b> <code>{entry:.2f}</code>\n"
        f"• <b>SL:</b> <code>{sl:.2f}</code> (<code>{sl_dist:.2f}</code> giá | {sr_label})\n"
        f"• <b>TP:</b> <code>{tp:.2f}</code> (<code>{tp_dist:.2f}</code> giá)\n"
        f"• <b>XAU/USD (D1/H1):</b> {xau_summary}\n"
        f"• <b>MACD ({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}):</b> {macd_desc}\n"
        f"• <b>DXY (D1/H1):</b> {dxy_text}\n"
        f"• <b>US10Y (D1/H1):</b> {us10y_text}\n"
        f"• <b>CPI / Non-Farm (FF):</b> {news_status}"
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
    logger.info("Đã gửi tín hiệu Telegram: %s", sig_key)


async def scanner_loop() -> None:
    while True:
        try:
            await scan_once()
        except Exception as exc:
            LATEST_STATUS["last_error"] = str(exc)
            logger.error("Lỗi chu kỳ quét: %s", exc)
        await asyncio.sleep(LOOP_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Khởi động bot tự quét %s | R:R 1:%g", SYMBOL, RR_RATIO)
    asyncio.create_task(scanner_loop())


@app.get("/")
async def root() -> Dict[str, Any]:
    return {"service": "huy-xauusd-bot", "mode": "auto_scanner", "status": "live"}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "mode": "auto_scanner", "rr": f"1:{RR_RATIO:g}"}


@app.get("/status")
async def status() -> Dict[str, Any]:
    return LATEST_STATUS


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
