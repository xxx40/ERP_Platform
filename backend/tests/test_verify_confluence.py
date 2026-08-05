import httpx

from scripts.verify_confluence import _html_title, _safe_url


def test_safe_url_removes_query_and_fragment() -> None:
    assert (
        _safe_url("https://kb.example.test/download/file.png?token=secret#section")
        == "https://kb.example.test/download/file.png"
    )


def test_html_title_identifies_login_or_error_page() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<html><head><title>Confluence Login</title></head></html>",
    )

    assert _html_title(response) == "Confluence Login"
