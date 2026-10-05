"""The browser form flows: session cookie, no-double-send, kept values.

The load-bearing rule is SPEC.md's: keep the pay form's values after success,
and submitting it again without changing a field must not send another payment.
That is enforced by deriving the Idempotency-Key from a per-render nonce plus the
canonical body, so these tests exercise the real derivation rather than a mock.

Every helper follows the redirect the way a browser does, because the flash
(values kept, refusal messages) rides in that first request's query string.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _nonce(html: str) -> str:
    m = re.search(r'name="nonce" value="([^"]+)"', html)
    assert m, "screen must carry a nonce"
    return m.group(1)


def _input_value(html: str, testid: str) -> str:
    m = re.search(rf'data-testid="{testid}"[^>]*value="([^"]*)"', html)
    assert m, f"missing {testid}"
    return m.group(1)


def _data_amount(html: str, testid: str) -> str:
    """Balances are elements carrying data-amount, not form inputs."""
    m = re.search(rf'data-testid="{testid}" data-amount="(-?\d+)"', html)
    assert m, f"missing {testid}"
    return m.group(1)


def _alert(html: str, testid: str) -> str:
    m = re.search(rf'data-testid="{testid}"[^>]*>([^<]*)<', html)
    return m.group(1).strip() if m else ""


def _get(path: str = "/") -> str:
    return client.get(path, headers={"Accept": "text/html"}).text


def _submit(path: str, data: dict) -> str:
    """POST then follow the redirect with the browser's Accept header."""
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code in (302, 303), (r.status_code, r.text[:200])
    return client.get(r.headers["location"], headers={"Accept": "text/html"}).text


@pytest.fixture()
def ada():
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
    client.post(
        "/ui/login", data={"email": "ada@example.com", "password": "pw12345"}
    )
    return client


# ----------------------------------------------------------------------
# session
# ----------------------------------------------------------------------


def test_signing_in_sets_a_session_cookie_and_shows_the_wallet(ada):
    assert "pocketful_session" in ada.cookies
    body = _get()
    assert 'data-testid="current-handle"' in body
    assert ">ada<" in body


def test_signing_out_clears_the_session(ada):
    _submit("/ui/logout", {})
    assert 'data-testid="login-submit"' in _get()


def test_sign_in_failure_shows_auth_error(ada):
    r = client.post(
        "/ui/login", data={"email": "ada@example.com", "password": "wrong"}
    )
    assert _alert(r.text, "auth-error")


def test_a_bearer_header_still_works_for_the_api(ada):
    token = client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "pw12345"}
    ).json()["access_token"]
    r = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["handle"] == "ada"


# ----------------------------------------------------------------------
# the no-double-send rule
# ----------------------------------------------------------------------


def test_resubmitting_the_pay_form_unchanged_sends_exactly_one_payment(ada):
    nonce = _nonce(_get())
    body = {"to": "bob", "amount": "10.00", "note": "lunch", "nonce": nonce}

    first = _submit("/ui/pay", body)
    balance_after_first = _data_amount(first, "wallet-balance")

    second = _submit("/ui/pay", body)

    assert _data_amount(second, "wallet-balance") == balance_after_first
    assert second.count('data-testid="activity-item-') == 1
    assert not _alert(second, "pay-error")


def test_changing_a_field_makes_the_next_submission_a_new_payment(ada):
    nonce = _nonce(_get())
    _submit("/ui/pay", {"to": "bob", "amount": "10.00", "note": "one", "nonce": nonce})
    nonce2 = _nonce(_get())
    body = _submit("/ui/pay", {"to": "bob", "amount": "10.00", "note": "two", "nonce": nonce2})
    assert body.count('data-testid="activity-item-') == 2


def test_a_fresh_page_load_can_deliberately_repeat_the_same_payment(ada):
    """A new nonce means a genuinely new payment, not a replay."""
    _submit("/ui/pay", {"to": "bob", "amount": "5.00", "note": "", "nonce": _nonce(_get())})
    body = _submit("/ui/pay", {"to": "bob", "amount": "5.00", "note": "", "nonce": _nonce(_get())})
    assert body.count('data-testid="activity-item-') == 2


def test_values_are_kept_after_success(ada):
    body = _submit(
        "/ui/pay",
        {"to": "bob", "amount": "12.50", "note": "kept", "nonce": _nonce(_get())},
    )
    assert _input_value(body, "pay-handle") == "bob"
    assert _input_value(body, "pay-amount") == "12.50"
    assert _input_value(body, "pay-note") == "kept"


# ----------------------------------------------------------------------
# refused and malformed input
# ----------------------------------------------------------------------


