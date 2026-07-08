from backend.math_speech import math_to_speech


def test_basic_exponent():
    assert math_to_speech("$c^2 = a^2 + b^2$") == "c squared equals a squared plus b squared"


def test_sqrt():
    assert math_to_speech("$\\sqrt{a^2 + b^2}$") == "the square root of a squared plus b squared"


def test_fraction():
    assert math_to_speech("$\\frac{a}{b}$") == "a over b"


def test_mixed_prose_and_math():
    assert (
        math_to_speech("So $c^2 = a^2 + b^2$ is the formula.")
        == "So c squared equals a squared plus b squared is the formula."
    )


def test_no_math_passthrough():
    assert math_to_speech("Hello there.") == "Hello there."


def test_pi_and_times():
    assert math_to_speech("$2 \\times \\pi \\times r$") == "2 times pi times r"


def test_negative_exponent_unbraced():
    """Regression: $x^-2$ was silently skipped; '-' is now included in the exponent char class."""
    result = math_to_speech("$x^-2$")
    assert "to the power of" in result
    # The '-' in the exponent is correctly converted to "minus" by the subtraction rule
    assert "minus" in result


def test_escaped_dollar_passthrough():
    """Regression: \\$ used to trigger math-mode detection incorrectly."""
    text = r"The price is \$5 today."
    result = math_to_speech(text)
    # The escaped dollar should NOT be converted as math.
    assert "price" in result
    assert "to the power of" not in result
