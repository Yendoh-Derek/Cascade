"""
cascade/backend/math_speech.py

Converts LaTeX-style math tagged with $...$ into spoken English for TTS.
The supported syntax is intentionally narrow and deterministic.
"""

from __future__ import annotations

import re


def _convert_expression(expr: str) -> str:
    # Resolve simple nested wrappers first.
    for _ in range(2):
        expr = re.sub(r"\\sqrt\{([^{}]+)\}", r"the square root of \1", expr)
        expr = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"\1 over \2", expr)

    # Common operator and symbol conversions.
    expr = re.sub(r"([A-Za-z0-9_]+)\^\{([^{}]+)\}", r"\1 to the power of \2", expr)
    expr = re.sub(r"([A-Za-z0-9_]+)\^2\b", r"\1 squared", expr)
    expr = re.sub(r"([A-Za-z0-9_]+)\^3\b", r"\1 cubed", expr)
    expr = re.sub(r"([A-Za-z0-9_]+)\^([A-Za-z0-9_]+)", r"\1 to the power of \2", expr)
    expr = re.sub(r"\\times", " times ", expr)
    expr = re.sub(r"\\div", " divided by ", expr)
    expr = re.sub(r"\\pi", "pi", expr)
    expr = re.sub(r"\\approx", " approximately equals ", expr)
    expr = re.sub(r"=", " equals ", expr)
    expr = re.sub(r"\+", " plus ", expr)
    expr = re.sub(r"-", " minus ", expr)

    # Light cleanup for readable speech text.
    expr = expr.replace("{", " ").replace("}", " ")
    expr = re.sub(r"\\", " ", expr)
    expr = re.sub(r"_", " ", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr


def math_to_speech(text: str) -> str:
    """Convert complete $...$ spans into spoken English."""

    def _replace(match: re.Match[str]) -> str:
        return _convert_expression(match.group(1))

    return re.sub(r"\$([^$]+)\$", _replace, text)

