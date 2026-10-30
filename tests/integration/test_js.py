from unittest.mock import patch

import pytest

from browser_harness import helpers


def _capture_playwright(result=None):
    captured = []

    def fake(action, **params):
        captured.append((action, params))
        return result

    return fake, captured


def test_simple_expression_passes_through():
    fake, captured = _capture_playwright()
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        helpers.js("document.title")
    assert captured == [("evaluate", {"expression": "document.title", "target_id": None})]


def test_illegal_top_level_return_retries_inside_function():
    captured = []

    def fake(action, **params):
        captured.append((action, params))
        if len(captured) == 1:
            raise RuntimeError("SyntaxError: Illegal return statement")
        return 1

    with patch("browser_harness.helpers._playwright", side_effect=fake):
        assert helpers.js("const x = 1; return x") == 1
    assert [params["expression"] for _action, params in captured] == [
        "const x = 1; return x",
        "(function(){const x = 1; return x})()",
    ]


def test_iife_with_internal_return_is_not_retried():
    fake, captured = _capture_playwright("ok")
    expression = "(function(){ return document.title; })()"
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        assert helpers.js(expression) == "ok"
    assert captured == [("evaluate", {"expression": expression, "target_id": None})]


def test_js_passes_iframe_target_to_playwright():
    fake, captured = _capture_playwright("frame result")
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        assert helpers.js("document.title", target_id="iframe-1") == "frame result"
    assert captured == [("evaluate", {"expression": "document.title", "target_id": "iframe-1"})]


def test_js_preserves_playwright_result_types():
    value = {"nan": float("nan"), "infinity": float("inf"), "bigint": 1}
    with patch("browser_harness.helpers._playwright", return_value=value):
        assert helpers.js("window.values") == value


def test_js_propagates_javascript_error():
    with patch(
        "browser_harness.helpers._playwright",
        side_effect=RuntimeError("ReferenceError: missing is not defined"),
    ):
        with pytest.raises(RuntimeError, match="ReferenceError"):
            helpers.js("missing.value")


def test_non_return_error_is_not_retried():
    fake, captured = _capture_playwright()

    def fail(action, **params):
        captured.append((action, params))
        raise RuntimeError("SyntaxError: Invalid token")

    with patch("browser_harness.helpers._playwright", side_effect=fail):
        with pytest.raises(RuntimeError, match="Invalid token"):
            helpers.js("bad syntax")
    assert len(captured) == 1
