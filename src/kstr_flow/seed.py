from __future__ import annotations

import math as _math
import random as _random
from typing import Any


class SafeMath:
    _allowed = {
        "ceil", "comb", "copysign", "cos", "degrees", "dist", "e", "exp",
        "fabs", "factorial", "floor", "fmod", "fsum", "gcd", "hypot", "inf",
        "isclose", "isfinite", "isinf", "isnan", "lcm", "log", "log10",
        "log2", "nan", "pi", "pow", "prod", "radians", "remainder", "sin",
        "sqrt", "tan", "tau", "trunc",
    }

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in self._allowed:
            raise AttributeError(name)
        return getattr(_math, name)


class SeededRandom:
    """Deterministic random namespace bound to a KSTR Flow execution seed."""

    _allowed = {
        "choice", "choices", "getrandbits", "randint", "random", "randrange",
        "sample", "shuffle", "triangular", "uniform",
    }

    def __init__(self, seed: int):
        self.seed = int(seed)
        self._rng = _random.Random(self.seed)

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in self._allowed:
            raise AttributeError(name)
        return getattr(self._rng, name)
