import os
import threading
import time
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI
from google import genai

app = FastAPI()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

ZONE_BUFFER = float(os.environ.get("ZONE_BUFFER", "2.5"))   # Vùng đệm chạm cản (giá)
SL_BUFFER = float(os.environ.get("SL_BUFFER", "3.0"))       # Khoảng đệm SL ngoài vùng cản
RR_RATIO = float(os.environ.get("RR_RATIO", "2.0"))         # Tỷ lệ R:R 1:2
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # Quét mỗi 300s (5 phút)
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "3600"))  # Nghỉ 60 phút giữa các lần báo

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print("Lỗi gửi Telegram:", e)


def fetch_h1_gold():
    # Gold Futures trên Yahoo Finance thường phản ánh sát biến động giá vàng quốc tế
    df = yf.download("GC=F", period="10d", interval="1h", progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["High", "Low", "Close"]).reset_index()
    return df


def find_sr_levels(df: pd.DataFrame, window: int = 8):
    supports = []
    resistances = []
    lows = df["Low"].tolist()
    highs = df["High"].tolist()

    for i in range(window, len(df) - window):
        if lows[i] == min(lows[i - window : i + window + 1]):
            supports.append(float(lows[i]))
        if highs[i] == max(highs[i - window : i + window + 1]):
            resistances.append(float(highs[i]))

    return supports[-3:], resistances[-3:]


def calc_rr(side: str, entry: float, zone: float, sl_buf: float, rr: float):
    if side == "BUY":
        sl = zone - sl_buf
        risk = entry - sl
        tp = entry + (rr * risk)
    else:
        sl = zone + sl_buf
        risk = sl - entry
        tp = entry - (rr * risk)
    return round(sl, 2), round(tp, 2), round(risk, 2)


def get_ai_note(side: str, price: float, zone: float, sl: float, tp: float, risk: float):
    fallback = "Chờ nến H1 đóng cửa xác nhận phản ứng giá và kiểm tra tin tức Mỹ trước khi vào lệnh."
    if not client:
        return fallback
    try:
        prompt = (
            f"Tín hiệu XAU/USD khung H1: {side} quanh vùng "
            f"{'Hỗ trợ' if side == 'BUY' else 'Kháng cự'} {zone:.2f}. "
            f"Entry: {price:.2f}, SL: {sl:.2f} (risk {risk:.2f} giá), TP: {tp:.2f} (R:R 1:{RR_RATIO:.0f}). "
            f"Viết 3 gạch đầu dòng ngắn gọn (dưới 50 từ) lưu ý xác nhận nến H1 và quản lý vốn."
        )
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return res.text.strip() if res.text else fallback
    except Exception:
        return fallback


def scanner_loop():
    last_alert_ts = 0.0
    send_telegram("🤖 *Bot XAU/USD (H1 - R:R 1:2)* đã khởi động chế độ tự quét giá trên Render!")

    while True:
        try:
            df = fetch_h1_gold()
            if df is not None and len(df) >= 25:
                current_price = float(df["Close"].iloc[-1])
                supports, resistances = find_sr_levels(df, window=8)

                if time.time() - last_alert_ts >= COOLDOWN_SECONDS:
                    alerted = False

                    for sup in supports:
                        if abs(current_price - sup) <= ZONE_BUFFER:
                            sl, tp, risk = calc_rr("BUY", current_price, sup, SL_BUFFER, RR_RATIO)
                            if risk > 0:
                                note = get_ai_note("BUY", current_price, sup, sl, tp, risk)
                                msg = (
                                    f"🟢 *TÍN HIỆU VÀNG (H1) - CHẠM HỖ TRỢ (BUY)*\n"
                                    f"• Vùng Hỗ trợ H1: `{sup:.2f}`\n"
                                    f"• *Entry:* `{current_price:.2f}`\n"
                                    f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                                    f"• *Take Profit (2R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n\n"
                                    f"🤖 *Góc nhìn AI:*\n{note}"
                                )
                                send_telegram(msg)
                                last_alert_ts = time.time()
                                alerted = True
                                break

                    if not alerted:
                        for res_lvl in resistances:
                            if abs(current_price - res_lvl) <= ZONE_BUFFER:
                                sl, tp, risk = calc_rr("SELL", current_price, res_lvl, SL_BUFFER, RR_RATIO)
                                if risk > 0:
                                    note = get_ai_note("SELL", current_price, res_lvl, sl, tp, risk)
                                    msg = (
                                        f"🔴 *TÍN HIỆU VÀNG (H1) - CHẠM KHÁNG CỰ (SELL)*\n"
                                        f"• Vùng Kháng cự H1: `{res_lvl:.2f}`\n"
                                        f"• *Entry:* `{current_price:.2f}`\n"
                                        f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                                        f"• *Take Profit (2R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n\n"
                                        f"🤖 *Góc nhìn AI:*\n{note}"
                                    )
                                    send_telegram(msg)
                                    last_alert_ts = time.time()
                                    break
        except Exception as e:
            print("Lỗi vòng lặp quét giá:", e)

        time.sleep(CHECK_INTERVAL)


@app.on_event("startup")
def on_startup():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()


@app.get("/")
def root():
    return {"status": "Bot XAUUSD H1 R:R 1:2 dang chay"}
