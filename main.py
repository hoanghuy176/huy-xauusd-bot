import asyncio
import json
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
RENDER_URL = os.getenv("RENDER_URL", "https://huy-xauusd-bot.onrender.com").strip().rstrip("/")

SL_BUFFER = float(os.getenv("SL_BUFFER", "3.0"))
RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))
STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()

app = FastAPI(title="XAUUSD H1 Breakout Bot")

gemini_client: Optional[genai.Client] = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:
        print(f"[WARN] Khởi tạo Gemini client lỗi: {exc}")


def default_state() -> Dict[str, Any]:
    return {
        "last_alert_candle_ts": None,
        "last_signal_key": None,
        "last_alert_ts": 0.0,
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_state()
        return {
            "last_alert_candle_ts": data.get("last_alert_candle_ts"),
            "last_signal_key": data.get("last_signal_key"),
            "last_alert_ts": float(data.get("last_alert_ts", 0.0)),
        }
    except Exception as exc:
        print(f"[WARN] Không đọc được {STATE_FILE}: {exc}")
        return default_state()


def save_state(state: Dict[str, Any]) -> None:
    if not STATE_FILE:
        return
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
