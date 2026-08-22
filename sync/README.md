# ConcordeAI Sync — the droplet, and why the server can't read your chats

**Box:** `concordeai-db`, Ubuntu 26.04 LTS, NYC3 · `104.236.212.237` (reserved `129.212.150.83`)
**Service:** `/opt/concordeai-sync/concordeai_sync.py`, systemd unit `concordeai-sync`, listening on `127.0.0.1:8792`
**TLS:** Caddy on `sync.millertechnology.net`, auto-provisioned once DNS resolves

---

## The promise, and the mechanism behind it

The server stores your chats and **cannot read them**. Not with database access,
not by me, not by DigitalOcean, not under subpoena. That isn't a policy — it's
arithmetic. Every byte of crypto happens on your device; the password never
leaves it.

At signup, your device derives two independent keys from your password:

```
stretched   = PBKDF2-SHA256(password, kdf_salt, 600,000 iterations)
auth_key    = HKDF(stretched, "concordeai-auth")   → proves who you are
wrap_key    = HKDF(stretched, "concordeai-wrap")   → never leaves the device
```

Then it makes a **data key** — 32 random bytes, unrelated to your password —
encrypts your chats with it, and encrypts the data key itself with `wrap_key`:

```
data_key    = 32 random bytes
wrapped_key = AES-GCM(wrap_key, data_key)
blob        = AES-GCM(data_key, gzip(json(chats)))
```

The server receives only `email`, `kdf_salt`, `auth_key`, `wrapped_key`, `blob`.
It hashes `auth_key` with scrypt before storing it, so even the login proof isn't
kept in a usable form. It has no path to `wrap_key` (that needs your password),
so it cannot unwrap `data_key`, so it cannot decrypt one message.

**Why the separate data key:** changing your password re-derives `wrap_key` and
re-wraps the same `data_key`. Your chats are never re-encrypted, never re-uploaded,
and never pass through the server in the clear.

**The honest cost:** a truly forgotten password means truly unrecoverable chats.
There is no reset link, because a reset link would mean the server could decrypt
your data — which is the whole thing we're avoiding. The signup screen says so
plainly.

---

## What's on the box

| Piece | State |
|---|---|
| Sync service | `active`, ~11 MB resident, `127.0.0.1:8792` only |
| Service user | `sync` — system account, `nologin`, owns nothing else |
| systemd sandbox | `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `MemoryMax=350M` |
| Firewall (ufw) | 22, 80, 443 only — the sync port is unreachable from outside |
| SSH | key-only; password and keyboard-interactive auth disabled |
| Swap | 2 GB, `swappiness=10` (headroom on a 1 GB box) |
| Auto-updates | `unattended-upgrades` on for security patches |
| Caddy | installed, config validated, waiting on DNS |

## API (all POST JSON, except health)

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | liveness |
| `/v1/login-begin` | returns `kdf_salt` — **and a convincing fake for unknown emails**, so it can't be used to test whether an address is registered |
| `/v1/signup` | create account, returns session token |
| `/v1/login` | returns token + `wrapped_key` + `blob` + `version` |
| `/v1/pull` | fetch current blob |
| `/v1/sync` | push blob with `base_version`; `409` + current copy if another device pushed first (client merges by chat id and retries) |
| `/v1/rekey` | password change — re-wraps the data key, signs out *other* devices |
| `/v1/logout` | drop this session |
| `/v1/delete` | erase the account — what Forget Me calls to reach the cloud copy |

Rate limiting: 30 requests / 5 min per IP on the unauthenticated auth routes.
Sessions: 45 days, sliding, stored only as an HMAC of the token.

## Operating it

```bash
ssh root@104.236.212.237
systemctl status concordeai-sync
journalctl -u concordeai-sync -n 50
```

The database is `/opt/concordeai-sync/sync.db` (SQLite, WAL). Worth adding
DigitalOcean weekly backups ($1.20/mo) — losing it means every user's encrypted
blobs are gone, and by design nobody can reconstruct them.
