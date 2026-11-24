import json

STATE_FILE = "state.json"


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {
            "last_alert_candle_ts": None,
            "last_signal_key": None,
            "last_alert_ts": 0.0,
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "last_alert_candle_ts": data.get("last_alert_candle_ts"),
            "last_signal_key": data.get("last_signal_key"),
            "last_alert_ts": float(data.get("last_alert_ts", 0.0)),
        }
    except Exception as exc:
        print(f"[WARN] Không đọc được {STATE_FILE}: {exc}")
        return {
            "last_alert_candle_ts": None,
            "last_signal_key": None,
            "last_alert_ts": 0.0,
        }


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[WARN] Không ghi được {STATE_FILE}: {exc}")
