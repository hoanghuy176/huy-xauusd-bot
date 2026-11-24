import os
import threading
import time
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, Request
from google import genai

app = FastAPI()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_URL = os.environ.get("RENDER_URL", "https://huy-xauusd-bot.onrender.com")

ZONE_BUFFER = float(os.environ.get("ZONE_BUFFER", "2.5"))
SL_BUFFER = float(os.environ.get("SL_BUFFER", "3.0"))
RR_RATIO = float(os.environ.get("RR_RATIO", "2.0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "3600"))

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def send_telegram(text: str, chat_id: str | None = None):
    target_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not target_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": target_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print("Lỗi gửi Telegram:", e)


def fetch_h1_gold():
    df = yf.download("GC=F", period="10d", interval="1h", progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["High", "Low", "Close"]).reset_index()
    return df


def find_sr_levels(df: pd.DataFrame, window: int = 8):
    supports, resistances = [], []
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


def get_ai_note(prompt: str, fallback: str):
    if not client:
        return fallback
    try:
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return res.text.strip() if res.text else fallback
    except Exception:
        return fallback


def build_price_report():
    df = fetch_h1_gold()
    if df is None or len(df) < 25:
        return "⚠️ Tạm thời chưa kéo được dữ liệu giá vàng, bạn thử lại sau ít phút nhé."

    price = float(df["Close"].iloc[-1])
    high_h1 = float(df["High"].iloc[-1])
    low_h1 = float(df["Low"].iloc[-1])
    supports, resistances = find_sr_levels(df, window=8)

    sup_str = ", ".join(f"`{s:.2f}`" for s in supports) if supports else "Chưa rõ"
    res_str = ", ".join(f"`{r:.2f}`" for r in resistances) if resistances else "Chưa rõ"

    ai_comment = get_ai_note(
        f"Giá vàng hiện tại (H1): {price:.2f} (High H1: {high_h1:.2f}, Low H1: {low_h1:.2f}). "
        f"Hỗ trợ gần nhất: {supports}. Kháng cự gần nhất: {resistances}. "
        f"Nhận xét cực ngắn 2 câu (dưới 40 từ) về vị trí giá hiện tại so với các vùng cản.",
        "Quan sát phản ứng nến H1 tại các vùng hỗ trợ/kháng cự trên trước khi vào lệnh.",
    )

    return (
        f"📊 *CẬP NHẬT GIÁ VÀNG (H1)*\n"
        f"• *Giá hiện tại:* `{price:.2f}`\n"
        f"• Biên độ nến H1: `{low_h1:.2f}` – `{high_h1:.2f}`\n"
        f"• *Hỗ trợ H1:* {sup_str}\n"
        f"• *Kháng cự H1:* {res_str}\n\n"
        f"🤖 *Nhận định nhanh:* {ai_comment}"
    )


def scanner_loop():
    last_alert_ts = 0.0
    send_telegram("🤖 *Bot XAU/USD (H1 - R:R 1:2)* đã sẵn sàng!\nGõ `/gia` bất cứ lúc nào để xem giá hiện tại.")

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
                                note = get_ai_note(
                                    f"Tín hiệu XAU/USD H1: BUY quanh Hỗ trợ {sup:.2f}, Entry {current_price:.2f}, SL {sl:.2f}, TP {tp:.2f} (R:R 1:2). Viết 3 gạch đầu dòng dưới 50 từ.",
                                    "Chờ xác nhận đóng nến H1 rõ ràng trước khi vào lệnh.",
                                )
                                send_telegram(
                                    f"🟢 *TÍN HIỆU VÀNG (H1) - CHẠM HỖ TRỢ (BUY)*\n"
                                    f"• Vùng Hỗ trợ H1: `{sup:.2f}`\n"
                                    f"• *Entry:* `{current_price:.2f}`\n"
                                    f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                                    f"• *Take Profit (2R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n\n"
                                    f"🤖 *Góc nhìn AI:*\n{note}"
                                )
                                last_alert_ts = time.time()
                                alerted = True
                                break

                    if not alerted:
                        for res_lvl in resistances:
                            if abs(current_price - res_lvl) <= ZONE_BUFFER:
                                sl, tp, risk = calc_rr("SELL", current_price, res_lvl, SL_BUFFER, RR_RATIO)
                                if risk > 0:
                                    note = get_ai_note(
                                        f"Tín hiệu XAU/USD H1: SELL quanh Kháng cự {res_lvl:.2f}, Entry {current_price:.2f}, SL {sl:.2f}, TP {tp:.2f} (R:R 1:2). Viết 3 gạch đầu dòng dưới 50 từ.",
                                        "Chờ xác nhận đóng nến H1 rõ ràng trước khi vào lệnh.",
                                    )
                                    send_telegram(
                                        f"🔴 *TÍN HIỆU VÀNG (H1) - CHẠM KHÁNG CỰ (SELL)*\n"
                                        f"• Vùng Kháng cự H1: `{res_lvl:.2f}`\n"
                                        f"• *Entry:* `{current_price:.2f}`\n"
                                        f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                                        f"• *Take Profit (2R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n\n"
                                        f"🤖 *Góc nhìn AI:*\n{note}"
                                    )
                                    last_alert_ts = time.time()
                                    break
        except Exception as e:
            print("Lỗi vòng lặp quét giá:", e)

        time.sleep(CHECK_INTERVAL)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip().lower()

    # Chỉ trả lời đúng chủ bot
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return {"ok": True}

    if any(k in text for k in ["/gia", "giá", "gia", "xau", "vàng", "vang", "/start"]):
        report = build_price_report()
        send_telegram(report, chat_id=chat_id)
    else:
        send_telegram("Gõ `/gia` hoặc nhắn *giá vàng* để xem giá XAU/USD hiện tại và các vùng cản H1 nhé!", chat_id=chat_id)

    return {"ok": True}


@app.on_event("startup")
def on_startup():
    if TELEGRAM_TOKEN and RENDER_URL:
        try:
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                params={"url": f"{RENDER_URL.rstrip('/')}/telegram-webhook"},
                timeout=10,
            )
        except Exception as e:
            print("Lỗi setWebhook:", e)

    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()


@app.get("/")
def root():
    return {"status": "Bot XAUUSD H1 R:R 1:2 dang chay"}
