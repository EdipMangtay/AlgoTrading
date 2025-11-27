"""
Meta-label filter that confirms HL/LH moves after a raw signal.
"""

import asyncio


async def metalabel_confirmation(engine, symbol: str, side: str, window: int = 2) -> bool:
    buffers = engine.price_buffers
    if symbol not in buffers or len(buffers[symbol]) == 0:
        return False

    base = float(buffers[symbol][-1])
    for _ in range(window):
        await asyncio.sleep(1.0)
        if symbol not in buffers or len(buffers[symbol]) == 0:
            continue

        current = float(buffers[symbol][-1])
        if side == "SHORT" and current < base:
            return True
        if side == "LONG" and current > base:
            return True

    return False