def test_insufficient_funds_shows_pay_error(ada):
    body = _submit(
        "/ui/pay", {"to": "bob", "amount": "9999.00", "nonce": _nonce(_get())}
    )
    assert _alert(body, "pay-error")
    assert _input_value(body, "pay-amount") == "9999.00"


@pytest.mark.parametrize("amount", ["15.005", "abc", "", "-5", "1.2.3"])
def test_malformed_amount_shows_the_error_without_sending_a_request(ada, amount):
    before = _data_amount(_get(), "wallet-balance")
    body = _submit("/ui/pay", {"to": "bob", "amount": amount, "nonce": _nonce(_get())})
    assert _data_amount(body, "wallet-balance") == before, "no payment may be sent"
    assert _alert(body, "pay-error")


def test_unknown_handle_shows_pay_error(ada):
    body = _submit("/ui/pay", {"to": "nobody", "amount": "1.00", "nonce": _nonce(_get())})
    assert _alert(body, "pay-error")


def test_paying_yourself_shows_pay_error(ada):
    body = _submit("/ui/pay", {"to": "ada", "amount": "1.00", "nonce": _nonce(_get())})
    assert _alert(body, "pay-error")


# ----------------------------------------------------------------------
# requests
# ----------------------------------------------------------------------


def test_request_flow_gives_the_creator_cancel_not_pay(ada):
    _submit(
        "/ui/request",
        {"to": "bob", "amount": "5.00", "note": "n", "nonce": _nonce(_get())},
    )
    page = _get("/requests")
    assert 'data-status="open"' in page
    assert "request-cancel-" in page
    assert "request-pay-" not in page


def test_recipient_sees_pay_and_decline(ada):
    _submit(
        "/ui/request",
        {"to": "bob", "amount": "5.00", "note": "n", "nonce": _nonce(_get())},
    )
    # No reset here: it would wipe the request the form just created.
    bob = client.post(
        "/auth/login", json={"email": "bob@example.com", "password": "pw12345"}
    ).json()["access_token"]
    page = client.get(
        "/requests",
        headers={"Accept": "text/html", "Authorization": f"Bearer {bob}"},
    ).text
    assert "request-pay-" in page
    assert "request-decline-" in page
    assert "request-cancel-" not in page


def test_empty_requests_screen_shows_both_containers(ada):
    page = _get("/requests")
    assert 'data-testid="incoming-list"' in page
    assert 'data-testid="outgoing-list"' in page
    assert 'data-testid="empty-requests"' in page


# ----------------------------------------------------------------------
# authorizations
# ----------------------------------------------------------------------


def test_reserve_then_void_through_the_ui(ada):
    _submit(
        "/ui/authorizations",
        {"to": "bob", "amount": "20.00", "nonce": _nonce(_get("/authorizations"))},
    )
    page = _get("/authorizations")
    assert 'data-testid="wallet-held"' in page
    assert "20.00 EUR" in page

    aid = re.search(r'data-testid="authorization-item-([^"]+)"', page).group(1)
    page = _submit(f"/ui/authorizations/{aid}/void", {"nonce": _nonce(page)})
    assert 'data-status="voided"' in page
    assert 'data-testid="wallet-held"' not in page


def test_reserving_more_than_available_is_refused(ada):
    body = _submit(
        "/ui/authorizations",
        {"to": "bob", "amount": "9999.00", "nonce": _nonce(_get("/authorizations"))},
    )
    assert _alert(body, "authorize-error")


def test_available_is_the_headline_and_held_is_secondary(ada):
    _submit(
        "/ui/authorizations",
        {"to": "bob", "amount": "20.00", "nonce": _nonce(_get("/authorizations"))},
    )
    page = _get("/authorizations")
    # 20.00 EUR is 2000 minor units out of a seeded 30.00 EUR.
    assert _data_amount(page, "wallet-available") == "1000"
    assert _data_amount(page, "wallet-held") == "2000"
    assert _data_amount(page, "wallet-balance") == "3000"
    assert 'class="money headline" data-testid="wallet-available"' in page


