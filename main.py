def build_fallback_message(sig: Dict[str, Any]) -> str:
    im = sig["intermarket"]
    news = sig["news"]
    warn_sl = (
        f"\n⚠️ *Cảnh báo:* Ước lỗ `~${sig['est_loss_usd']}` vượt trần `${MAX_RISK_PER_TRADE_USD:g}`!"
        if sig["est_loss_usd"] > MAX_RISK_PER_TRADE_USD
        else ""
    )

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

    if warn_sl:
        lines.append(warn_sl.strip())

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
