import base64, hashlib, json, os, subprocess, time, urllib.request, urllib.error, tempfile, signal
D=tempfile.mkdtemp()
env=dict(os.environ, SYNC_DB=os.path.join(D,"t.db"), SYNC_SECRET=os.path.join(D,"s.sec"), SYNC_PORT="8799", SYNC_BIND="127.0.0.1")
srv=subprocess.Popen(["python3","concordeai_sync.py"],env=env)
time.sleep(1.5)
B="http://127.0.0.1:8799"
def post(path,obj,expect=200):
    req=urllib.request.Request(B+path,data=json.dumps(obj).encode(),headers={"Content-Type":"application/json"},method="POST")
    try:
        r=urllib.request.urlopen(req,timeout=5); code=r.status; body=json.loads(r.read())
    except urllib.error.HTTPError as e:
        code=e.code; body=json.loads(e.read())
    assert code==expect, f"{path}: got {code} {body}, wanted {expect}"
    return body
def ak(pw,salt):  # stand-in for the client's PBKDF2->HKDF auth_key
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256",pw.encode(),salt.encode(),50000,32)).decode()
def rnd(n): return base64.b64encode(os.urandom(n)).decode()
try:
    fails=[]
    r=post("/v1/login-begin",{"email":"nobody@x.com"}); assert "kdf_salt" in r; fake=r["kdf_salt"]
    KS=rnd(16)
    r=post("/v1/signup",{"email":"a@b.com","kdf_salt":KS,"auth_key":ak("hunter2",KS),"wrapped_key":rnd(48)}); tok=r["token"]; assert tok
    r=post("/v1/login-begin",{"email":"a@b.com"}); assert r["kdf_salt"]==KS, "real salt returned"
    assert fake!=KS, "fake salt differs from real"
    r=post("/v1/signup",{"email":"a@b.com","kdf_salt":KS,"auth_key":ak("x",KS),"wrapped_key":rnd(48)},409)
    r=post("/v1/login",{"email":"a@b.com","auth_key":ak("WRONG",KS)},401)
    r=post("/v1/login",{"email":"a@b.com","auth_key":ak("hunter2",KS)}); tok=r["token"]; assert r["blob"] is None and r["version"]==0
    r=post("/v1/sync",{"token":tok,"blob":rnd(200),"base_version":0}); assert r["version"]==1
    r=post("/v1/sync",{"token":tok,"blob":rnd(200),"base_version":0},409); assert r["version"]==1 and r["blob"]
    r=post("/v1/sync",{"token":tok,"blob":rnd(200),"base_version":1}); assert r["version"]==2
    r=post("/v1/pull",{"token":tok}); assert r["version"]==2 and r["blob"]
    r=post("/v1/login",{"email":"a@b.com","auth_key":ak("hunter2",KS)}); tokB=r["token"]   # a second device
    NKS=rnd(16)
    r=post("/v1/rekey",{"token":tok,"kdf_salt":NKS,"auth_key":ak("newpw",NKS),"wrapped_key":rnd(48)}); assert r["ok"]
    r=post("/v1/pull",{"token":tok}); assert r["version"]==2   # the rekeying device stays in
    r=post("/v1/pull",{"token":tokB},401)                      # OTHER devices are signed out
    r=post("/v1/login",{"email":"a@b.com","auth_key":ak("hunter2",KS)},401)   # old pw dead
    r=post("/v1/login",{"email":"a@b.com","auth_key":ak("newpw",NKS)}); tok2=r["token"]; assert r["version"]==2, "data survived rekey"
    r=post("/v1/delete",{"token":tok2}); assert r["ok"]
    r=post("/v1/login",{"email":"a@b.com","auth_key":ak("newpw",NKS)},401)   # gone
    r=post("/v1/login-begin",{"email":"a@b.com"}); assert r["kdf_salt"]!=NKS, "deleted -> fake salt again"
    print("SELFTEST: all lifecycle checks passed")
finally:
    srv.send_signal(signal.SIGTERM); srv.wait()
