#!/usr/bin/env python3
"""Pocketful Stage 1 API - Full implementation."""

import json
import uuid
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import re

# Third-party deps (needs installation)
try:
    import bcrypt
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "bcrypt"])
    import bcrypt

# ============== In-Memory Storage ==============

class Storage:
    """Thread-safe in-memory storage."""

    def __init__(self):
        self._lock = threading.RLock()
        self._users = {}              # handle -> user dict
        self._sessions = {}           # session_token -> handle
        self._balances = {}            # handle -> balance (minor units)
        self._requests = {}            # id -> request dict
        self._splits = {}              # id -> split dict
        self._activity = []            # list of activity dicts
        self._idempotency = {}         # key -> response dict
        self._seed_total = 0

    def reset(self, fixture):
        with self._lock:
            self._users.clear()
            self._sessions.clear()
            self._balances.clear()
            self._requests.clear()
            self._splits.clear()
            self._activity.clear()
            self._idempotency.clear()

            # Load users and balances
            total = 0
            for u in fixture.get('users', []):
                handle = u['handle']
                password_hash = u.get('password_hash', bcrypt.hashpw(b'correct horse', bcrypt.gensalt()).decode())
                self._users[handle] = {
                    'handle': handle,
                    'email': u.get('email', f'{handle}@test.com'),
                    'password_hash': password_hash,
                    'display_name': u.get('display_name', handle),
                    'created_at': datetime.utcnow().isoformat()
                }
                balance = u.get('balance', 0)
                self._balances[handle] = balance
                total += balance

            self._seed_total = total
            return {'status': 'ok', 'seed_total': total}

    def export(self):
        with self._lock:
            return {
                'users': list(self._users.values()),
                'balances': dict(self._balances),
                'requests': list(self._requests.values()),
                'splits': list(self._splits.values()),
                'activity': list(self._activity),
                'seed_total': self._seed_total
            }

    def import_(self, data):
        with self._lock:
            self._users = {u['handle']: u for u in data.get('users', [])}
            self._balances = data.get('balances', {})
            self._requests = {r['id']: r for r in data.get('requests', [])}
            self._splits = {s['id']: s for s in data.get('splits', [])}
            self._activity = data.get('activity', [])
            self._seed_total = data.get('seed_total', 0)
            return {'status': 'ok'}

    def create_user(self, email, password, display_name):
        handle = email.split('@')[0].lower()
        handle = re.sub(r'[^a-z0-9_]', '', handle)
        handle = handle[:20]
        if not handle or not re.match(r'^[a-z0-9_]{1,20}$', handle):
            return None, 'invalid_handle'
        if handle in self._users:
            return None, 'user_exists'

        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = {
            'handle': handle,
            'email': email,
            'password_hash': password_hash,
            'display_name': display_name,
            'created_at': datetime.utcnow().isoformat()
        }
        self._users[handle] = user
        self._balances[handle] = 0
        return user, None

    def authenticate(self, email, password):
        handle = email.split('@')[0].lower()
        handle = re.sub(r'[^a-z0-9_]', '', handle)
        handle = handle[:20]
        user = self._users.get(handle)
        if not user:
            return None
        if not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
            return None
        return user

    def create_session(self, handle):
        token = str(uuid.uuid4())
        self._sessions[token] = handle
        return token

    def get_session(self, token):
        return self._sessions.get(token)

    def get_balance(self, handle):
        return self._balances.get(handle, 0)

    def add_activity(self, event_type, data):
        with self._lock:
            self._activity.append({
                'type': event_type,
                'data': data,
                'timestamp': datetime.utcnow().isoformat()
            })
            # Keep last 1000
            if len(self._activity) > 1000:
                self._activity = self._activity[-1000:]

    def get_activity(self, handle):
        return [a for a in self._activity if a['data'].get('user_handle') == handle]


# Global storage instance
storage = Storage()

# ============== Helpers ==============

def parse_auth(handler):
    """Parse Authorization header, return (handle or None, error)."""
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None, 'missing_auth'
    token = auth[7:]
    handle = storage.get_session(token)
    if not handle:
        return None, 'invalid_token'
    return handle, None

def require_auth(handler):
    """Require authentication, return (handle, error)."""
    handle, err = parse_auth(handler)
    if err:
        return None, err
    return handle, None

