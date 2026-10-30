from unittest.mock import patch

import pytest

from browser_harness import helpers


def _capture_playwright(result=None):
    captured = []

    def fake_playwright(action, **params):
        captured.append((action, params))
        return result

    return fake_playwright, captured


def _evaluated_expression(captured):
    return next(params["expression"] for action, params in captured if action == "evaluate")


def test_simple_expression_passes_through():
    fake, captured = _capture_playwright()
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        helpers.js("document.title")
    assert _evaluated_expression(captured) == "document.title"


def test_return_statement_gets_wrapped():
    fake, captured = _capture_playwright()
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        helpers.js("const x = 1; return x")
    assert _evaluated_expression(captured) == "(function(){const x = 1; return x})()"


def test_iife_with_internal_return_is_not_double_wrapped():
    fake, captured = _capture_playwright()
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        helpers.js("(function(){ return document.title; })()")
    assert _evaluated_expression(captured) == "(function(){ return document.title; })()"


@pytest.mark.parametrize(
    "expression",
    ['document.body.innerText.includes("return ")', "// return comment\n1 + 1"],
)
def test_non_statement_return_word_does_not_trigger_wrapping(expression):
    fake, captured = _capture_playwright()
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        helpers.js(expression)
    assert _evaluated_expression(captured) == expression


@pytest.mark.parametrize("expression", ["return\t1", "return\n1"])
def test_top_level_return_with_whitespace_gets_wrapped(expression):
    fake, captured = _capture_playwright()
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        helpers.js(expression)
    assert _evaluated_expression(captured) == f"(function(){{{expression}}})()"


def test_js_passes_iframe_target_to_playwright():
    fake, captured = _capture_playwright("frame result")
    with patch("browser_harness.helpers._playwright", side_effect=fake):
        assert helpers.js("document.title", target_id="iframe-1") == "frame result"
    assert captured == [("evaluate", {"expression": "document.title", "target_id": "iframe-1"})]


def test_js_preserves_playwright_result_types():
    value = {"nan": float("nan"), "infinity": float("inf"), "bigint": 1}
    with patch("browser_harness.helpers._playwright", return_value=value):
        assert helpers.js("window.values") == value


def test_js_propagates_playwright_javascript_error():
    with patch(
        "browser_harness.helpers._playwright",
        side_effect=RuntimeError("ReferenceError: missing is not defined"),
    ):
        with pytest.raises(RuntimeError, match="ReferenceError"):
            helpers.js("missing.value")


def test_js_timeout_error_includes_expression_context():
    with patch("browser_harness.helpers._playwright", side_effect=TimeoutError("timed out")):
        with pytest.raises(RuntimeError, match="Playwright evaluation.*document.title"):
            helpers.js("document.title")
