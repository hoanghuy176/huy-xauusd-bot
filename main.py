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
    nearest_sup = max(
        [s for s in supports if s <= current_price],
        default=min(supports) if supports else current_price - 5.0,
    )
    nearest_res = min(
        [r for r in resistances if r >= current_price],
        default=max(resistances) if resistances else current_price + 5.0,
    )

    ai_text = get_ai_note(
        (
            f"Giá XAU/USD (GC=F) hiện tại: {current_price:.2f}, nến H1 đóng gần nhất ({closed_time}) ở {closed_close:.2f}. "
            f"Hỗ trợ gần: {nearest_sup:.2f}, Kháng cự gần: {nearest_res:.2f}. "
            f"Viết 3 gạch đầu dòng ngắn gọn (dưới 60 từ) về kịch bản giao dịch H1 và quản lý rủi ro."
        ),
        "• Ưu tiên quan sát phản ứng giá tại các vùng hỗ trợ/kháng cự H1.\n"
        "• Chỉ vào lệnh khi nến H1 đóng cửa xác nhận
