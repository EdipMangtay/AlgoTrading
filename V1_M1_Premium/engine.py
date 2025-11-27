"""
Ultimate Reversal WebSocket engine implementation.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque

import ccxt.async_support as ccxt_async
import numpy as np
import websockets
from telegram import Bot

from .config import SETTINGS
from .logger import log
from .math_engine import UltimateMath
from .metalabel import metalabel_confirmation
from .regime import detect_regime


class UltimateReversalEngine:
    """Houses the WebSocket, aggregation, and signal dispatch loops."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or SETTINGS
        log("[INIT] Ultimate Reversal Engine (Meta-Label + Regime) başlatılıyor...")
        self.ex = ccxt_async.binanceusdm(
            {
                "apiKey": self.settings["BINANCE_API_KEY"],
                "secret": self.settings["BINANCE_API_SECRET"],
                "enableRateLimit": True,
            }
        )
        token = self.settings["TG_BOT_TOKEN"]
        self.bot = Bot(token) if token else None

        self.valid_ids: set[str] = set()
        self.latest_price: dict[str, float] = {}
        self.latest_vol24: dict[str, float] = {}
        self.last_vol24_snapshot: dict[str, float] = {}

        context_window = self.settings["CONTEXT_WINDOW"]
        self.price_buffers = defaultdict(lambda: deque(maxlen=context_window))
        self.volume_buffers = defaultdict(lambda: deque(maxlen=context_window))

        self.state: dict[str, dict] = {}

    async def load_futures_list(self):
        markets = await self.ex.load_markets()
        for market in markets.values():
            if market.get("quote") == "USDT" and market.get("active", True):
                self.valid_ids.add(market["id"])

        for sym in self.valid_ids:
            self.state[sym] = {
                "mode": "IDLE",
                "apex": 0.0,
                "nadir": 0.0,
                "cooldown_until": 0.0,
                "context_mean": 0.0,
                "context_sigma": 0.0,
                "locked_sigma": 0.0,
                "sigma_avg": 0.0,
                "z_peak": 0.0,
                "last_signal_ts": 0.0,
            }

        log(f"[MARKET] {len(self.valid_ids)} sembol bulundu.")

    async def websocket_loop(self):
        ws_url = self.settings["WS_URL"]
        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    log("[WS] Bağlandı.")
                    async for msg in ws:
                        data = json.loads(msg).get("data")
                        if not data:
                            continue

                        for item in data:
                            sym = item["s"]
                            if sym not in self.valid_ids:
                                continue

                            price = float(item["c"])
                            vol24 = float(item["v"])
                            self.latest_price[sym] = price
                            self.latest_vol24[sym] = vol24
            except Exception as exc:
                log("[WS ERR]", exc)
                await asyncio.sleep(5)

    async def aggregation_loop(self):
        context_window = self.settings["CONTEXT_WINDOW"]
        while True:
            start = time.time()
            count = 0

            for sym in list(self.valid_ids):
                price = self.latest_price.get(sym)
                if price is None:
                    continue

                vol24 = self.latest_vol24.get(sym, 0.0)
                prev = self.last_vol24_snapshot.get(sym, vol24)
                self.last_vol24_snapshot[sym] = vol24
                delta_vol = max(vol24 - prev, 0.0)

                self.price_buffers[sym].append(price)
                self.volume_buffers[sym].append(delta_vol)

                if len(self.price_buffers[sym]) >= context_window:
                    count += 1
                    await self.process_series(
                        sym,
                        np.array(self.price_buffers[sym]),
                        np.array(self.volume_buffers[sym]),
                    )

            log(f"[AGG] {count} sembol işlendi.")
            elapsed = time.time() - start
            await asyncio.sleep(max(0.0, 1.0 - elapsed))

    async def process_series(self, symbol, prices, volumes):
        st = self.state[symbol]
        now = time.time()

        if now < st["cooldown_until"]:
            return

        price = float(prices[-1])
        settings = self.settings

        tail = prices[-settings["SIGMA_WINDOW"] :]
        rng = (tail.max() - tail.min()) / price * 100
        if rng < settings["MIN_RANGE_PCT"]:
            return

        ctx = prices[-settings["CONTEXT_WINDOW"] :]
        z, mean, sigma_ctx = UltimateMath.z_score(ctx, price)
        st["context_mean"] = mean
        st["context_sigma"] = sigma_ctx

        _, sigma_short = UltimateMath.get_sigma(prices[-settings["SIGMA_WINDOW"] :])

        if st["sigma_avg"] == 0:
            st["sigma_avg"] = sigma_short
        else:
            st["sigma_avg"] = 0.98 * st["sigma_avg"] + 0.02 * sigma_short

        abs_z = abs(z)
        if st["z_peak"] == 0:
            st["z_peak"] = abs_z
        else:
            st["z_peak"] = 0.98 * st["z_peak"] + 0.02 * abs_z

        sm = UltimateMath.ema(prices)
        velocity, acceleration = UltimateMath.get_kinematics(sm)
        regime = detect_regime(sigma_short, st["sigma_avg"], velocity, acceleration)

        entry_strict = settings["ENTRY_STRICTNESS"]
        sig_strict = settings["SIGMA_MOVE_STRICT"]

        if regime == "HIGH_CHOP":
            entry_strict *= 1.05
            sig_strict *= 1.05
        elif regime == "UP_TREND" and acceleration > 0:
            entry_strict *= 0.9
        elif regime == "DOWN_TREND" and acceleration < 0:
            entry_strict *= 0.9

        vw_prices = prices[-settings["VWAP_WINDOW"] :]
        vw_vols = volumes[-settings["VWAP_WINDOW"] :]
        vwap = UltimateMath.rolling_vwap(vw_prices, vw_vols)
        vwap_dev = (price - vwap) / vwap * 100

        mode = st["mode"]
        vol_ratio = sigma_short / max(st["sigma_avg"], 1e-8)
        vol_factor = float(np.clip(vol_ratio, 0.7, 1.5))

        dyn_z = 0.4 * st["z_peak"] + 0.6 * settings["BASE_Z_SCORE_THRESHOLD"]
        dyn_z = max(1.2, min(3.0, dyn_z))

        await self._maybe_start_short(symbol, prices, ctx, st, z, vwap_dev, mode, sigma_short)
        await self._maybe_confirm_short(
            symbol,
            prices,
            st,
            vwap_dev,
            entry_strict,
            vol_factor,
            dyn_z,
            sig_strict,
            acceleration,
            z,
            mean,
            sigma_ctx,
            sigma_short,
        )

        await self._maybe_start_long(symbol, prices, ctx, st, z, vwap_dev, mode, sigma_short)
        await self._maybe_confirm_long(
            symbol,
            prices,
            st,
            vwap_dev,
            entry_strict,
            vol_factor,
            dyn_z,
            sig_strict,
            acceleration,
            z,
            mean,
            sigma_ctx,
            sigma_short,
        )

        if st["mode"] == "COOLDOWN" and now >= st["cooldown_until"] and abs(vwap_dev) < 0.5:
            st["mode"] = "IDLE"

    async def _maybe_start_short(self, symbol, prices, ctx, state, z, vwap_dev, mode, sigma_short):
        settings = self.settings
        if mode == "IDLE" and z >= dyn := self._dyn_threshold(state) and vwap_dev >= settings["VWAP_STRETCH_THRESHOLD"]:
            state["mode"] = "WATCHING_SHORT"
            state["apex"] = float(prices[-1])
            state["locked_sigma"] = sigma_short
            state["nadir"] = float(ctx.min())

    async def _maybe_confirm_short(
        self,
        symbol,
        prices,
        state,
        vwap_dev,
        entry_strict,
        vol_factor,
        dyn_z,
        sig_strict,
        acceleration,
        z_value,
        mean,
        sigma_ctx,
        sigma_short,
    ):
        if state["mode"] != "WATCHING_SHORT":
            return

        settings = self.settings
        price = float(prices[-1])
        if price > state["apex"]:
            state["apex"] = price

        pump_move = state["apex"] - state["nadir"]
        pump_pct = (pump_move / state["nadir"]) * 100 if state["nadir"] > 0 else 0
        base_req = UltimateMath.get_dynamic_reversal_threshold(pump_pct)
        curve_ok, curvature, _, curr_sl = UltimateMath.poly_curve_signal(prices, "SHORT")
        final_req = base_req * (settings["CURVE_ACCELERATOR"] if curve_ok else 1.0)
        final_req *= entry_strict * vol_factor

        drop_pct = (state["apex"] - price) / state["apex"] * 100
        drop_sigma = (state["apex"] - price) / max(state["locked_sigma"], 1e-6)
        z_mean_ok = z_value <= dyn_z * settings["Z_EXIT_BAND"]

        if drop_pct >= final_req and drop_sigma >= sig_strict and acceleration < 0 and z_mean_ok:
            if await metalabel_confirmation(self, symbol, "SHORT"):
                await self.send_signal(
                    symbol,
                    "SHORT",
                    price,
                    z_value,
                    vwap_dev,
                    pump_pct,
                    drop_pct,
                    drop_sigma,
                    curve_ok,
                    curvature,
                    curr_sl,
                    mean,
                    sigma_ctx,
                    sigma_short,
                )
                state["mode"] = "COOLDOWN"
                state["cooldown_until"] = time.time() + settings["COOLDOWN_SEC"]
            else:
                state["mode"] = "IDLE"

    async def _maybe_start_long(self, symbol, prices, ctx, state, z, vwap_dev, mode, sigma_short):
        settings = self.settings
        dyn_threshold = self._dyn_threshold(state)
        if mode == "IDLE" and z <= -dyn_threshold and vwap_dev <= -settings["VWAP_STRETCH_THRESHOLD"]:
            state["mode"] = "WATCHING_LONG"
            state["nadir"] = float(prices[-1])
            state["apex"] = float(ctx.max())
            state["locked_sigma"] = sigma_short

    async def _maybe_confirm_long(
        self,
        symbol,
        prices,
        state,
        vwap_dev,
        entry_strict,
        vol_factor,
        dyn_z,
        sig_strict,
        acceleration,
        z_value,
        mean,
        sigma_ctx,
        sigma_short,
    ):
        if state["mode"] != "WATCHING_LONG":
            return

        settings = self.settings
        price = float(prices[-1])
        if price < state["nadir"]:
            state["nadir"] = price

        dump_move = state["apex"] - state["nadir"]
        dump_pct = (dump_move / state["apex"]) * 100 if state["apex"] > 0 else 0
        base_req = UltimateMath.get_dynamic_reversal_threshold(dump_pct)
        curve_ok, curvature, _, curr_sl = UltimateMath.poly_curve_signal(prices, "LONG")
        final_req = base_req * (settings["CURVE_ACCELERATOR"] if curve_ok else 1.0)
        final_req *= entry_strict * vol_factor

        bounce_pct = (price - state["nadir"]) / state["nadir"] * 100
        bounce_sigma = (price - state["nadir"]) / max(state["locked_sigma"], 1e-6)
        z_mean_ok = z_value >= -dyn_z * settings["Z_EXIT_BAND"]

        if bounce_pct >= final_req and bounce_sigma >= sig_strict and acceleration > 0 and z_mean_ok:
            if await metalabel_confirmation(self, symbol, "LONG"):
                await self.send_signal(
                    symbol,
                    "LONG",
                    price,
                    z_value,
                    vwap_dev,
                    dump_pct,
                    bounce_pct,
                    bounce_sigma,
                    curve_ok,
                    curvature,
                    curr_sl,
                    mean,
                    sigma_ctx,
                    sigma_short,
                )
                state["mode"] = "COOLDOWN"
                state["cooldown_until"] = time.time() + settings["COOLDOWN_SEC"]
            else:
                state["mode"] = "IDLE"

    async def send_signal(
        self,
        symbol,
        side,
        price,
        z,
        vwap_dev,
        move_pct,
        rev_pct,
        rev_sigma,
        curve_ok,
        curvature,
        curr_sl,
        mean,
        sigma_ctx,
        sigma_short,
    ):
        if not self.bot:
            return

        star = "⭐" if abs(z) < 2.5 else "⭐⭐" if abs(z) < 3.5 else "⭐⭐⭐"
        text = (
            f"📌 <b>M1 Hammer</b>\n"
            f"{'🔴 SHORT' if side == 'SHORT' else '🟢 LONG'} {star}\n"
            f"• Coin: <b>#{symbol}</b>\n"
            f"• Fiyat: <b>{price:.6f}</b>\n"
            f"• Z-score: <b>{z:.2f}</b>\n"
            f"• VWAP Sapma: <b>{vwap_dev:.2f}%</b>\n"
            f"• Hareket %: <b>{move_pct:.2f}%</b>\n"
            f"• Dönüş %: <b>{rev_pct:.2f}%</b>\n"
            f"• Sigma Move: <b>{rev_sigma:.2f}</b>\n"
            f"• Eğri Hızlandırıcı (Poly): <b>{'EVET 🏎️' if curve_ok else 'Hayır'}</b>\n"
            f"• Curvature: <b>{curvature:.5f}</b>\n"
            f"• Son slope: <b>{curr_sl:.5f}</b>\n"
            f"• Ort. Mean: <b>{mean:.6f}</b>\n"
            f"• Sigma Context: <b>{sigma_ctx:.6f}</b>\n"
            f"• Sigma Short: <b>{sigma_short:.6f}</b>"
        )
        try:
            await self.bot.send_message(
                self.settings["TG_CHAT_ID"],
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            log(f"[SIGNAL] {symbol} {side} gönderildi.")
        except Exception as exc:
            log("[TG ERR]", exc)

    def _dyn_threshold(self, state):
        settings = self.settings
        dyn_z = 0.4 * state["z_peak"] + 0.6 * settings["BASE_Z_SCORE_THRESHOLD"]
        return max(1.2, min(3.0, dyn_z))

    async def run(self):
        await self.load_futures_list()
        await asyncio.gather(self.websocket_loop(), self.aggregation_loop())

