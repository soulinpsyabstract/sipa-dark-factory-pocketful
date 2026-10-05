"""The spec's remaining three routes: /signup, /login, /split.

The load-bearing assertion is that ``split-preview`` shows exactly the shares
the server then charges. Both sides come from ``operations.split_shares``, and
these tests check the preview against the payments the store actually
recorded, so the two cannot agree by construction and still be wrong.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
H = {"Accept": "text/html"}


def _seed():
    # seeded_total must equal the sum of the seeded balances, or the reset
    # fails its own invariant and leaves the users unusable.
    client.post(
        "/_test/reset",
        json={
            "seeded_total": 4000,
            "users": [
                {
                    "handle": "ada",
                    "email": "ada@example.com",
                    "balance": 2000,
                    "password": "pw12345",
                },
                {
                    "handle": "bob",
                    "email": "bob@example.com",
                    "balance": 1000,
                    "password": "pw12345",
                },
                {
                    "handle": "cy",
                    "email": "cy@example.com",
                    "balance": 1000,
                    "password": "pw12345",
                },
            ],
        },
    )
    client.cookies.clear()


def _login(email: str = "ada@example.com", password: str = "pw12345"):
    client.post("/ui/login", data={"email": email, "password": password})


def _text(html: str, testid: str) -> str:
    m = re.search(rf'data-testid="{testid}"[^>]*>([^<]*)<', html)
    return m.group(1).strip() if m else ""


def _get(path: str) -> str:
    return client.get(path, headers=H).text


# ----------------------------------------------------------------------
# signup
# ----------------------------------------------------------------------


def test_signup_page_offers_the_three_inputs():
    _seed()
    page = _get("/signup")
    for t in ("signup-email", "signup-password", "signup-display-name", "signup-submit"):
        assert f'data-testid="{t}"' in page, t
    assert 'data-testid="auth-error"' not in page, "no error, so no error element"


def test_signing_up_creates_the_account_and_sends_the_browser_to_login():
    """POST /auth/signup mints no token, so there is no session to hand over."""
    _seed()
    r = client.post(
        "/ui/signup",
        data={
            "email": "newbie@example.com",
            "password": "hunter2hunter2",
            "display_name": "New Bie",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/login"
    assert "pocketful_session" not in client.cookies

    # The account really was created: the login form now works for it.
    assert client.get("/login", headers=H).status_code == 200
    done = client.post(
        "/ui/login",
        data={"email": "newbie@example.com", "password": "hunter2hunter2"},
        follow_redirects=False,
    )
    assert done.status_code == 303, done.text
    home = _get("/")
    assert _text(home, "current-user") == "New Bie"
    assert _text(home, "current-handle") == "newbie"


def test_a_rejected_signup_shows_auth_error_and_keeps_the_email():
    _seed()
    page = client.post(
        "/ui/signup",
        data={
            "email": "!!!@example.com",
            "password": "hunter2hunter2",
            "display_name": "Bad",
        },
    ).text
    assert _text(page, "auth-error")
    assert 'value="!!!@example.com"' in page


def test_a_duplicate_signup_is_refused_with_an_error():
    _seed()
    client.post(
        "/ui/signup",
        data={"email": "ada@example.com", "password": "pw12345", "display_name": "Ada"},
    )
    page = client.post(
        "/ui/signup",
        data={"email": "ada@example.com", "password": "pw12345", "display_name": "Ada"},
    ).text
    assert _text(page, "auth-error")


# ----------------------------------------------------------------------
# login
# ----------------------------------------------------------------------


def test_login_page_offers_its_inputs():
    _seed()
    page = _get("/login")
    for t in ("login-email", "login-password", "login-submit"):
        assert f'data-testid="{t}"' in page, t
    assert 'data-testid="auth-error"' not in page


def test_a_bad_password_shows_auth_error_on_the_login_route():
    _seed()
    page = client.post(
        "/ui/login", data={"email": "ada@example.com", "password": "nope"}
    ).text
    assert _text(page, "auth-error")


def test_signed_in_home_shows_the_display_name_and_a_working_logout():
    _seed()
    _login()
    home = _get("/")
    assert 'data-testid="current-user"' in home
    assert 'data-testid="logout-button"' in home


# ----------------------------------------------------------------------
# split
# ----------------------------------------------------------------------


def test_split_preview_shows_one_share_per_participant():
    _seed()
    _login()
    page = _get("/split?amount=10.00&handles=ada,bob,cy")
    assert 'data-testid="split-preview"' in page
    # 1000 minor over 3: 334 / 333 / 333, remainder to the first.
    assert _text(page, "split-share-ada") == "3.34 EUR"
    assert _text(page, "split-share-bob") == "3.33 EUR"
    assert _text(page, "split-share-cy") == "3.33 EUR"


def test_split_preview_leaves_the_remainder_with_the_earliest_handles():
    _seed()
    _login()
    page = _get("/split?amount=10.00&handles=cy,bob,ada")
    assert _text(page, "split-share-cy") == "3.34 EUR"
    assert _text(page, "split-share-ada") == "3.33 EUR"


def test_an_even_split_needs_no_remainder():
    _seed()
    _login()
    page = _get("/split?amount=10.00&handles=ada,bob")
    assert _text(page, "split-share-ada") == "5.00 EUR"
    assert _text(page, "split-share-bob") == "5.00 EUR"


def test_preview_and_submitted_shares_are_identical():
    """SPEC: the preview shows what the server would compute, before posting.

    The comparison is against the store's own recorded parts, so a preview
    that agreed with a hand-written expectation while the server did something
    else would still fail here.
    """
    _seed()
    _login()
    query = "amount=10.00&handles=ada,bob,cy&note=dinner"
    preview = _get(f"/split?{query}")
    promised = {h: _text(preview, f"split-share-{h}") for h in ("ada", "bob", "cy")}
    assert promised == {
        "ada": "3.34 EUR",
        "bob": "3.33 EUR",
        "cy": "3.33 EUR",
    }

    page = _get(f"/split?{query}")
    nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
    r = client.post(
        "/ui/split",
        data={
            "amount": "10.00",
            "handles": "ada,bob,cy",
            "note": "dinner",
            "nonce": nonce,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    feed = client.get("/activity", headers={"Authorization": f"Bearer {_tok()}"}
                      ).json()["activity"]
    splits = [row for row in feed if row["type"] == "split"]
    assert len(splits) == 1, splits
    charged = {p["handle"]: p["amount"] for p in splits[0]["parts"]}

    # Same numbers the preview printed, formatted the same way.
    for handle, minor in charged.items():
        assert _fmt(minor) == promised[handle], (handle, minor, promised[handle])
    assert sum(charged.values()) == 1000, charged


def _tok(email: str = "ada@example.com") -> str:
    return client.post(
        "/auth/login", json={"email": email, "password": "pw12345"}
    ).json()["access_token"]


def _fmt(minor: int) -> str:
    return f"{minor // 100}.{minor % 100:02d} EUR"


def test_a_bad_split_amount_shows_split_error_and_sends_nothing():
    _seed()
    _login()
    before = _get("/")
    page = client.post(
        "/ui/split",
        data={"amount": "15.005", "handles": "ada,bob", "nonce": "n"},
        follow_redirects=False,
    )
    body = client.get(page.headers["location"], headers=H).text
    assert _text(body, "split-error")
    assert "3.34" not in body


def test_a_duplicate_handle_in_a_split_is_refused():
    _seed()
    _login()
    page = _get("/split?amount=10.00&handles=ada,ada")
    assert _text(page, "split-error")


def test_split_needs_a_signed_in_browser():
    _seed()
    r = client.get("/split", headers=H)
    assert 'data-testid="login-submit"' in r.text


def test_split_form_keeps_its_values_after_a_refusal():
    _seed()
    _login()
    page = _get("/split")
    nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
    r = client.post(
        "/ui/split",
        data={
            "amount": "10.00",
            "handles": "ghost,nobody",
            "note": "keepme",
            "nonce": nonce,
        },
        follow_redirects=False,
    )
    body = client.get(r.headers["location"], headers=H).text
    assert _text(body, "split-error")
    assert 'value="keepme"' in body
    assert 'value="10.00"' in body


def test_split_pages_pull_nothing_over_the_network():
    """SPEC: the UI renders under --network none."""
    _seed()
    _login()
    for path in ("/", "/requests", "/authorizations", "/signup", "/login", "/split"):
        page = _get(path)
        for bad in ("http://", "https://", "//cdn", "@import"):
            assert bad not in page, f"{path} references {bad}"


def test_the_session_token_never_appears_on_the_new_pages():
    _seed()
    _login()
    token = client.cookies.get("pocketful_session")
    assert token
    for path in ("/signup", "/login", "/split"):
        assert token not in _get(path), path