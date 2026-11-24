# Dùng nến H1 vừa đóng cửa xong (iloc[-2]) để tránh tín hiệu giả khi nến chưa đóng
                closed_candle = df.iloc[-2]
                closed_close = float(closed_candle["Close"])
                candle_ts = str(closed_candle.iloc[0])

                # Lấy vùng cản từ dữ liệu trước nến vừa đóng để không tự lấy chính nến này làm cản
                supports, resistances = find_sr_levels(df.iloc[:-1], window=8)

                current_signal_key = None
                signal_payload = None

                # 1. Vượt kháng cự và đóng nến phía trên
                for res_lvl in resistances:
                    if closed_close > res_lvl:
                        sl = round(res_lvl - SL_BUFFER, 2)
                        risk = round(closed_close - sl, 2)
                        if risk > 0:
                            tp = round(closed_close + (RR_RATIO * risk), 2)
                            current_signal_key = f"BREAK_UP_{round(res_lvl, 1)}"
                            signal_payload = ("BREAK_UP", res_lvl, closed_close, sl, tp, risk)
                        break

                # 2. Phá hỗ trợ và đóng nến phía dưới
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

                if (
                    signal_payload
                    and candle_ts != last_alert_candle_ts
                    and current_signal_key != last_signal_key
                    and (time.time() - last_alert_ts >= COOLDOWN_SECONDS)
                ):
                    side, zone_lvl, entry_px, sl, tp, risk = signal_payload
                    if side == "BREAK_UP":
                        note = get_ai_note(
                            f"Nến H1 XAU/USD đóng cửa vượt Kháng cự {zone_lvl:.2f} tại {entry_px:.2f}, SL {sl:.2f}, TP {tp:.2f} (R:R 1:2). Viết 3 gạch đầu dòng dưới 50 từ.",
                            "Ưu tiên canh nhịp hồi kiểm tra lại vùng cản vừa phá hoặc quản lý rủi ro chặt.",
                        )
                        send_telegram(
                            f"🚀 *NẾN H1 VƯỢT KHÁNG CỰ & ĐÓNG CỬA*\n"
                            f"• Kháng cự bị phá: `{zone_lvl:.2f}`\n"
                            f"• *Entry (đóng nến H1):* `{entry_px:.2f}`\n"
                            f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                            f"• *Take Profit (2R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n\n"
                            f"🤖 *Góc nhìn AI:*\n{note}"
                        )
                    else:
                        note = get_ai_note(
                            f"Nến H1 XAU/USD đóng cửa phá Hỗ trợ {zone_lvl:.2f} tại {entry_px:.2f}, SL {sl:.2f}, TP {tp:.2f} (R:R 1:2). Viết 3 gạch đầu dòng dưới 50 từ.",
                            "Ưu tiên canh nhịp hồi kiểm tra lại vùng cản vừa phá hoặc quản lý rủi ro chặt.",
                        )
                        send_telegram(
                            f"📉 *NẾN H1 PHÁ HỖ TRỢ & ĐÓNG CỬA*\n"
                            f"• Hỗ trợ bị phá: `{zone_lvl:.2f}`\n"
                            f"• *Entry (đóng nến H1):* `{entry_px:.2f}`\n"
                            f"• *Stop Loss (1R):* `{sl:.2f}` (-{risk:.2f} giá)\n"
                            f"• *Take Profit (2R):* `{tp:.2f}` (+{RR_RATIO * risk:.2f} giá)\n\n"
                            f"🤖 *Góc nhìn AI:*\n{note}"
                        )
                    last_alert_candle_ts = candle_ts
                    last_signal_key = current_signal_key
                    last_alert_ts = time.time()
