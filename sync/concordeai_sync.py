#!/usr/bin/env python3
"""ConcordeAI Sync — a ZERO-KNOWLEDGE account + chat-sync service.

The whole point: this server can NEVER read a user's chats, even with
full database access, even by me, even by DigitalOcean. It stores
ciphertext and an authentication verifier and nothing else that means
anything without the user's password — and the password never leaves
the user's device.

How that holds (the client does every bit of crypto in the WebView's
native Web Crypto; the server is deliberately dumb):

  stretched   = PBKDF2-SHA256(password, kdf_salt, 600k iters)   [client]
  auth_key    = HKDF(stretched, "concordeai-auth")              [client]
  wrap_key    = HKDF(stretched, "concordeai-wrap")              [client]
  data_key    = 32 random bytes, made once at signup            [client]
  wrapped_key = AES-GCM(wrap_key, data_key)                     [client]
  blob        = AES-GCM(data_key, gzip(json(chats)))            [client]

The server receives, and stores, only: email, kdf_salt (public),
scrypt(auth_key) + its salt (so a DB leak doesn't even expose auth_key),
wrapped_key (opaque), and blob (opaque). It cannot derive wrap_key
(needs the password), cannot unwrap data_key, cannot read a single
message. data_key is independent of the password, so a password change
just re-wraps it — no re-encryption, no server ever seeing plaintext.

Stdlib only — http.server + sqlite3 + hashlib.scrypt + hmac + secrets —
so it fits the app's no-dependencies ethos and there is less to audit.
Runs on 127.0.0.1; Caddy terminates TLS on 443 and forwards here.
"""
import base64
import gzip
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SYNC_DB", os.path.join(HERE, "sync.db"))
SECRET_PATH = os.environ.get("SYNC_SECRET", os.path.join(HERE, "server.secret"))
BIND = os.environ.get("SYNC_BIND", "127.0.0.1")
PORT = int(os.environ.get("SYNC_PORT", "8792"))

MAX_BODY = 8 * 1024 * 1024          # 8 MB — a very large encrypted chat set
TOKEN_TTL = 45 * 24 * 3600          # sessions last 45 days, refreshed on use
SCRYPT = dict(n=2 ** 14, r=8, p=1)  # server-side, on an already-strong auth_key

# ------------------------------------------------------------ server secret
# One random secret, made once, 0600. Two jobs: a deterministic FAKE salt
# for unknown emails (so /login-begin can't be used to test whether an
# address is registered), and a pepper folded into token hashing.
if os.path.exists(SECRET_PATH):
    with open(SECRET_PATH, "rb") as f:
        SERVER_SECRET = f.read()
else:
    SERVER_SECRET = secrets.token_bytes(32)
    _fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(_fd, SERVER_SECRET)
    os.close(_fd)

# ------------------------------------------------------------ database
_db_lock = threading.Lock()


def _db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


def _init_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            email        TEXT PRIMARY KEY,
            kdf_salt     TEXT NOT NULL,
            auth_hash    BLOB NOT NULL,
            auth_salt    BLOB NOT NULL,
            wrapped_key  TEXT NOT NULL,
            blob         BLOB,
            version      INTEGER NOT NULL DEFAULT 0,
            created      REAL NOT NULL,
            updated      REAL NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
            token_hash   BLOB PRIMARY KEY,
            email        TEXT NOT NULL,
            expires      REAL NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_sess_email "
                  "ON sessions(email)")


# ------------------------------------------------------------ helpers
def _norm_email(e):
    return str(e or "").strip().lower()[:200]


def _valid_email(e):
    return "@" in e and "." in e.split("@")[-1] and 3 < len(e) <= 200


def _b64ok(s, maxlen):
    """A base64 string of a bounded byte length — reject anything else so
    a client can't smuggle junk into a stored field."""
    if not isinstance(s, str) or len(s) > maxlen:
        return False
    try:
        base64.b64decode(s, validate=True)
        return True
    except Exception:
        return False


