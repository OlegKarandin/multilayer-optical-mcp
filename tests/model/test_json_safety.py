
from multilayer_optical_mcp.model.json_safety import safe_float


def test_finite_float_passes_through():
    assert safe_float(3.5) == 3.5
    assert safe_float(0.0) == 0.0
    assert safe_float(-12.75) == -12.75


def test_non_float_values_pass_through_unchanged():
    assert safe_float("hello") == "hello"
    assert safe_float(None) is None
    assert safe_float(5) == 5
    assert safe_float([1, 2, 3]) == [1, 2, 3]


def test_negative_infinity_sanitized():
    assert safe_float(float("-inf")) == "-Infinity"


def test_positive_infinity_sanitized():
    assert safe_float(float("inf")) == "Infinity"


def test_nan_sanitized():
    assert safe_float(float("nan")) == "NaN"


def test_result_is_json_safe():
    import json
    payload = {"a": safe_float(float("-inf")), "b": safe_float(float("nan")),
               "c": safe_float(2.5)}
    # Must not raise and must not contain bare Infinity/NaN tokens.
    text = json.dumps(payload)
    reparsed = json.loads(text)
    assert reparsed == {"a": "-Infinity", "b": "NaN", "c": 2.5}
