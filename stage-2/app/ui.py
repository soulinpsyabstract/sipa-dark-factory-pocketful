"""Stage-2 D2 UI: content negotiation, money formatting, and the screens.

Pure rendering. Nothing in this module touches the store or the lock: every
function here takes already-projected plain data and returns a string. The
route handlers do the projecting under ``store.read(...)`` and call in
afterwards, which keeps the no-nested-lock guards in ``test_stage1.py`` intact.

Self-contained by construction: no external stylesheet, script, font, image or
favicon, so every screen renders under ``--network none``. The empty
``data:`` favicon is deliberate - without it the browser issues a
``/favicon.ico`` fetch, which is the sort of thing that renders fine locally
and fails an offline gate.
"""

from __future__ import annotations

import html
import re

__all__ = [
    "wants_html",
    "format_amount",
    "parse_amount",
    "render_signed_out",
    "render_home",
    "render_requests",
    "render_authorizations",
]

_DECIMAL = re.compile(r"\A\d+(\.\d+)?\Z")


# ----------------------------------------------------------------------
# content negotiation
# ----------------------------------------------------------------------


def wants_html(accept: str | None) -> bool:
    """True when the caller explicitly asked for HTML and not for JSON.

    The rule is deliberately literal rather than q-weighted: HTML only when
    ``text/html`` is named explicitly *and* ``application/json`` is not. So
    ``text/html;q=0.9, application/json`` yields JSON, and so do ``*/*`` and
    ``application/json`` - an API client is never handed a web page.
    """
    if not accept:
        return False
    named = {part.split(";")[0].strip().lower() for part in accept.split(",")}
    return "text/html" in named and "application/json" not in named


# ----------------------------------------------------------------------
# money
# ----------------------------------------------------------------------


def format_amount(minor: int, currency: str, minor_units: int) -> str:
    """Minor units to a person, e.g. ``1500, EUR, 2`` -> ``15.00 EUR``.

    At ``minor_units == 0`` there is no decimal point at all (``1200 JPY``),
    matching the spec. Raw minor-unit integers are never shown to a person.
    """
    value = int(minor)
    sign = "-" if value < 0 else ""
    digits = abs(value)
    if minor_units <= 0:
        return f"{sign}{digits} {currency}"
    scale = 10**minor_units
    whole, frac = divmod(digits, scale)
    return f"{sign}{whole}.{frac:0{minor_units}d} {currency}"


