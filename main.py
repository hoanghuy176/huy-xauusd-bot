import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from google import genai
except ImportError:
    genai = None

app = FastAPI(title="XAUUSD H1/30m Breakout Bot")

VN_TZ = timezone(timedelta(hours=7))

# --- CẤU HÌNH ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()

GOLD_SYMBOL = os.getenv("GOLD_SYMBOL", "GC=F").strip()
DXY_SYMBOL = os.getenv("DXY_SYMBOL", "DX-Y.NYB").strip()
US10Y_SYMBOL = os.getenv("US10Y_SYMBOL", "^TNX").strip()

TIMEFRAMES = [
    tf.strip()
    for tf in os.getenv("TIMEFRAMES", "1h,30m").split(",")
    if tf.strip() in {"1h", "30m"}
] or ["1h", "30m"]

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "3600"))

# Tỷ lệ chốt lời TP1 / TP2 / TP3
RR_RATIO = float(os.getenv("RR_RATIO", "1.3"))
TP2_RATIO = float(os.getenv("TP2_RATIO", "2.0"))
TP3_RATIO = float(os.getenv("TP3_RATIO", "3.0"))

BREAK_BUFFER = float(os.getenv("BREAK_BUFFER", "2.0"))
SR_WINDOW = int(os.getenv("SR_WINDOW", "12"))
USE_MACD_FILTER = os.getenv("USE_MACD_FILTER", "true").lower() == "true"

# Quản lý vốn & khối lượng (vốn $50 đánh 0.01 lot)
ACCOUNT_CAPITAL_USD = float(os.getenv("ACCOUNT_CAPITAL_USD", "50"))
LOT_SIZE = float(os.getenv("LOT_SIZE", "0.01"))
MAX_RISK_PER_TRADE_USD = float(os.getenv("MAX_RISK_PER_TRADE_USD", "3.0"))
MAX_SL_POINTS = float(os.getenv("MAX_SL_POINTS", "3.0"))

QUIET_HOURS_VN = {
    int(h.strip())
    for h in os.getenv("QUIET_HOURS_VN", "2,3,4,5").split(",")
    if h.strip().isdigit()
}

# Lọc tin ForexFactory: Non-Farm & CPI
FF_CALENDAR_URL = os.getenv("FF_CALENDAR_URL", "https://www.forexfactory.com/calendar").strip()
NEWS_WINDOW_MINUTES = int(os.getenv("NEWS_WINDOW_MINUTES", "60"))
BLOCK_ON_NFP_CPI = os.getenv("BLOCK_ON_NFP_CPI", "false").lower() == "true"
NEWS_CACHE_TTL = int(os.getenv("NEWS_CACHE_TTL", "1800"))

STATE_FILE = os.getenv("STATE_FILE", "/tmp/state.json").strip()

state: Dict[str, Any] = {
    "last_signals": {},
    "last_update_id": 0,
    "news_cache": {
        "fetched_at": 0.0,
        "events": [],
    },
}


def load_state() -> None:
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    state.update(data)
        except Exception as e:
            print(f"[STATE] Lỗi đọc {STATE_FILE}: {e}")


def save_state() -> None:
    try:
        folder = os.path.dirname(STATE_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[STATE] Lỗi lưu {STATE_FILE}: {e}")


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


def is_quiet_hour() -> bool:
    return now_vn().hour in QUIET_HOURS_VN


def _extract_close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(0):
            s = df["Close"]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            return s.dropna().astype(float)
    if "Close" in df.columns:
        return df["Close"].dropna().astype(float)
    return pd.Series(dtype=float)


def calc_macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float]:
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(hist.iloc[-1])


def get_market_trend(symbol: str, interval: str, period: str) -> Dict[str, Any]:
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
        closes = _extract_close_series(df)
        if len(closes) < 2:
            return {"symbol": symbol, "tf": interval, "price": None, "change_pct": None, "dir": "N/A"}

        last_val = float(closes.iloc[-1])
        prev_val = float(closes.iloc[-2])
        chg_pct = ((last_val - prev_val) / prev_val * 100.0) if prev_val else 0.0

        if chg_pct > 0.02:
            direction = "TĂNG"
        elif chg_pct < -0.02:
            direction = "GIẢM"
        else:
            direction = "ĐI NGANG"

        return {
            "symbol": symbol,
            "tf": interval,
            "price": round(last_val, 3),
            "change_pct": round(chg_pct, 2),
            "dir": direction,
        }
    except Exception as e:
        print(f"[YF] Lỗi lấy {symbol} ({interval}): {e}")
        return {"symbol": symbol, "tf": interval, "price": None, "change_pct": None, "dir": "N/A"}


