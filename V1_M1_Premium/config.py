"""
Static configuration values for the Ultimate Reversal Engine.
"""

SETTINGS = {
    # Telegram
    "TG_BOT_TOKEN": "7959911378:AAEl6WlhbJ243tK-WdnTlsoP_scf0RjBpVQ",
    "TG_CHAT_ID": 1222350744,
    # Binance
    "BINANCE_API_KEY": "",
    "BINANCE_API_SECRET": "",
    # Windows
    "CONTEXT_WINDOW": 300,
    "VWAP_WINDOW": 120,
    "SIGMA_WINDOW": 60,
    "POLY_WINDOW": 30,
    # Parameters (loosening for faster signals)
    "BASE_Z_SCORE_THRESHOLD": 1.60,
    "VWAP_STRETCH_THRESHOLD": 0.80,
    "SIGMA_MOVE_STRICT": 0.90,
    "ENTRY_STRICTNESS": 0.80,
    "Z_EXIT_BAND": 1.10,
    "CURVE_ACCELERATOR": 0.85,
    "MIN_CURVATURE": 5.0e-06,
    "EMA_ALPHA": 0.20,
    "MIN_RANGE_PCT": 0.20,
    "REVERSAL_CONFIRM_PCT_MIN": 0.15,
    "DYNAMIC_THRESHOLDS": [
        (15.0, 0.25),
        (7.0, 0.40),
        (3.0, 0.70),
        (1.5, 1.00),
    ],
    "COOLDOWN_SEC": 30,
    "WS_URL": "wss://fstream.binance.com/stream?streams=!miniTicker@arr",
}

