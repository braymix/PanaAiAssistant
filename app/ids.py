"""Generatori di id leggibili e ordinabili nel tempo."""

from __future__ import annotations

import secrets
import time


def _rand(n: int = 6) -> str:
    return secrets.token_hex(n)


def new_id(prefix: str) -> str:
    # prefix-<ms base36-ish>-<rand>: ordinabile a colpo d'occhio, unico a sufficienza
    return f"{prefix}-{int(time.time() * 1000):x}-{_rand()}"