def get_intermarket_snapshot() -> Dict[str, Dict[str, Any]]:
    return {
        "DXY_D1": get_market_trend(DXY_SYMBOL, "1d", "15d"),
        "DXY_H1": get_market_trend(DXY_SYMBOL, "1h", "5d"),
        "US10Y_D1": get_market_trend(US10Y_SYMBOL, "1d", "15d"),
        "US10Y_H1": get_market_trend(US10Y_SYMBOL, "1h", "5d"),
    }


def _is_target_nfp_or_cpi(title: str) -> Optional[str]:
    t = (title or "").lower()
    if "non-farm" in t or "nonfarm" in t or "nfp" in t:
        return "NON-FARM"
    if "cpi" in t or "consumer price index" in t:
        return "CPI"
    return None


def _parse_event_dt_vn(raw_dt: str) -> Optional[datetime]:
    if not raw_dt:
        return None
    s = str(raw_dt).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VN_TZ)
        return dt.astimezone(VN_TZ)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d-%Y %I:%M%p", "%b %d, %Y %I:%M%p"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=VN_TZ)
            return dt
        except Exception:
            continue
    return None


def fetch_forexfactory_nfp_cpi() -> Dict[str, Any]:
    cache = state.get("news_cache", {})
    fetched_at = float(cache.get("fetched_at", 0.0))
    if time.time() - fetched_at < NEWS_CACHE_TTL and isinstance(cache.get("events"), list):
        matched = cache["events"]
    else:
        matched = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
        }
        try:
            resp = requests.get(FF_CALENDAR_URL, headers=headers, timeout=12)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()

            if "json" in content_type or resp.text.strip().startswith(("[", "{")):
                payload = resp.json()
                rows = payload if isinstance(payload, list) else payload.get("events", payload.get("data", []))
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    currency = str(item.get("currency", item.get("country", ""))).upper().strip()
                    title = str(item.get("title", item.get("event", item.get("name", "")))).strip()
                    tag = _is_target_nfp_or_cpi(title)
                    if currency == "USD" and tag:
                        dt_vn = _parse_event_dt_vn(
                            str(item.get("date", item.get("datetime", item.get("time", ""))))
                        )
                        matched.append({
                            "tag": tag,
                            "title": title,
                            "currency": currency,
                            "time_vn": dt_vn.isoformat() if dt_vn else str(item.get("time", "")),
                            "impact": str(item.get("impact", "")),
                            "forecast": str(item.get("forecast", "")),
                            "previous": str(item.get("previous", "")),
                        })
            else:
                print("[NEWS] Nguồn ForexFactory trả về HTML/bị chặn bot.")
        except Exception as e:
            print(f"[NEWS] Không lấy được lịch ForexFactory ({FF_CALENDAR_URL}): {e}")

        state["news_cache"] = {
            "fetched_at": time.time(),
            "events": matched,
        }
        save_state()

    current = now_vn()
    nearby: List[Dict[str, Any]] = []
    upcoming_today: List[Dict[str, Any]] = []

    for ev in matched:
        dt_str = ev.get("time_vn", "")
        dt_obj = _parse_event_dt_vn(dt_str)
        if not dt_obj:
            continue
        diff_mins = (dt_obj - current).total_seconds() / 60.0
        if abs(diff_mins) <= NEWS_WINDOW_MINUTES:
            ev_copy = dict(ev)
            ev_copy["diff_mins"] = round(diff_mins, 1)
            nearby.append(ev_copy)
        if dt_obj.date() == current.date() and diff_mins >= -15:
            upcoming_today.append(ev)

    return {
        "nearby": nearby,
        "today_nfp_cpi": upcoming_today,
        "has_alert": len(nearby) > 0,
    }


