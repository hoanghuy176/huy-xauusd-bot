import asyncio
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, Request
from google import genai

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
RENDER_URL = os.getenv("RENDER_URL", "https://huy-xauusd-bot.onrender.com").strip().rstrip("/")

ZONE_BUFFER = float(os.getenv("ZONE_BUFFER", "2.5"))
SL_BUFFER = float(os.getenv("SL_BUFFER", "3.0"))
RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))

app = FastAPI(title="XAUUSD H1 Breakout Bot")

gemini_client: Optional[genai.Client] = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:
        print(f"[WARN] Khởi tạo Gemini client lỗi: {exc}")


def send_telegram(text: str, chat_id: Optional[str] = None) -> None:
    target_chat = (chat_id or TELEGRAM_CHAT_ID).strip()
    if not TELEGRAM_TOKEN or not target_chat:
        print("[WARN] Thiếu TELEGRAM_TOKEN hoặc TELEGRAM_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print(f"[WARN] Telegram sendMessage lỗi {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"[ERROR] Gửi tin nhắn Telegram thất bại: {exc}")


def register_webhook() -> None:
    if not TELEGRAM_TOKEN or not RENDER_URL:
        return
    webhook_url = f"{RENDER_URL}/telegram-webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    try:
        resp = requests.get(url, params={"url": webhook_url}, timeout=15)
        print(f"[INFO] Đăng ký webhook ({webhook_url}): {resp.text}")
    except Exception as exc:
        print(f"[ERROR] Đăng ký webhook thất bại: {exc}")


def fetch_h1_data() -> Optional[pd.DataFrame]:
    try:
        df = yf.download("GC=F", period="10d", interval="1h", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]

        df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
        if len(df) < 25:
            return None

        df = df.reset_index()
        time_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        df["candle_time"] = df[time_col].astype(str)
        return df
    except Exception as exc:
        print(f"[ERROR] Lấy dữ liệu GC=F thất bại: {exc}")
        return None


def find_sr_levels(df: pd.DataFrame, window: int = 8) -> Tuple[List[float], List[float]]:
    highs = df["High"].astype(float).tolist()
    lows = df["Low"].astype(float).tolist()
    n = len(df)

    raw_supports: List[float] = []
    raw_resistances: List[float] = []

    for i in range(window, n - window):
        cur_low = lows[i]
        cur_high = highs[i]

        if cur_low == min(lows[i - window : i + window + 1]):
            raw_supports.append(round(cur_low, 2))

        if cur_high == max(highs[i - window : i + window + 1]):
            raw_resistances.append(round(cur_high, 2))

    def cluster_levels(levels: List[float], tolerance: float = 2.0) -> List[float]:
        if not levels:
            return []
        levels_sorted = sorted(levels)
        clustered: List[List[float]] = [[levels_sorted[0]]]
        for lvl in levels_sorted[1:]:
            if abs(lvl - clustered[-1][-1]) <= tolerance:
                clustered[-1].append(lvl)
            else:
                clustered.append([lvl])
        return [round(sum(group) / len(group), 2) for group in clustered]

    return cluster_levels(raw_supports), cluster_levels(raw_resistances)


def get_ai_note(prompt: str, fallback: str) -> str:
    if not gemini_client:
        return fallback
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        txt = (response.text or "").strip()
        return txt if txt else fallback
    except Exception as exc:
        print(f"[WARN] Gemini lỗi, dùng fallback: {exc}")
        return fallback


def build_price_report() -> str:
    df = fetch_h1_data()
    if df is None:
        return "⚠️ Tạm thời chưa lấy được dữ liệu XAU/USD (`GC=F`) từ Yahoo Finance. Thử lại sau ít phút."

    current_price = float(df["Close"].iloc[-1])
    closed_candle = df.iloc[-2]
    closed_close = float(closed_candle["Close"])
    closed_time = str(closed_candle["candle_time"])

    supports, resistances = find_sr_levels(df.iloc[:-1], window=8)
    nearest_sup = max([s for s in supports if s <= current_price], default=min(supports) if supports else current_price - 5.0)
    nearest_res = min([r for r in resistances if r >= current_price], default=max(resistances) if resistances else current_price + 5.0)

    ai_text = get_ai_note(
        (
            f"Giá XAU/USD (GC=F) hiện tại: {current_price:.2f}, nến H1 đóng gần nhất ({closed_time}) ở {closed_close:.2f}. "
            f"Hỗ trợ gần: {nearest_sup:.2f}, Kháng cự gần: {nearest_res:.2f}. "
            f"Viết 3 gạch đầu dòng ngắn gọn (dưới 60 từ) về kịch bản giao dịch H1 và quản lý rủi ro."
        ),
        "• Ưu tiên quan sát phản ứng giá tại các vùng hỗ trợ/kháng cự H1.\n• Chỉ vào lệnh khi nến H1 đóng cửa xác nhận rõ ràng.\n• Luôn đặt SL chặt và tuân thủ tỷ lệ rủi ro/lợi nhuận.",
    )

    return (
        f"📊 *CẬP NHẬT XAU/USD (H1 - GC=F)*\n"
        f"• Giá hiện tại: `{current_price:.2f}`\n"
        f"• Nến H1 vừa đóng (`{closed_time}`): `{closed_close:.2f}`\n"
        f"• Hỗ trợ gần nhất: `{nearest_sup:.2f}`\n"
        f"• Kháng cự gần nhất: `{nearest_res:.2f}`\n\n"
        f"🤖 *Nhận định nhanh:*\n{ai_text}"
    )


def scanner_loop() -> None:
    last_alert_candle_ts: Optional[str] = None
    last_signal_key: Optional[str] = None
    last_alert_ts: float = 0.0

    while True:
        try:
            df = fetch_h1_data()
            if df is not None and len(df) >= 20:
                closed_candle = df.iloc[-2]
                closed_close = float(closed_candle["Close"])
                candle_ts = str(closed_candle["candle_time"])

                supports, resistances = find_sr_levels(df.iloc[:-1], window=8)

                current_signal_key: Optional[str] = None
                signal_payload: Optional[Tuple[str, float, float, float, float, float]] = None

                # 1. Nến H1 vượt kháng cự và đóng cửa phía trên
                for res_lvl in resistances:
                    if closed_close > res_lvl:
                        sl = round(res_lvl - SL_BUFFER, 2)
                        risk = round(closed_close - sl, 2)
                        if risk > 0:
                            tp = round(closed_close + (RR_RATIO * risk), 2)
                            current_signal_key = f"BREAK_UP_{round(res_lvl, 1)}"
                            signal_payload = ("BREAK_UP", res_lvl, closed_close, sl, tp, risk)
                        break

                # 2. Nến H1 phá hỗ trợ và đóng cửa phía dưới
                if not signal_payload:
                    for sup_lvl in supports:
                        if closed_close < sup_lvl:
                            sl = round(sup_lvl + SL_BUFFER, 2)
                            risk = round(sl - closed_close, 2)
                            if risk > 0:
                                tp = round(closed_close - (RR_RATIO * risk), 2)
                                current_signal_key = f"BREAK_DOWN_{round(sup_lvl, 1)}"
                                signal_payload = ("BREAK_DOWN", sup_lvl, closed_close, sl, tp, risk)
                            break

                # Chỉ thông báo khi có pha đóng nến phá cản MỚI và qua thời gian nghỉ
                if (
                    signal_payload
                    and candle_ts != last_alert_candle_ts
                    and current_signal_key != last_signal_key
                    and (time.time() - last_alert_ts >= COOLDOWN_SECONDS)
                ):
                    side, zone_lvl, entry_px, sl, tp, risk = signal_payload
                    if side == "BREAK_UP":
                        note = get_ai_note(
                            f"Nến H1 XAU/USD đóng cửa vượt Kháng cự {zone_lvl:.2f} tại {entry_px:.2f}, SL {sl:.2f}, TP {tp:.2f} (R:R 1:{RR_RATIO:.1f}). Viết 3 gạch đầu dòng dưới 50 từ.",
                            "• Nến H1 đã đóng cửa trên vùng cản cũ.\n• Cân nhắc canh nhịp hồi retest hoặc chốt lời từng phần.\n• Giữ SL cố định để tránh quét râu.",
                        )
                        send_telegram(
                            f"🚀 *NẾN H1 VƯỢT KHÁNG CỰ & ĐÓNG CỬA*\n"
                            f"• Kháng cự bị phá: `{zone_lvl:.2f}`\n"
                            f"• *Entry (đóng nến H1):* `{entry_px:.2f}`\n"
                            f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                            f"• *Take Profit ({RR_RATIO:.1f}R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n"
                            f"• Thời điểm nến: `{candle_ts}`\n\n"
                            f"🤖 *Góc nhìn AI:*\n{note}"
                        )
                    else:
                        note = get_ai_note(
                            f"Nến H1 XAU/USD đóng cửa phá Hỗ trợ {zone_lvl:.2f} tại {entry_px:.2f}, SL {sl:.2f}, TP {tp:.2f} (R:R 1:{RR_RATIO:.1f}). Viết 3 gạch đầu dòng dưới 50 từ.",
                            "• Nến H1 đã đóng cửa dưới vùng hỗ trợ cũ.\n• Cân nhắc canh nhịp hồi kiểm tra lại cản hoặc quản trị vị thế.\n• Tuân thủ SL để kiểm soát biến động vàng.",
                        )
                        send_telegram(
                            f"📉 *NẾN H1 PHÁ HỖ TRỢ & ĐÓNG CỬA*\n"
                            f"• Hỗ trợ bị phá: `{zone_lvl:.2f}`\n"
                            f"• *Entry (đóng nến H1):* `{entry_px:.2f}`\n"
                            f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                            f"• *Take Profit ({RR_RATIO:.1f}R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n"
                            f"• Thời điểm nến: `{candle_ts}`\n\n"
                            f"🤖 *Góc nhìn AI:*\n{note}"
                        )

                    last_alert_candle_ts = candle_ts
                    last_signal_key = current_signal_key
                    last_alert_ts = time.time()
        except Exception as exc:
            print(f"[ERROR] Lỗi trong scanner_loop: {exc}")

        time.sleep(SCAN_INTERVAL)
