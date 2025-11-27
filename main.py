#!/usr/bin/env python3
"""
Executable entrypoint for the Ultimate Reversal Engine.
"""

import asyncio

from V1_M1_Premium import SETTINGS, UltimateReversalEngine


def main():
    engine = UltimateReversalEngine(SETTINGS)
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()