def _fake_salt(email):
    """A stable, real-looking salt for an email we've never seen, so the
    response to /login-begin is identical whether or not the account
    exists — no registration oracle."""
    return base64.b64encode(
        hmac.new(SERVER_SECRET, b"salt:" + email.encode(),
                 hashlib.sha256).digest()[:16]).decode()


def _auth_hash(auth_key_b64, auth_salt):
    return hashlib.scrypt(base64.b64decode(auth_key_b64),
                          salt=auth_salt, dklen=32, **SCRYPT)


def _token_hash(token):
    return hmac.new(SERVER_SECRET, b"tok:" + token.encode(),
                    hashlib.sha256).digest()


def _new_session(c, email):
    token = secrets.token_urlsafe(32)
    c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    c.execute("INSERT INTO sessions(token_hash,email,expires) VALUES(?,?,?)",
              (_token_hash(token), email, time.time() + TOKEN_TTL))
    return token


def _session_email(c, token):
    if not token:
        return None
    row = c.execute("SELECT email,expires FROM sessions WHERE token_hash=?",
                    (_token_hash(token),)).fetchone()
    if not row or row[1] < time.time():
        return None
    # sliding expiry: active sessions don't die under a user
    c.execute("UPDATE sessions SET expires=? WHERE token_hash=?",
              (time.time() + TOKEN_TTL, _token_hash(token)))
    return row[0]


# ------------------------------------------------------------ rate limit
# per-IP token bucket for the unauthenticated auth endpoints — a brute
# force against auth_key is already hopeless (it is PBKDF2-600k output),
# but this keeps anyone from grinding the box.
_rl_lock = threading.Lock()
_rl = {}                    # ip -> [tokens, last_refill]
_RL_MAX, _RL_REFILL = 30, 30.0 / 300      # 30 per 5 min, refilling smoothly


def _rate_ok(ip):
    now = time.time()
    with _rl_lock:
        toks, last = _rl.get(ip, (_RL_MAX, now))
        toks = min(_RL_MAX, toks + (now - last) * _RL_REFILL)
        if toks < 1:
            _rl[ip] = (toks, now)
            return False
        _rl[ip] = (toks - 1, now)
        return True