def parse_amount(text: str, minor_units: int) -> int:
    """Parse what a person typed into minor units, or raise ``ValueError``.

    Accepts ``15.00`` and ``15`` for the same 1500, and ``15.5`` as 1550.
    Rejects more than ``minor_units`` decimal places rather than rounding, so
    ``15.005`` is refused instead of silently becoming 1500 or 1501.
    """
    raw = (text or "").strip()
    if not _DECIMAL.match(raw):
        raise ValueError("enter an amount like 15.00")
    whole, _, frac = raw.partition(".")
    if len(frac) > max(minor_units, 0):
        raise ValueError(f"at most {minor_units} decimal places")
    if minor_units <= 0:
        return int(whole) * 1
    frac = frac.ljust(minor_units, "0")[:minor_units]
    return int(whole) * (10**minor_units) + int(frac or "0")


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# ----------------------------------------------------------------------
# layout
# ----------------------------------------------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#f6f7f9;color:#16181d;
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:#1b4fd8}
header.top{background:#fff;border-bottom:1px solid #dfe3e8;padding:.75rem 1rem;
display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}
header.top .who{font-weight:600}
nav.top{display:flex;flex-wrap:wrap;gap:.5rem;margin-left:auto}
nav.top a{text-decoration:none;padding:.4rem .7rem;border-radius:6px;color:#16181d}
nav.top a[aria-current=page]{background:#e8eeff;color:#12296b;font-weight:600}
main{max-width:52rem;margin:0 auto;padding:1rem}
section.card{background:#fff;border:1px solid #dfe3e8;border-radius:10px;
padding:1rem;margin:0 0 1rem}
h1{font-size:1.35rem;margin:0 0 .75rem}
h2{font-size:1.05rem;margin:0 0 .6rem}
.money{font-variant-numeric:tabular-nums;white-space:nowrap}
.headline{font-size:2rem;font-weight:700;line-height:1.2}
.secondary{color:#5b6472;font-size:.9rem}
label{display:block;font-weight:600;font-size:.875rem;margin:.6rem 0 .2rem}
input,select{width:100%;padding:.55rem .6rem;font:inherit;color:inherit;
background:#fff;border:1px solid #98a1b0;border-radius:6px}
input:focus-visible,select:focus-visible,button:focus-visible,a:focus-visible,
button:focus,input:focus{outline:3px solid #1b4fd8;outline-offset:2px}
button{margin-top:.8rem;padding:.55rem 1rem;font:inherit;font-weight:600;
color:#fff;background:#1b4fd8;border:1px solid #14399e;border-radius:6px;cursor:pointer}
button.secondary-action{background:#fff;color:#16181d;border-color:#98a1b0}
form.inline{display:flex;flex-wrap:wrap;gap:.6rem;align-items:flex-end}
form.inline>div{flex:1 1 8rem;min-width:0}
ul.list{list-style:none;margin:0;padding:0}
ul.list>li{border-top:1px solid #e6e9ee;padding:.7rem 0;display:flex;
flex-wrap:wrap;gap:.5rem;align-items:center}
ul.list>li:first-child{border-top:0}
.row-main{flex:1 1 14rem;min-width:0}
.pill{display:inline-block;padding:.1rem .5rem;border-radius:999px;
font-size:.75rem;font-weight:600;background:#eceff3;color:#39414e}
.pill.open{background:#e4f3e9;color:#155c31}
.pill.paid,.pill.captured{background:#e8eeff;color:#12296b}
.pill.declined,.pill.cancelled,.pill.voided,.pill.expired{background:#f3e6e6;color:#7a2020}
.pill.pending{background:#fbf0d9;color:#6b4a06}
.empty{color:#5b6472;font-style:italic}
.err{color:#7a2020;font-weight:600;margin-top:.6rem}
.err:empty{display:none}
.grid3{display:flex;flex-wrap:wrap;gap:1rem}
.grid3>div{flex:1 1 9rem;min-width:0}
@media (prefers-color-scheme:dark){
body{background:#14161a;color:#e8eaee}
header.top,section.card{background:#1c1f25;border-color:#333842}
input,select{background:#14161a;border-color:#4a5261;color:#e8eaee}
.secondary,.empty{color:#a2abb8}
nav.top a{color:#e8eaee}nav.top a[aria-current=page]{background:#24304d;color:#cfe0ff}
}
"""


_NAV = (
    ("home", "/", "Wallet"),
    ("requests", "/requests", "Requests"),
    ("authorizations", "/authorizations", "Authorisations"),
)


def _layout(title: str, body: str, handle: str | None, display: str | None) -> str:
    here = title.lower()
    links = []
    for key, href, label in _NAV:
        current = ' aria-current="page"' if label.lower().startswith(here[:5]) else ""
        links.append(f'<a href="{href}" data-testid="nav-{key}"{current}>{label}</a>')
    nav = "".join(links) if handle else ""
    who = ""
    if handle:
        who = (
            f'<span class="who" data-testid="current-user">{esc(display)}</span>'
            f'<span class="secondary" data-testid="current-handle">{esc(handle)}</span>'
            '<form method="post" action="/ui/logout" style="margin:0">'
            '<button type="submit" class="secondary-action" '
            'data-testid="logout-button">Sign out</button></form>'
        )
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="icon" href="data:,">'
        f"<title>{esc(title)} - Pocketful</title>"
        f"<style>{_CSS}</style></head><body>"
        f'<header class="top"><strong>Pocketful</strong>{who}'
        f'<nav class="top">{nav}</nav></header>'
        f"<main>{body}</main></body></html>"
    )


def _field(label: str, testid: str, name: str, *, kind: str = "text",
           placeholder: str = "", value: str = "") -> str:
    return (
        f"<label for=\"{esc(testid)}\">{esc(label)}</label>"
        f'<input id="{esc(testid)}" data-testid="{esc(testid)}" name="{esc(name)}" '
        f'type="{kind}" placeholder="{esc(placeholder)}" value="{esc(value)}" '
        f'autocomplete="off">'
    )


# ----------------------------------------------------------------------
# screens
# ----------------------------------------------------------------------


def _wallet_numbers(user: dict, *, headline_available: bool) -> str:
    cur = user.get("currency", "EUR")
    mu = int(user.get("minor_units", 2))
    held = int(user.get("held", 0))
    avail = int(user.get("available", int(user.get("total", 0)) - held))
    total = int(user.get("total", int(user.get("balance", 0))))
    cells = [
        '<div><span class="secondary">Total</span><div class="money" '
        f'data-testid="wallet-balance" data-amount="{total}">'
        f"{esc(format_amount(total, cur, mu))}</div></div>"
    ]
    if headline_available:
        cells.append(
            '<div><span class="secondary">Available to spend</span>'
            f'<div class="money headline" data-testid="wallet-available" '
            f'data-amount="{avail}">{esc(format_amount(avail, cur, mu))}</div></div>'
        )
    if held:
        cells.append(
            '<div><span class="secondary">Held</span>'
            f'<div class="money" data-testid="wallet-held" data-amount="{held}">'
            f"{esc(format_amount(held, cur, mu))}</div></div>"
        )
    return f'<div class="grid3">{"".join(cells)}</div>'


def render_signed_out(auth_error: str = "") -> str:
    """The signed-out landing page.

    A browser following a bare link sends no bearer token. Returning 401 for
    ``Accept: text/html`` would make negotiation look broken, so this renders
    a sign-in prompt instead. No ``current-user`` testid: the spec scopes that
    to "when signed in".

    ``auth-error`` is emitted only when there is one, per the spec's wording,
    so its mere presence is never mistaken for a failure.
    """
    err = (
        f'<p class="err" data-testid="auth-error" role="alert">{esc(auth_error)}</p>'
        if auth_error
        else ""
    )
    body = (
        '<section class="card"><h1>Pocketful</h1>'
        '<p class="secondary">Sign in to see your balance, pay someone, '
        "request money and review authorisations.</p>"
        '<form method="post" action="/ui/login">'
        '<label for="login-email">Email</label>'
        '<input id="login-email" data-testid="login-email" name="email" '
        'type="email" autocomplete="username">'
        '<label for="login-password">Password</label>'
        '<input id="login-password" data-testid="login-password" name="password" '
        'type="password" autocomplete="current-password">'
        f'<div><button type="submit" data-testid="login-submit">Sign in</button></div>'
        f"{err}"
        '<p class="secondary">No account yet? <a href="/signup">Create one</a>.</p>'
        "</form></section>"
    )
    return _layout("Sign in", body, None, None)


def render_login(auth_error: str = "", email: str = "") -> str:
    """``/login`` as its own route, so the spec's route table is honoured."""
    err = (
        f'<p class="err" data-testid="auth-error" role="alert">{esc(auth_error)}</p>'
        if auth_error
        else ""
    )
    body = (
        '<section class="card"><h1>Sign in</h1>'
        '<form method="post" action="/ui/login">'
        '<label for="login-email">Email</label>'
        f'<input id="login-email" data-testid="login-email" name="email" type="email" '
        f'autocomplete="username" value="{esc(email)}">'
        '<label for="login-password">Password</label>'
        '<input id="login-password" data-testid="login-password" name="password" '
        'type="password" autocomplete="current-password">'
        f'<div><button type="submit" data-testid="login-submit">Sign in</button></div>'
        f"{err}"
        '<p class="secondary">No account yet? <a href="/signup">Create one</a>.</p>'
        "</form></section>"
    )
    return _layout("Sign in", body, None, None)


def render_signup(
    auth_error: str = "", email: str = "", display_name: str = ""
) -> str:
    err = (
        f'<p class="err" data-testid="auth-error" role="alert">{esc(auth_error)}</p>'
        if auth_error
        else ""
    )
    body = (
        '<section class="card"><h1>Create your account</h1>'
        '<form method="post" action="/ui/signup">'
        '<label for="signup-email">Email</label>'
        f'<input id="signup-email" data-testid="signup-email" name="email" type="email" '
        f'autocomplete="username" value="{esc(email)}">'
        '<label for="signup-display-name">Display name</label>'
        f'<input id="signup-display-name" data-testid="signup-display-name" '
        f'name="display_name" value="{esc(display_name)}">'
        '<label for="signup-password">Password</label>'
        '<input id="signup-password" data-testid="signup-password" name="password" '
        'type="password" autocomplete="new-password">'
        f'<div><button type="submit" data-testid="signup-submit">Create account</button></div>'
        f"{err}"
        '<p class="secondary">Already registered? <a href="/login">Sign in</a>.</p>'
        "</form></section>"
    )
    return _layout("Sign up", body, None, None)


def render_split(
    user: dict,
    *,
    amount: str = "",
    handles: str = "",
    note: str = "",
    shares: list[dict] | None = None,
    split_error: str = "",
    nonce: str = "",
) -> str:
    """``/split``: the form plus a preview of the shares before posting.

    ``shares`` is computed by the caller with ``operations.split_shares`` - the
    same function ``POST /splits`` uses - so the preview cannot disagree with
    what the server will actually do.
    """
    cur = str(user.get("currency", "EUR"))
    mu = int(user.get("minor_units", 2))
    if shares is None:
        preview = (
            '<p class="secondary" data-testid="split-preview">Enter an amount and '
            "the handles, in order, to see each share.</p>"
        )
    else:
        rows = "".join(
            f'<li><span class="money" data-testid="split-share-{esc(str(s["handle"]))}">'
            f"{esc(format_amount(int(s['amount']), cur, mu))}</span>"
            f'<span class="secondary"> to {esc(str(s["handle"]))}</span></li>'
            for s in shares
        )
        preview = f'<ul class="list" data-testid="split-preview">{rows}</ul>'

    err = (
        f'<p class="err" data-testid="split-error" role="alert">{esc(split_error)}</p>'
        if split_error
        else ""
    )
    form = (
        '<h2>Split a bill</h2>'
        f'<form method="get" action="/split" data-testid="split-preview-form">'
        '<label for="split-amount">Amount</label>'
        f'<input id="split-amount" data-testid="split-amount" name="amount" '
        f'placeholder="15.00" value="{esc(amount)}">'
        '<label for="split-handles">Handles, in order</label>'
        f'<input id="split-handles" data-testid="split-handles" name="handles" '
        f'placeholder="ada, bob, cy" value="{esc(handles)}">'
        '<label for="split-note">Note (optional)</label>'
        f'<input id="split-note" data-testid="split-note" name="note" '
        f'value="{esc(note)}">'
        '<div><button type="submit" data-testid="split-preview-submit">'
        "Preview shares</button></div>"
        "</form>"
        + preview
        + '<form method="post" action="/ui/split">'
        + _hidden("nonce", nonce)
        + _hidden("amount", amount)
        + _hidden("handles", handles)
        + _hidden("note", note)
        + f'<div><button type="submit" data-testid="split-submit">'
        f"Send the split</button></div>"
        f"{err}"
        + "</form>"
    )
    return _layout("Split", f'<section class="card">{form}</section>', user, None)


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def render_home(*, user: dict, activity: list[dict], visibility: dict,
                nonce: str = "",
                pay_values: dict | None = None,
                request_values: dict | None = None,
                pay_error: str = "", pay_uncertain: str = "",
                request_error: str = "") -> str:
    cur = user.get("currency", "EUR")
    mu = int(user.get("minor_units", 2))
    handle = str(user.get("handle", ""))
    pv = pay_values or {}
    rv = request_values or {}

    pay_form = (
        '<h2>Pay someone</h2><form method="post" action="/ui/pay">'
        + _hidden("nonce", nonce)
        + _field("Handle", "pay-handle", "to", placeholder="ada",
                 value=str(pv.get("to", "")))
        + _field("Amount", "pay-amount", "amount", placeholder="15.00",
                 value=str(pv.get("amount", "")))
        + _field("Note (optional)", "pay-note", "note", value=str(pv.get("note", "")))
        + '<label for="pay-visibility">Visibility</label>'
        + '<select id="pay-visibility" data-testid="pay-visibility" name="visibility">'
        f'<option value="private"{" selected" if pv.get("visibility") == "private" else ""}>Private</option>'
        f'<option value="public"{" selected" if pv.get("visibility") != "private" else ""}>Public</option>'
        "</select>"
        + '<div><button type="submit" data-testid="pay-submit">Send payment</button>'
        # SPEC: a refresh that keeps the pay form. It is a GET on this same
        # route, so the browser resends the pay fields as the query string and
        # the re-render keeps them. Being a navigation, it cannot race itself,
        # so "latest refresh wins" holds by construction: there is no second
        # in-flight read to land late over a newer one.
        '<button type="submit" data-testid="wallet-refresh" formmethod="get" '
        'formaction="/">Refresh balance</button></div>'
        f'<p class="err" data-testid="pay-error" role="alert">{esc(pay_error)}</p>'
        f'<p class="warn" data-testid="pay-uncertain" role="alert">{esc(pay_uncertain)}</p>'
        "</form>"
    )

    req_form = (
        '<h2>Request money</h2><form method="post" action="/ui/request">'
        + _hidden("nonce", nonce)
        + _field("Handle", "request-handle", "to", placeholder="ada",
                 value=str(rv.get("to", "")))
        + _field("Amount", "request-amount", "amount", placeholder="15.00",
                 value=str(rv.get("amount", "")))
        + _field("Note (optional)", "request-note", "note",
                 value=str(rv.get("note", "")))
        + '<div><button type="submit" data-testid="request-submit">Send request</button></div>'
        f'<p class="err" data-testid="request-error" role="alert">{esc(request_error)}</p>'
        "</form>"
    )

    rows = []
    for item in activity:
        pid = item.get("related_id") or item.get("id")
        vis = visibility.get(pid)
        vis_attr = f' data-visibility="{esc(vis)}"' if vis else ""
        parties = " and ".join(
            h for h in (item.get("actor"), item.get("counterparty")) if h
        )
        rows.append(
            f'<li data-testid="activity-item-{esc(pid)}"{vis_attr}>'
            f'<div class="row-main"><span data-testid="activity-parties-{esc(pid)}">'
            f"{esc(parties)}</span> "
            f'<span class="money" data-testid="activity-amount-{esc(pid)}">'
            f'{esc(format_amount(item.get("amount", 0), cur, mu))}</span></div>'
            f'<span data-testid="activity-note-{esc(pid)}">{esc(item.get("memo") or "")}</span>'
            f'<span class="pill">{esc(item.get("direction", ""))}</span></li>'
        )
    feed = (
        f'<ul class="list" data-testid="activity-list">{"".join(rows)}</ul>'
        if rows
        else '<p class="empty" data-testid="empty-activity">No activity yet.</p>'
    )

    body = (
        f'<section class="card"><h1>Your wallet</h1>{_wallet_numbers(user, headline_available=False)}</section>'
        f'<section class="card">{pay_form}</section>'
        f'<section class="card">{req_form}</section>'
        f'<section class="card"><h2>Activity</h2>{feed}</section>'
    )
    return _layout("Wallet", body, handle, user.get("display_name"))


def render_requests(*, user: dict, items: list[dict], nonce: str = "",
             request_error: str = "") -> str:
    cur = user.get("currency", "EUR")
    mu = int(user.get("minor_units", 2))
    incoming, outgoing = [], []
    for req in items:
        pid = req.get("id")
        status = req.get("status", "")
        amt = (
            f'<span class="money" data-testid="request-amount-{esc(pid)}">'
            f'{esc(format_amount(req.get("amount", 0), cur, mu))}</span>'
        )
        acts = []
        if status == "open" and req.get("direction") == "incoming":
            acts.append(
                f'<button type="submit" form="f-{esc(pid)}" data-testid="request-pay-{esc(pid)}">Pay</button>'
            )
            acts.append(
                f'<button type="submit" form="g-{esc(pid)}" class="secondary-action" '
                f'data-testid="request-decline-{esc(pid)}">Decline</button>'
            )
        if status == "open" and req.get("direction") == "outgoing":
            acts.append(
                f'<button type="submit" form="h-{esc(pid)}" class="secondary-action" '
                f'data-testid="request-cancel-{esc(pid)}">Cancel</button>'
            )
        forms = ""
        if status == "open" and req.get("direction") == "incoming":
            forms = (
                f'<form id="f-{esc(pid)}" method="post" action="/ui/requests/{esc(pid)}/pay">{_hidden("nonce", nonce)}</form>'
                f'<form id="g-{esc(pid)}" method="post" action="/ui/requests/{esc(pid)}/decline">{_hidden("nonce", nonce)}</form>'
            )
        if status == "open" and req.get("direction") == "outgoing":
            forms = f'<form id="h-{esc(pid)}" method="post" action="/ui/requests/{esc(pid)}/cancel">{_hidden("nonce", nonce)}</form>'
        row = (
            f'<li data-testid="request-item-{esc(pid)}" data-status="{esc(status)}">'
            f'<div class="row-main">{amt} <span class="secondary">'
            f'{esc(req.get("from_handle") or req.get("from"))} to '
            f'{esc(req.get("to_handle") or req.get("to"))}</span></div>'
            f'<span class="pill {esc(status)}">{esc(status)}</span>'
            f'{"".join(acts)}{forms}</li>'
        )
        (incoming if req.get("direction") == "incoming" else outgoing).append(row)

    def block(testid: str, title: str, rows: list[str], empty: str) -> str:
        # The container is a required testid and is always present, even when
        # the list is empty; only `empty-requests` is conditional.
        inner = (
            "".join(rows)
            if rows
            else f'<li class="empty">{esc(empty)}</li>'
        )
        return (
            f'<section class="card"><h2>{esc(title)}</h2>'
            f'<ul class="list" data-testid="{testid}">{inner}</ul></section>'
        )

    body = (
        block("incoming-list", "Incoming requests", incoming, "Nothing incoming.")
        + block("outgoing-list", "Outgoing requests", outgoing, "Nothing outgoing.")
        + f'<p class="err" data-testid="request-error" role="alert">{esc(request_error)}</p>'
    )
    if not items:
        body = '<p class="empty" data-testid="empty-requests">No requests yet.</p>' + body
    return _layout(
        "Requests", body, str(user.get("handle", "")), user.get("display_name")
    )


def render_authorizations(*, user: dict, items: list[dict], nonce: str = "",
                       authorize_error: str = "", authorization_error: str = "") -> str:
    cur = user.get("currency", "EUR")
    mu = int(user.get("minor_units", 2))
    rows = []
    for a in items:
        aid = a.get("id")
        status = a.get("status", "")
        amt = (
            f'<span class="money" data-testid="authorization-amount-{esc(aid)}">'
            f'{esc(format_amount(a.get("amount", 0), cur, mu))}</span>'
        )
        parts = [amt]
        if status == "captured":
            parts.append(
                f'<span class="money" data-testid="authorization-captured-{esc(aid)}">'
                f'{esc(format_amount(a.get("captured_amount", 0), cur, mu))} captured</span>'
            )
        parts.append(
            f'<span class="secondary" data-testid="authorization-expires-{esc(aid)}">'
            f'{esc(a.get("expires_at", ""))}</span>'
        )
        if status == "open" and a.get("direction") == "incoming":
            parts.append(
                f'<input type="text" inputmode="decimal" '
                f'data-testid="authorization-capture-amount-{esc(aid)}" '
                f'name="amount" form="c-{esc(aid)}" '
                f'value="{esc(format_amount(a.get("remaining_amount", 0), cur, mu).split(" ")[0])}">'
            )
            parts.append(
                f'<button type="submit" form="c-{esc(aid)}" '
                f'data-testid="authorization-capture-{esc(aid)}">Capture</button>'
            )
        if status == "open" and a.get("direction") == "outgoing":
            parts.append(
                f'<button type="submit" form="v-{esc(aid)}" class="secondary-action" '
                f'data-testid="authorization-void-{esc(aid)}">Void</button>'
            )
        forms = ""
        if status == "open" and a.get("direction") == "incoming":
            forms = f'<form id="c-{esc(aid)}" method="post" action="/ui/authorizations/{esc(aid)}/capture">{_hidden("nonce", nonce)}</form>'
        if status == "open" and a.get("direction") == "outgoing":
            forms = f'<form id="v-{esc(aid)}" method="post" action="/ui/authorizations/{esc(aid)}/void">{_hidden("nonce", nonce)}</form>'
        rows.append(
            f'<li data-testid="authorization-item-{esc(aid)}" data-status="{esc(status)}">'
            f'<div class="row-main">{"".join(parts)}</div>'
            f'<span class="pill {esc(status)}">{esc(status)}</span>{forms}</li>'
        )

    lst = (
        f'<ul class="list" data-testid="authorization-list">{"".join(rows)}</ul>'
        if rows
        else '<p class="empty" data-testid="empty-authorizations">No authorisations yet.</p>'
    )
    form = (
        "<h2>Reserve money</h2>"
        + '<form method="post" action="/ui/authorizations">'
        + _hidden("nonce", nonce)
        + _field("Handle", "authorize-handle", "to", placeholder="ada")
        + _field("Amount", "authorize-amount", "amount", placeholder="15.00")
        + _field("Note (optional)", "authorize-note", "note")
        + '<label for="authorize-visibility">Visibility</label>'
        + '<select id="authorize-visibility" data-testid="authorize-visibility" name="visibility">'
        '<option value="private">Private</option>'
        '<option value="public">Public</option></select>'
        '<div><button type="submit" data-testid="authorize-submit">Reserve</button></div>'
        f'<p class="err" data-testid="authorize-error" role="alert">{esc(authorize_error)}</p></form>'
    )
    body = (
        f'<section class="card"><h1>Authorisations</h1>'
        f'{_wallet_numbers(user, headline_available=True)}</section>'
        f'<section class="card">{form}</section>'
        f'<section class="card"><h2>Your authorisations</h2>{lst}</section>'
        f'<p class="err" data-testid="authorization-error" role="alert">{esc(authorization_error)}</p>'
    )
    return _layout(
        "Authorisations", body, str(user.get("handle", "")), user.get("display_name")
    )