def require_operator(handler):
    """Require operator role."""
    handle, err = require_auth(handler)
    if err:
        return None, err
    user = storage._users.get(handle)
    if not user or user.get('role') != 'operator':
        return None, 'operator_required'
    return handle, None

def validate_handle(s):
    """Validate handle format."""
    return bool(re.match(r'^[a-z0-9_]{1,20}$', s))

def validate_idempotency_key(key, handle):
    """Check/store idempotency key. Returns (existing_response or None, error)."""
    if not key:
        return None, 'idempotency_required'
    full_key = f"{handle}:{key}"
    if full_key in storage._idempotency:
        return storage._idempotency[full_key], None
    return None, None

def store_idempotency(key, handle, response):
    """Store idempotency key with response."""
    full_key = f"{handle}:{key}"
    storage._idempotency[full_key] = response

def read_json(handler):
    """Read JSON from request body."""
    content_length = int(handler.headers.get('Content-Length', 0))
    if content_length == 0:
        return {}
    body = handler.rfile.read(content_length)
    return json.loads(body.decode())

def send_json(handler, status, data, error_code=None):
    """Send JSON response."""
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    
    if 400 <= status < 600 and error_code:
        # Error response in spec format
        response = {"error": {"code": error_code, "message": data.get('error', str(data))}}
        handler.wfile.write(json.dumps(response).encode())
    else:
        handler.wfile.write(json.dumps(data).encode())

def send_204(handler):
    """Send 204 No Content."""
    handler.send_response(204)
    handler.end_headers()

def get_path_param(handler, pattern):
    """Extract path parameter."""
    match = re.match(pattern, handler.path)
    if match:
        return match.group(1)
    return None

# ============== Request Handlers ==============

def handle_health(handler):
    send_json(handler, 200, {'status': 'ok'})

def handle_test_reset(handler):
    data = read_json(handler)
    fixture = data  # The entire body is the fixture
    try:
        storage.reset(fixture)
        send_204(handler)
    except Exception as e:
        send_json(handler, 422, {'error': str(e)}, 'invalid_fixture')

def handle_test_export(handler):
    data = storage.export()
    send_json(handler, 200, data)

def handle_test_import(handler):
    data = read_json(handler)
    try:
        storage.import_(data)
        send_204(handler)
    except Exception as e:
        send_json(handler, 400, {'error': str(e)}, 'import_failed')

def handle_signup(handler):
    data = read_json(handler)
    email = data.get('email', '').strip()
    password = data.get('password', '')
    display_name = data.get('display_name', '').strip()

    if not email or not password or not display_name:
        send_json(handler, 400, {'error': 'email, password, display_name required'}, 'missing_fields')
        return

    user, err = storage.create_user(email, password, display_name)
    if err:
        send_json(handler, 400, {'error': err}, err)
        return

    token = storage.create_session(user['handle'])
    send_json(handler, 201, {
        'user': {'handle': user['handle'], 'email': user['email'], 'display_name': user['display_name']},
        'token': token
    })

def handle_login(handler):
    data = read_json(handler)
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not email or not password:
        send_json(handler, 400, {'error': 'email and password required'}, 'missing_fields')
        return

    user = storage.authenticate(email, password)
    if not user:
        send_json(handler, 401, {'error': 'Invalid credentials'}, 'invalid_credentials')
        return

    token = storage.create_session(user['handle'])
    send_json(handler, 200, {
        'user': {'handle': user['handle'], 'email': user['email'], 'display_name': user['display_name']},
        'token': token
    })