def analyze_timeframe(tf: str, intermarket: Dict[str, Dict[str, Any]], news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    period = "10d" if tf == "1h" else "5d"
    df = yf.download(GOLD_SYMBOL, period=period, interval=tf, progress=False, auto_adjust=False)
    closes = _extract_close_series(df)

    if len(closes) < SR_WINDOW + 5:
        return None

    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2])

    window_slice = closes.iloc[-(SR_WINDOW + 1):-1]
    resistance = float(window_slice.max())
    support = float(window_slice.min())

    macd_val, sig_val, hist_val = calc_macd(closes)

    signal_type: Optional[str] = None
    if last_close > resistance + BREAK_BUFFER and prev_close <= resistance + BREAK_BUFFER:
        signal_type = "BREAK_UP"
    elif last_close < support - BREAK_BUFFER and prev_close >= support - BREAK_BUFFER:
        signal_type = "BREAK_DOWN"

    if not signal_type:
        return None

    if USE_MACD_FILTER:
        if signal_type == "BREAK_UP" and hist_val <= 0:
            return None
        if signal_type == "BREAK_DOWN" and hist_val >= 0:
            return None

    max_risk_pts = max(MAX_SL_POINTS, 0.5)

    if signal_type == "BREAK_UP":
        entry = round(last_close, 2)
        raw_sl = min(support, entry - max(BREAK_BUFFER * 1.2, 1.5))
        risk_pts = min(max(entry - raw_sl, 1.0), max_risk_pts)
        sl = round(entry - risk_pts, 2)
        tp1 = round(entry + risk_pts * RR_RATIO, 2)
        tp2 = round(entry + risk_pts * TP2_RATIO, 2)
        tp3 = round(entry + risk_pts * TP3_RATIO, 2)
        side_label = "MUA (BREAK_UP)"
    else:
        entry = round(last_close, 2)
        raw_sl = max(resistance, entry + max(BREAK_BUFFER * 1.2, 1.5))
        risk_pts = min(max(raw_sl - entry, 1.0), max_risk_pts)
        sl = round(entry + risk_pts, 2)
        tp1 = round(entry - risk_pts * RR_RATIO, 2)
        tp2 = round(entry - risk_pts * TP2_RATIO, 2)
        tp3 = round(entry - risk_pts * TP3_RATIO, 2)
        side_label = "BÁN (BREAK_DOWN)"

    est_loss_usd = round(risk_pts * 100.0 * LOT_SIZE, 2)

    return {
        "tf": tf,
        "signal_type": signal_type,
        "side_label": side_label,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_pts": round(risk_pts, 2),
        "est_loss_usd": est_loss_usd,
        "resistance": round(resistance, 2),
        "support": round(support, 2),
        "macd": round(macd_val, 3),
        "macd_signal": round(sig_val, 3),
        "macd_hist": round(hist_val, 3),
        "intermarket": intermarket,
        "news": news,
    }


def _fmt_im(item: Dict[str, Any]) -> str:
    if item.get("price") is None:
        return "N/A"
    return f"{item['price']} ({item['dir']} {item['change_pct']:+.2f}%)"