def test_capture_moves_held_money_to_the_receiver(ada):
    token = client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "pw12345"}
    ).json()["access_token"]
    client.post(
        "/authorizations",
        json={"to": "bob", "amount": 2000, "note": "h"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "cap-1"},
    )
    btok = client.post(
        "/auth/login", json={"email": "bob@example.com", "password": "pw12345"}
    ).json()["access_token"]
    H = {"Accept": "text/html", "Authorization": f"Bearer {btok}"}
    page = client.get("/authorizations", headers=H).text
    aid = re.search(r'data-testid="authorization-item-([^"]+)"', page).group(1)
    r = client.post(
        f"/ui/authorizations/{aid}/capture",
        data={"amount": "20.00", "nonce": _nonce(page)},
        headers={"Authorization": f"Bearer {btok}"},
        follow_redirects=False,
    )
    page = client.get(
        r.headers["location"],
        headers={"Accept": "text/html", "Authorization": f"Bearer {btok}"},
    ).text
    assert 'data-status="captured"' in page
    assert _alert(page, "authorization-error") == ""
    bal = client.get("/me", headers={"Authorization": f"Bearer {btok}"}).json()
    assert bal["balance"] == 2000


def test_the_session_token_never_appears_in_markup(ada):
    body = _get()
    assert ada.cookies.get("pocketful_session") not in body


def test_json_endpoints_still_reject_a_bare_cookie(ada):
    """Cookie auth is confined to the three screens; writes stay header-only."""
    r = client.post("/payments", json={"to": "bob", "amount": 1}, headers={"Accept": "text/html"})
    assert r.status_code == 401


# ----------------------------------------------------------------------
# SPEC: activity visibility, refresh, uncertain outcomes
# ----------------------------------------------------------------------


def test_activity_items_carry_the_visibility_the_payer_chosen(ada):
    _submit("/ui/pay", {"to": "bob", "amount": "1.00", "note": "p",
                        "visibility": "public", "nonce": _nonce(_get())})
    _submit("/ui/pay", {"to": "bob", "amount": "2.00", "note": "s",
                        "visibility": "private", "nonce": _nonce(_get())})
    body = _get()
    seen = dict(re.findall(r'data-testid="activity-item-([^"]+)" data-visibility="([^"]+)"', body))
    assert len(seen) == 2, body
    assert set(seen.values()) == {"public", "private"}


def test_wallet_refresh_reloads_the_balance_without_clearing_the_pay_form(ada):
    """Refresh is a GET on the same route, so the form's own fields come back."""
    before = _data_amount(_get(), "wallet-balance")
    # Another client spends, so a refresh has something new to show.
    tok = client.post(
        "/auth/login", json={"email": "ada@example.com", "password": "pw12345"}
    ).json()["access_token"]
    client.post(
        "/payments",
        json={"to": "bob", "amount": 500},
        headers={"Authorization": f"Bearer {tok}", "Idempotency-Key": "refresh-1"},
    )
    body = _get(
        "/?to=zoe&amount=7.25&note=keepme&visibility=private"
    )
    assert _data_amount(body, "wallet-balance") == "2500"
    assert before == "3000"
    assert _input_value(body, "pay-handle") == "zoe"
    assert _input_value(body, "pay-amount") == "7.25"
    assert _input_value(body, "pay-note") == "keepme"


def test_a_lost_payment_answer_is_uncertain_and_the_retry_moves_money_once(ada, monkeypatch):
    """SPEC: an unknown outcome is not a confirmed rejection."""
    import app.routers.ui as ui_router

    def lost(*a, **kw):
        # The commit may already have happened upstream; only the answer is gone.
        raise ConnectionError("connection reset after the request was sent")

    body = {"to": "bob", "amount": "3.00", "note": "lost", "nonce": _nonce(_get())}
    monkeypatch.setattr(ui_router, "create_payment", lost)
    uncertain = _submit("/ui/pay", body)
    monkeypatch.undo()

    assert _alert(uncertain, "pay-uncertain")
    assert _alert(uncertain, "pay-error") == "", "unknown outcome is not a refusal"
    assert _input_value(uncertain, "pay-amount") == "3.00"
    before = _data_amount(uncertain, "wallet-balance")

    # The unchanged form reuses the same nonce, so the same key: if the first
    # attempt did commit, this replays it instead of paying twice.
    retried = _submit("/ui/pay", body)
    assert _alert(retried, "pay-uncertain") == ""
    assert retried.count('data-testid="activity-item-') == 1
    # 3000 seeded, minus the 300 minor units just sent.
    assert _data_amount(retried, "wallet-balance") == "2700"
    assert before == "3000"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("15", "1500"), ("15.0", "1500"), ("15.00", "1500"), ("15.5", "1550")],
)
def test_decimal_amounts_become_minor_units_as_a_person_types_them(ada, typed, expected):
    body = _submit("/ui/pay", {"to": "bob", "amount": typed, "nonce": _nonce(_get())})
    assert _alert(body, "pay-error") == ""
    assert _input_value(body, "pay-amount") == typed
    # 3000 seeded, minus the amount just sent.
    assert _data_amount(body, "wallet-balance") == str(3000 - int(expected))