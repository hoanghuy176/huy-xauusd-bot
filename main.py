import os
from fastapi import FastAPI, Request
import requests
from google import genai

app = FastAPI()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

@app.get("/")
def health_check():
    return {"status": "Bot XAUUSD dang hoat dong"}

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )

def calculate_rr_1_2(side: str, entry: float, zone: float, buffer: float, rr: float = 2.0):
    if side.upper() == "BUY":
        sl = zone - buffer
        risk = entry - sl
        tp = entry + (rr * risk)
    else:
        sl = zone + buffer
        risk = sl - entry
        tp = entry - (rr * risk)
    return round(sl, 2), round(tp, 2), round(risk, 2)

@app.post("/webhook")
async def tv_webhook(request: Request):
    data = await request.json()
    symbol = data.get("symbol", "XAUUSD")
    side = data.get("side", "BUY").upper()
    entry = float(data["price"])
    zone = float(data["zone_level"])
    buffer = float(data.get("sl_buffer", 3.0))
    rr = float(data.get("rr_ratio", 2.0))

    sl, tp, risk = calculate_rr_1_2(side, entry, zone, buffer, rr)

    if risk <= 0:
        return {"status": "ignored_invalid_risk"}

    try:
        prompt = (
            f"Tín hiệu {symbol} khung H1: {side} quanh vùng cản {zone:.2f}. "
            f"Entry: {entry:.2f}, SL: {sl:.2f} (risk {risk:.2f} giá), TP: {tp:.2f} (R:R 1:{rr:.0f}). "
            f"Viết 3 gạch đầu dòng cực ngắn gọn (dưới 50 từ) lưu ý xác nhận nến đóng cửa H1 và quản lý rủi ro."
        )
        ai_res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        ai_note = ai_res.text.strip()
    except Exception:
        ai_note = "Chờ xác nhận đóng nến H1 rõ ràng và kiểm tra lịch tin tức trước khi vào lệnh."

    icon = "🟢" if side == "BUY" else "🔴"
    msg = (
        f"{icon} *BOT AI XAU/USD (H1) - TÍN HIỆU {side}*\n"
        f"• Vùng cản tham chiếu: `{zone:.2f}`\n"
        f"• *Entry:* `{entry:.2f}`\n"
        f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
        f"• *Take Profit (2R):* `{tp:.2f}` (+{rr * risk:.2f} giá)\n\n"
        f"🤖 *Góc nhìn AI:*\n{ai_note}"
    )
    send_telegram(msg)
    return {"status": "ok"}