def handle_me(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    user = storage._users.get(handle)
    balance = storage.get_balance(handle)
    send_json(handler, 200, {
        'handle': user['handle'],
        'email': user['email'],
        'display_name': user['display_name'],
        'balance': balance
    })

def handle_payments(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    # Check idempotency
    idempotency_key = handler.headers.get('Idempotency-Key')
    existing, idempotency_err = validate_idempotency_key(idempotency_key, handle)
    if idempotency_err:
        send_json(handler, 400, {'error': idempotency_err}, idempotency_err)
        return
    if existing:
        handler.send_response(existing['status'])
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(existing['body'].encode())
        return

    data = read_json(handler)
    to_handle = data.get('to_handle') or data.get('to')
    amount = data.get('amount', 0)

    if not to_handle or amount <= 0:
        send_json(handler, 400, {'error': 'to_handle and positive amount required'}, 'missing_fields')
        return

    if not validate_handle(to_handle):
        send_json(handler, 400, {'error': 'Invalid handle'}, 'invalid_handle')
        return

    from_balance = storage.get_balance(handle)
    if from_balance < amount:
        send_json(handler, 422, {'error': 'Insufficient balance'}, 'insufficient_balance')
        return

    # Execute payment
    storage._balances[handle] = from_balance - amount
    storage._balances[to_handle] = storage.get_balance(to_handle) + amount

    payment_id = str(uuid.uuid4())[:8]
    payment = {
        'id': payment_id,
        'from': handle,
        'to': to_handle,
        'amount': amount,
        'timestamp': datetime.utcnow().isoformat()
    }

    storage.add_activity('payment', {
        'user_handle': handle,
        'payment': payment
    })

    response = {'payment': payment}
    response_body = json.dumps(response)
    store_idempotency(idempotency_key, handle, {'status': 201, 'body': response_body})
    send_json(handler, 201, payment)

def handle_requests_list(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    parsed = urlparse(handler.path)
    params = parse_qs(parsed.query)

    direction = params.get('direction', [None])[0]
    status_filter = params.get('status', [None])[0]

    user_requests = [r for r in storage._requests.values() 
                     if r.get('from') == handle or r.get('to') == handle]

    if direction == 'sent':
        user_requests = [r for r in user_requests if r.get('from') == handle]
    elif direction == 'received':
        user_requests = [r for r in user_requests if r.get('to') == handle]

    if status_filter:
        user_requests = [r for r in user_requests if r.get('status') == status_filter]

    send_json(handler, 200, {'requests': user_requests})

def handle_requests_create(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    # Check idempotency
    idempotency_key = handler.headers.get('Idempotency-Key')
    existing, idempotency_err = validate_idempotency_key(idempotency_key, handle)
    if idempotency_err:
        send_json(handler, 400, {'error': idempotency_err}, idempotency_err)
        return
    if existing:
        handler.send_response(existing['status'])
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(existing['body'].encode())
        return

    data = read_json(handler)
    # Support both payer_handle and to (for compatibility)
    payer_handle = data.get('payer_handle') or data.get('to')
    amount = data.get('amount', 0)

    if not payer_handle or amount <= 0:
        send_json(handler, 400, {'error': 'payer_handle and positive amount required'}, 'missing_fields')
        return

    if not validate_handle(payer_handle):
        send_json(handler, 400, {'error': 'Invalid handle'}, 'invalid_handle')
        return

    if payer_handle not in storage._users:
        send_json(handler, 400, {'error': 'Recipient not found'}, 'user_not_found')
        return

    request_id = str(uuid.uuid4())[:8]
    request = {
        'id': request_id,
        'from': handle,
        'to': payer_handle,
        'amount': amount,
        'status': 'pending',
        'created_at': datetime.utcnow().isoformat()
    }
    storage._requests[request_id] = request

    storage.add_activity('request_created', {
        'user_handle': handle,
        'request': request
    })

    response = {'request': request}
    response_body = json.dumps(response)
    store_idempotency(idempotency_key, handle, {'status': 201, 'body': response_body})
    send_json(handler, 201, request)

def handle_requests_pay(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    request_id = get_path_param(handler, r'^/requests/([^/]+)/pay$')
    if not request_id:
        send_json(handler, 404, {'error': 'Not found'}, 'not_found')
        return

    # Check idempotency
    idempotency_key = handler.headers.get('Idempotency-Key')
    existing, idempotency_err = validate_idempotency_key(idempotency_key, handle)
    if idempotency_err:
        send_json(handler, 400, {'error': idempotency_err}, idempotency_err)
        return
    if existing:
        handler.send_response(existing['status'])
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(existing['body'].encode())
        return

    request = storage._requests.get(request_id)
    if not request:
        send_json(handler, 404, {'error': 'Request not found'}, 'not_found')
        return

    if request['to'] != handle:
        send_json(handler, 403, {'error': 'Not authorized'}, 'forbidden')
        return

    if request['status'] != 'pending':
        send_json(handler, 400, {'error': f'Cannot pay request with status {request["status"]}'}, 'invalid_state')
        return

    from_handle = request['from']
    amount = request['amount']

    from_balance = storage.get_balance(from_handle)
    if from_balance < amount:
        send_json(handler, 422, {'error': 'Insufficient balance'}, 'insufficient_balance')
        return

    # Execute payment
    storage._balances[from_handle] = from_balance - amount
    storage._balances[handle] = storage.get_balance(handle) + amount

    request['status'] = 'paid'
    request['paid_at'] = datetime.utcnow().isoformat()

    storage.add_activity('request_paid', {
        'user_handle': handle,
        'request': request
    })

    response = {'request': request}
    response_body = json.dumps(response)
    store_idempotency(idempotency_key, handle, {'status': 200, 'body': response_body})
    send_json(handler, 200, request)

def handle_requests_decline(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    request_id = get_path_param(handler, r'^/requests/([^/]+)/decline$')
    if not request_id:
        send_json(handler, 404, {'error': 'Not found'}, 'not_found')
        return

    request = storage._requests.get(request_id)
    if not request:
        send_json(handler, 404, {'error': 'Request not found'}, 'not_found')
        return

    if request['to'] != handle:
        send_json(handler, 403, {'error': 'Not authorized'}, 'forbidden')
        return

    if request['status'] != 'pending':
        send_json(handler, 400, {'error': f'Cannot decline request with status {request["status"]}'}, 'invalid_state')
        return

    request['status'] = 'declined'
    request['declined_at'] = datetime.utcnow().isoformat()

    storage.add_activity('request_declined', {
        'user_handle': handle,
        'request': request
    })

    send_json(handler, 200, request)

def handle_requests_cancel(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    request_id = get_path_param(handler, r'^/requests/([^/]+)/cancel$')
    if not request_id:
        send_json(handler, 404, {'error': 'Not found'}, 'not_found')
        return

    request = storage._requests.get(request_id)
    if not request:
        send_json(handler, 404, {'error': 'Request not found'}, 'not_found')
        return

    if request['from'] != handle:
        send_json(handler, 403, {'error': 'Not authorized'}, 'forbidden')
        return

    if request['status'] != 'pending':
        send_json(handler, 400, {'error': f'Cannot cancel request with status {request["status"]}'}, 'invalid_state')
        return

    request['status'] = 'cancelled'
    request['cancelled_at'] = datetime.utcnow().isoformat()

    storage.add_activity('request_cancelled', {
        'user_handle': handle,
        'request': request
    })

    send_json(handler, 200, request)

def handle_splits_create(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    # Check idempotency
    idempotency_key = handler.headers.get('Idempotency-Key')
    existing, idempotency_err = validate_idempotency_key(idempotency_key, handle)
    if idempotency_err:
        send_json(handler, 400, {'error': idempotency_err}, idempotency_err)
        return
    if existing:
        handler.send_response(existing['status'])
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(existing['body'].encode())
        return

    data = read_json(handler)
    participants = data.get('participants', [])
    amount = data.get('amount', 0)
    description = data.get('description', '')

    if len(participants) < 2 or amount <= 0:
        send_json(handler, 400, {'error': 'At least 2 participants and positive amount required'}, 'missing_fields')
        return

    # Validate all handles
    for p in participants:
        if not validate_handle(p):
            send_json(handler, 400, {'error': f'Invalid handle: {p}'}, 'invalid_handle')
            return

    # Equal split with extra units to first participants
    n = len(participants)
    base = amount // n
    extra = amount % n

    splits = []
    for i, p in enumerate(participants):
        share = base + (1 if i < extra else 0)
        splits.append({'to': p, 'amount': share})

    split_id = str(uuid.uuid4())[:8]
    split = {
        'id': split_id,
        'from': handle,
        'amount': amount,
        'description': description,
        'splits': splits,
        'status': 'pending',
        'created_at': datetime.utcnow().isoformat()
    }
    storage._splits[split_id] = split

    storage.add_activity('split_created', {
        'user_handle': handle,
        'split': split
    })

    response = {'split': split}
    response_body = json.dumps(response)
    store_idempotency(idempotency_key, handle, {'status': 201, 'body': response_body})
    send_json(handler, 201, split)

def handle_activity(handler):
    handle, err = require_auth(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    activities = storage.get_activity(handle)
    send_json(handler, 200, {'activities': activities})

def handle_settlements(handler):
    handle, err = require_operator(handler)
    if err:
        send_json(handler, 401, {'error': err}, err)
        return

    # Check idempotency
    idempotency_key = handler.headers.get('Idempotency-Key')
    existing, idempotency_err = validate_idempotency_key(idempotency_key, handle)
    if idempotency_err:
        send_json(handler, 400, {'error': idempotency_err}, idempotency_err)
        return
    if existing:
        handler.send_response(existing['status'])
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(existing['body'].encode())
        return

    data = read_json(handler)
    settlements = data.get('settlements', [])

    if not settlements:
        send_json(handler, 400, {'error': 'settlements required'}, 'missing_fields')
        return

    results = []
    for s in settlements:
        from_handle = s.get('from')
        to_handle = s.get('to')
        amount = s.get('amount', 0)

        if not from_handle or not to_handle or amount <= 0:
            results.append({'error': 'Invalid settlement', 'settlement': s})
            continue

        from_balance = storage.get_balance(from_handle)
        if from_balance < amount:
            results.append({'error': 'Insufficient balance', 'settlement': s})
            continue

        storage._balances[from_handle] = from_balance - amount
        storage._balances[to_handle] = storage.get_balance(to_handle) + amount
        results.append({'status': 'ok', 'from': from_handle, 'to': to_handle, 'amount': amount})

    settlement_id = str(uuid.uuid4())[:8]
    settlement_batch = {
        'id': settlement_id,
        'settlements': results,
        'processed_at': datetime.utcnow().isoformat()
    }

    response = {'settlement_batch': settlement_batch}
    response_body = json.dumps(response)
    store_idempotency(idempotency_key, handle, {'status': 200, 'body': response_body})
    send_json(handler, 200, settlement_batch)


# ============== Router ==============

ROUTES = {
    ('GET', '/health'): handle_health,
    ('POST', '/_test/reset'): handle_test_reset,
    ('GET', '/_test/export'): handle_test_export,
    ('POST', '/_test/import'): handle_test_import,
    ('POST', '/auth/signup'): handle_signup,
    ('POST', '/auth/login'): handle_login,
    ('GET', '/me'): handle_me,
    ('POST', '/payments'): handle_payments,
    ('POST', '/requests'): handle_requests_create,
    ('GET', '/requests'): handle_requests_list,
    ('POST', '/requests/{id}/pay'): handle_requests_pay,
    ('POST', '/requests/{id}/decline'): handle_requests_decline,
    ('POST', '/requests/{id}/cancel'): handle_requests_cancel,
    ('POST', '/splits'): handle_splits_create,
    ('GET', '/activity'): handle_activity,
    ('POST', '/settlements'): handle_settlements,
}

def route_handler(handler):
    """Route request to handler."""
    path = handler.path.split('?')[0]

    # Try exact match first
    key = (handler.command, path)
    if key in ROUTES:
        return ROUTES[key](handler)

    # Try pattern matches
    for (method, pattern), func in ROUTES.items():
        if method != handler.command:
            continue
        if pattern.startswith('/requests/') and pattern.endswith('/pay'):
            if re.match(r'^/requests/[^/]+/pay$', path):
                return func(handler)
        elif pattern.startswith('/requests/') and pattern.endswith('/decline'):
            if re.match(r'^/requests/[^/]+/decline$', path):
                return func(handler)
        elif pattern.startswith('/requests/') and pattern.endswith('/cancel'):
            if re.match(r'^/requests/[^/]+/cancel$', path):
                return func(handler)

    send_json(handler, 404, {'error': 'Not found'}, 'not_found')


# ============== HTTP Server ==============

class PocketfulHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_GET(self):
        route_handler(self)

    def do_POST(self):
        route_handler(self)


def run_server(port=None):
    port = int(port or 8080)
    server = HTTPServer(('', port), PocketfulHandler)
    print(f"Pocketful API running on port {port}")
    server.serve_forever()


if __name__ == '__main__':
    import os
    port = os.environ.get('PORT', 8080)
    run_server(port)
