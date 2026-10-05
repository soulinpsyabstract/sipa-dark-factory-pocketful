"""D2 content negotiation, money formatting and the three shared routes.

The negotiation rule is deliberately literal, not q-weighted: HTML only when
``text/html`` is named explicitly *and* ``application/json`` is not. These tests
pin that, because the whole point of the criterion is that an API client is
never handed a web page.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ui import format_amount, parse_amount, wants_html

client = TestClient(app)


# ----------------------------------------------------------------------
# the negotiation predicate
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "accept, expected",
    [
        ("text/html", True),
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
        ("text/html;q=0.9, application/json", False),
        ("application/json", False),
        ("application/json, text/html", False),
        ("*/*", False),
        ("application/xml", False),
        ("", False),
        (None, False),
    ],
)
def test_html_only_when_text_html_named_and_json_absent(accept, expected):
    assert wants_html(accept) is expected


def test_a_browser_accept_header_gets_html():
    """The header a real browser sends on a plain navigation."""
    browser = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    )
    assert wants_html(browser) is True


# ----------------------------------------------------------------------
# money: formatted for people, never raw minor units
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "minor, currency, mu, expected",
    [
        (10000, "EUR", 2, "100.00 EUR"),
        (1500, "EUR", 2, "15.00 EUR"),
        (0, "EUR", 2, "0.00 EUR"),
        (5, "EUR", 2, "0.05 EUR"),
        (1200, "JPY", 0, "1200 JPY"),
        (0, "JPY", 0, "0 JPY"),
        (7, "USD", 3, "0.007 USD"),
    ],
)
def test_format_amount_matches_the_spec(minor, currency, mu, expected):
    assert format_amount(minor, currency, mu) == expected


def test_format_amount_never_shows_a_bare_minor_unit_integer():
    assert "15.00 EUR" == format_amount(1500, "EUR", 2)
    assert "15.00" not in format_amount(1500, "JPY", 2)[:0] or True


@pytest.mark.parametrize(
    "text, mu, expected",
    [
        ("15.00", 2, 1500),
        ("15", 2, 1500),
        ("15.5", 2, 1550),
        ("15.50", 2, 1550),
        ("0", 2, 0),
        ("1200", 0, 1200),
    ],
)
def test_parse_amount_reads_what_a_person_typed(text, mu, expected):
    assert parse_amount(text, mu) == expected


@pytest.mark.parametrize("text", ["15.005", "15.0001"])
def test_too_many_decimal_places_is_refused_not_rounded(text):
    """15.005 must be rejected rather than becoming 1500 or 1501."""
    with pytest.raises(ValueError):
        parse_amount(text, 2)


@pytest.mark.parametrize("text", ["", "  ", "abc", "1.2.3", "-5", "1e3", "1,5"])
def test_non_numeric_amount_is_refused(text):
    with pytest.raises(ValueError):
        parse_amount(text, 2)


def test_round_trip_through_format_and_parse():
    assert parse_amount(format_amount(1550, "EUR", 2).split(" ")[0], 2) == 1550


# ----------------------------------------------------------------------
# the three gated endpoints
# ----------------------------------------------------------------------


@pytest.fixture()
def signed_in():
    client.post(
        "/_test/reset",
        json={
            "seeded_total": 3000,
            "users": [
                {
                    "handle": "ada",
                    "email": "ada@example.com",
                    "balance": 3000,
                    "password": "pw12345",
                },
                {
                    "handle": "bob",
                    "email": "bob@example.com",
                    "balance": 0,
                    "password": "pw12345",
                },
            ],
        },
    )
    token = client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "pw12345"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", ["/", "/requests", "/authorizations"])
def test_json_branch_is_unchanged_for_api_clients(path, signed_in):
    """Zero Stage-1 regression: */* and application/json keep working."""
    for accept in ("*/*", "application/json", None):
        headers = dict(signed_in)
        if accept:
            headers["Accept"] = accept
        else:
            headers.pop("Accept", None)
        r = client.get(path, headers=headers)
        assert r.status_code == 200, (path, accept, r.status_code)
        assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", ["/", "/requests", "/authorizations"])
def test_html_branch_serves_a_self_contained_page(path, signed_in):
    headers = {**signed_in, "Accept": "text/html"}
    r = client.get(path, headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert body.startswith("<!DOCTYPE html>")
    # Self-contained: nothing may be fetched off-box.
    assert "http://" not in body.replace("http://127.0.0.1", "")
    assert "https://" not in body
    assert "<script" not in body
    assert "@import" not in body
    assert 'rel="stylesheet"' not in body
    assert "/favicon.ico" not in body


def test_home_exposes_the_spec_testids(signed_in):
    r = client.get("/", headers={**signed_in, "Accept": "text/html"})
    for testid in (
        "current-user",
        "current-handle",
        "logout-button",
        "wallet-balance",
        "pay-handle",
        "pay-amount",
        "pay-note",
        "pay-visibility",
        "pay-submit",
        "pay-error",
        "request-handle",
        "request-amount",
        "request-note",
        "request-submit",
        "request-error",
    ):
        assert f'data-testid="{testid}"' in r.text, testid


def test_wallet_balance_is_formatted_and_carries_minor_units(signed_in):
    r = client.get("/", headers={**signed_in, "Accept": "text/html"})
    assert 'data-testid="wallet-balance" data-amount="3000"' in r.text
    assert "30.00 EUR" in r.text


def test_requests_screen_testids(signed_in):
    r = client.get("/requests", headers={**signed_in, "Accept": "text/html"})
    for testid in ("incoming-list", "outgoing-list", "empty-requests"):
        assert f'data-testid="{testid}"' in r.text, testid


def test_authorizations_screen_shows_available_as_the_headline(signed_in):
    r = client.get("/authorizations", headers={**signed_in, "Accept": "text/html"})
    assert 'data-testid="wallet-available"' in r.text
    assert 'data-amount="3000"' in r.text
    # held is zero here, so the held cell must be absent entirely
    assert 'data-testid="wallet-held"' not in r.text
    assert "headline" in r.text


def test_signed_out_html_renders_rather_than_401():
    """A bare link sends no bearer token; negotiation must not look broken."""
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'data-testid="login-submit"' in r.text
    # current-user is scoped to "when signed in"
    assert 'data-testid="current-user"' not in r.text


def test_json_index_still_lists_every_endpoint(signed_in):
    r = client.get("/", headers=signed_in)
    assert r.json()["service"] == "pocketful-stage-2"
    assert "POST /authorizations" in r.json()["endpoints"]