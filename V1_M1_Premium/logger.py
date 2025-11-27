"""
Simple flush-aware logger helper.
"""


def log(*args):
    print(*args, flush=True)