def build_fallback_message(sig: Dict[str, Any]) -> str:
    im = sig["intermarket"]
    news = sig["news"]

    lines = [
        f"🚨 *XAU/USD [{sig['tf'].upper()}] — {sig['side_label']}*",
        "",
        f"• *Entry:* `{sig['entry']}`",
        f"• *SL:* `{sig['sl']}` (~`{sig['risk_pts']}` giá)",
        f"• *TP1 ({RR_RATIO}R):* `{sig['tp1']}`",
        f"• *TP2 ({TP2_RATIO}R):* `{sig['tp2']}`",
        f"• *TP3 ({TP3_RATIO}R):* `{sig['tp3']}`",
        "",
        f"• *Vốn:* `${ACCOUNT_CAPITAL_USD:g}`",
        f"• *Lot:* `{LOT_SIZE}`",
        f"• *Ước lỗ SL:* `~${sig['est_loss_usd']}`",
        "",
        f"• *Kháng cự:* `{sig['resistance']}`",
        f"• *Hỗ trợ:* `{sig['support']}`",
        f"• *MACD (12,26,9):* `{sig['macd']}`",
        f"• *MACD Signal:* `{sig['macd_signal']}`",
        f"• *MACD Hist:* `{sig['macd_hist']}`",
        "",
        f"• *DXY D1:* `{_fmt_im(im['DXY_D1'])}`",
        f"• *DXY H1:* `{_fmt_im(im['DXY_H1'])}`",
        f"• *US10Y D1:* `{_fmt_im(im['US10Y_D1'])}`",
        f"• *US10Y H1:* `{_fmt_im(im['US10Y_H1'])}`",
    ]

    if sig["est_loss_usd"] > MAX_RISK_PER_TRADE_USD:
        lines.append(f"⚠️ *Cảnh báo:* Ước lỗ `~${sig['est_loss_usd']}` vượt trần `${MAX_RISK_PER_TRADE_USD:g}`")

    if news.get("nearby"):
        lines.append("")
        lines.append(f"⚠️ *Tin NFP/CPI sát giờ (±{NEWS_WINDOW_MINUTES}p):*")
        for e in news["nearby"]:
            lines.append(f"  - {e['tag']}: {e['title']} (`{e['diff_mins']:+.0f}` phút)")
    elif news.get("today_nfp_cpi"):
        lines.append("")
        lines.append("🗓 *Tin NFP/CPI hôm nay:*")
        for e in news["today_nfp_cpi"]:
            lines.append(f"  - {e['tag']} lúc `{e['time_vn'][11:16]}`")
    else:
        lines.append("")
        lines.append("🗓 *Tin NFP/CPI:* Không có tin Non-Farm/CPI sát giờ.")

    lines.append("")
    lines.append(f"🕒 `{now_vn().strftime('%Y-%m-%d %H:%M:%S')} GMT+7`")
    return "\n".join(lines)