# ------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "concordeai-sync"

    def log_message(self, *a):        # never log bodies or tokens
        pass

    def _client_ip(self):
        # only Caddy on localhost talks to us, and it sets XFF
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff
                else self.client_address[0])

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            d = json.loads(self.rfile.read(n))
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def do_GET(self):
        if self.path == "/v1/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path
        auth = {"/v1/signup", "/v1/login-begin", "/v1/login"}
        if p in auth and not _rate_ok(self._client_ip()):
            return self._send(429, {"error": "slow down"})
        d = self._body()
        if d is None:
            return self._send(400, {"error": "bad request"})
        try:
            with _db_lock, _db() as c:
                return self._route(p, d, c)
        except Exception:
            return self._send(500, {"error": "server error"})

    def _route(self, p, d, c):
        if p == "/v1/login-begin":
            email = _norm_email(d.get("email"))
            if not _valid_email(email):
                return self._send(400, {"error": "bad email"})
            row = c.execute("SELECT kdf_salt FROM users WHERE email=?",
                            (email,)).fetchone()
            # identical shape whether or not the account exists
            return self._send(200, {"kdf_salt": row[0] if row
                                    else _fake_salt(email)})

        if p == "/v1/signup":
            email = _norm_email(d.get("email"))
            ks, ak, wk = (d.get("kdf_salt"), d.get("auth_key"),
                          d.get("wrapped_key"))
            if not _valid_email(email):
                return self._send(400, {"error": "bad email"})
            if not (_b64ok(ks, 64) and _b64ok(ak, 64) and _b64ok(wk, 512)):
                return self._send(400, {"error": "bad fields"})
            if c.execute("SELECT 1 FROM users WHERE email=?",
                         (email,)).fetchone():
                return self._send(409, {"error": "account exists"})
            salt = secrets.token_bytes(16)
            c.execute("INSERT INTO users(email,kdf_salt,auth_hash,auth_salt,"
                      "wrapped_key,blob,version,created) "
                      "VALUES(?,?,?,?,?,NULL,0,?)",
                      (email, ks, _auth_hash(ak, salt), salt, wk, time.time()))
            return self._send(200, {"token": _new_session(c, email)})

        if p == "/v1/login":
            email = _norm_email(d.get("email"))
            ak = d.get("auth_key")
            if not (_valid_email(email) and _b64ok(ak, 64)):
                return self._send(400, {"error": "bad fields"})
            row = c.execute("SELECT auth_hash,auth_salt,kdf_salt,wrapped_key,"
                            "blob,version FROM users WHERE email=?",
                            (email,)).fetchone()
            # constant-ish time: hash even when the user is unknown
            ref = row[1] if row else secrets.token_bytes(16)
            got = _auth_hash(ak, ref)
            if not row or not hmac.compare_digest(got, row[0]):
                return self._send(401, {"error": "wrong email or password"})
            blob = base64.b64encode(row[4]).decode() if row[4] else None
            return self._send(200, {"token": _new_session(c, email),
                                    "kdf_salt": row[2], "wrapped_key": row[3],
                                    "blob": blob, "version": row[5]})

        # ---- authenticated below ----
        email = _session_email(c, d.get("token"))
        if not email:
            return self._send(401, {"error": "signed out"})

        if p == "/v1/pull":
            row = c.execute("SELECT blob,version FROM users WHERE email=?",
                            (email,)).fetchone()
            blob = base64.b64encode(row[0]).decode() if row and row[0] else None
            return self._send(200, {"blob": blob,
                                    "version": row[1] if row else 0})

        if p == "/v1/sync":
            blob, base_v = d.get("blob"), d.get("base_version")
            if not _b64ok(blob, MAX_BODY // 3 * 4) or not isinstance(base_v, int):
                return self._send(400, {"error": "bad fields"})
            row = c.execute("SELECT blob,version FROM users WHERE email=?",
                            (email,)).fetchone()
            cur = row[1] if row else 0
            if base_v != cur:
                # someone else pushed first — hand back the current copy so
                # the client can merge (union chats by id) and retry
                other = (base64.b64encode(row[0]).decode()
                         if row and row[0] else None)
                return self._send(409, {"error": "conflict",
                                        "blob": other, "version": cur})
            nv = cur + 1
            c.execute("UPDATE users SET blob=?,version=?,updated=? "
                      "WHERE email=?",
                      (base64.b64decode(blob), nv, time.time(), email))
            return self._send(200, {"version": nv})

        if p == "/v1/rekey":
            # password change: the client re-derived auth_key/wrap_key from
            # the NEW password and re-wrapped the SAME data_key, so not a
            # byte of chat data is touched or re-uploaded
            ks, ak, wk = (d.get("kdf_salt"), d.get("auth_key"),
                          d.get("wrapped_key"))
            if not (_b64ok(ks, 64) and _b64ok(ak, 64) and _b64ok(wk, 512)):
                return self._send(400, {"error": "bad fields"})
            salt = secrets.token_bytes(16)
            c.execute("UPDATE users SET kdf_salt=?,auth_hash=?,auth_salt=?,"
                      "wrapped_key=? WHERE email=?",
                      (ks, _auth_hash(ak, salt), salt, wk, email))
            c.execute("DELETE FROM sessions WHERE email=? AND token_hash!=?",
                      (email, _token_hash(d.get("token"))))
            return self._send(200, {"ok": True})

        if p == "/v1/logout":
            c.execute("DELETE FROM sessions WHERE token_hash=?",
                      (_token_hash(d.get("token")),))
            return self._send(200, {"ok": True})

        if p == "/v1/delete":
            # the Account pane's Forget Me, reaching the cloud copy too
            c.execute("DELETE FROM sessions WHERE email=?", (email,))
            c.execute("DELETE FROM users WHERE email=?", (email,))
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def main():
    _init_db()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print("concordeai-sync on %s:%d  db=%s" % (BIND, PORT, DB_PATH),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