def generate_ai_commentary(sig: Dict[str, Any]) -> str:
    fallback = build_fallback_message(sig)
    if not GEMINI_API_KEY or genai is None:
        return fallback

    im = sig["intermarket"]
    news_info = "Có tin NFP/CPI sát giờ" if sig["news"].get("nearby") else "Không có tin NFP/CPI sát giờ"

    prompt = (
        "Bạn là trợ lý phân tích kỹ thuật XAU/USD. Hãy viết nhận định ngắn gọn (khoảng 60-85 từ, tiếng Việt, "
        "không thêm dữ liệu ngoài số liệu được cung cấp):\n"
        f"- Khung: {sig['tf']} | Tín hiệu: {sig['side_label']} | Entry: {sig['entry']} | SL: {sig['sl']} "
        f"| TP1: {sig['tp1']} | TP2: {sig['tp2']} | TP3: {sig['tp3']}\n"
        f"- MACD Hist: {sig['macd_hist']}\n"
        f"- DXY D1: {_fmt_im(im['DXY_D1'])}, DXY H1: {_fmt_im(im['DXY_H1'])}\n"
        f"- US10Y D1: {_fmt_im(im['US10Y_D1'])}, US10Y H1: {_fmt_im(im['US10Y_H1'])}\n"
        f"- Tin tức: {news_info}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        ai_text = (resp.text or "").strip()
        if not ai_text:
            return fallback
        return f"{fallback}\n\n🤖 *Nhận định nhanh:*\n{ai_text}"
    except Exception as e:
        print(f"[GEMINI] Lỗi gọi model {GEMINI_MODEL}: {e}")
        return fallback


def send_telegram(text: str, chat_id: Optional[str] = None) -> None:
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target_chat:
        print("[TG] Thiếu TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": target_chat,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=12,
        )
        if not r.ok:
            requests.post(
                url,
                json={"chat_id": target_chat, "text": text.replace("*", "").replace("`", "")},
                timeout=12,
            )
    except Exception as e:
        print(f"[TG] Lỗi gửi tin nhắn: {e}")


def build_status_message() -> str:
    im = get_intermarket_snapshot()
    news = fetch_forexfactory_nfp_cpi()
    gold_h1 = get_market_trend(GOLD_SYMBOL, "1h", "3d")
    gold_30m = get_market_trend(GOLD_SYMBOL, "30m", "2d")

    lines = [
        f"📊 *CẬP NHẬT XAU/USD ({now_vn().strftime('%H:%M %d/%m/%Y')} GMT+7)*",
        "",
        f"• *XAU/USD H1:* `{_fmt_im(gold_h1)}`",
        f"• *XAU/USD 30m:* `{_fmt_im(gold_30m)}`",
        "",
        f"• *DXY D1:* `{_fmt_im(im['DXY_D1'])}`",
        f"• *DXY H1:* `{_fmt_im(im['DXY_H1'])}`",
        f"• *US10Y D1:* `{_fmt_im(im['US10Y_D1'])}`",
        f"• *US10Y H1:* `{_fmt_im(im['US10Y_H1'])}`",
        "",
        f"• *Vốn:* `${ACCOUNT_CAPITAL_USD:g}`",
        f"• *Lot:* `{LOT_SIZE}`",
        f"• *SL tối đa:* `{MAX_SL_POINTS}` giá",
        f"• *TP1:* `{RR_RATIO}R`",
        f"• *TP2:* `{TP2_RATIO}R`",
        f"• *TP3:* `{TP3_RATIO}R`",
        f"• *Khung nghỉ:* `{sorted(QUIET_HOURS_VN)}`",
        f"• *Trạng thái nghỉ:* `{'CÓ' if is_quiet_hour() else 'KHÔNG'}`",
    ]

    if news.get("today_nfp_cpi"):
        lines.append("")
        lines.append("🗓 *NFP/CPI hôm nay:*")
        for e in news["today_nfp_cpi"]:
            lines.append(f"  - {e['tag']} (`{e['time_vn'][11:16]}`)")
    else:
        lines.append("")
        lines.append("🗓 *NFP/CPI hôm nay:* Không có lịch NFP/CPI được ghi nhận.")

    return "\n".join(lines)


async def telegram_polling_loop() -> None:
    if not TELEGRAM_TOKEN:
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    while True:
        try:
            offset = int(state.get("last_update_id", 0)) + 1
            resp = await asyncio.to_thread(
                requests.get,
                f"{base_url}/getUpdates",
                params={"offset": offset, "timeout": 20},
                timeout=25,
            )
            if resp.ok:
                data = resp.json()
                for upd in data.get("result", []):
                    uid = upd.get("update_id")
                    if isinstance(uid, int):
                        state["last_update_id"] = uid
                        save_state()
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    if text.startswith("/gia") or text.startswith("/status"):
                        reply = await asyncio.to_thread(build_status_message)
                        await asyncio.to_thread(send_telegram, reply, chat_id)
        except Exception as e:
            print(f"[POLLING] Lỗi lấy tin nhắn Telegram: {e}")
            await asyncio.sleep(5)


async def scanner_loop() -> None:
    await asyncio.sleep(3)
    while True:
        try:
            if not is_quiet_hour():
                intermarket = await asyncio.to_thread(get_intermarket_snapshot)
                news = await asyncio.to_thread(fetch_forexfactory_nfp_cpi)

                for tf in TIMEFRAMES:
                    sig = await asyncio.to_thread(analyze_timeframe, tf, intermarket, news)
                    if not sig:
                        continue

                    if BLOCK_ON_NFP_CPI and news.get("has_alert"):
                        print(f"[FILTER] Bỏ qua tín hiệu {tf} do có tin NFP/CPI sát giờ.")
                        continue

                    key = f"{tf}:{sig['signal_type']}"
                    last_ts = float(state.get("last_signals", {}).get(key, 0.0))
                    if time.time() - last_ts < COOLDOWN_SECONDS:
                        continue

                    msg = await asyncio.to_thread(generate_ai_commentary, sig)
                    await asyncio.to_thread(send_telegram, msg)

                    state.setdefault("last_signals", {})[key] = time.time()
                    save_state()
        except Exception as e:
            print(f"[SCANNER] Lỗi vòng lặp quét: {e}")

        await asyncio.sleep(SCAN_INTERVAL)


@app.on_event("startup")
async def on_startup() -> None:
    load_state()
    asyncio.create_task(scanner_loop())
    asyncio.create_task(telegram_polling_loop())


@app.get("/")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "time_vn": now_vn().isoformat(),
        "timeframes": TIMEFRAMES,
        "quiet_hour": is_quiet_hour(),
    })


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    msg = payload.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if text.startswith("/gia") or text.startswith("/status"):
        reply = await asyncio.to_thread(build_status_message)
        await asyncio.to_thread(send_telegram, reply, chat_id)
    return JSONResponse({"ok": True})
