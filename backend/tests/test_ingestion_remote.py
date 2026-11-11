"""Ingestion Lab across servers: steps run over SSH on other machines.

Needs a disposable Postgres and an SSH server with two password logins that stand in
for two servers (each with its own folder and Python):

    INGESTION_TEST_PG_DSN="host=localhost dbname=regtest user=postgres" \\
    INGESTION_TEST_SSH="127.0.0.1:2222" \\
    INGESTION_TEST_SSH_USERS="alpha:pw-alpha,beta:pw-beta" \\
        python -m pytest tests/test_ingestion_remote.py -q

Each user needs ~/pipe-<user>/venv/bin/python (a symlink to python3 is enough).
"""
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

SSH = os.environ.get("INGESTION_TEST_SSH")
USERS = dict(u.split(":", 1) for u in os.environ.get("INGESTION_TEST_SSH_USERS", "").split(",") if ":" in u)

from test_ingestion_lab import DSN, SECOND_SERVER, gone, il, start, watch  # noqa: E402

pytestmark = pytest.mark.skipif(not (DSN and SSH and len(USERS) >= 2 and os.name == "posix"),
                                reason="INGESTION_TEST_PG_DSN / INGESTION_TEST_SSH / INGESTION_TEST_SSH_USERS not set")

PIPELINE = """
import signal, sys, time
stop = False
def on_int(sig, frame):
    global stop
    print("Ctrl-C received, finishing in-flight documents...", flush=True)
    stop = True
signal.signal(signal.SIGINT, on_int)
n = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
for i in range(1, n + 1):
    print(f"doc {i}", flush=True)
    if stop:
        time.sleep(0.3); print("clean stop", flush=True); sys.exit(0)
    time.sleep(delay)
print("all done", flush=True)
sys.exit(3 if "--fail" in sys.argv else 0)
"""
STUBBORN = """
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    print("still going", flush=True); time.sleep(0.1)
"""


def host_key():
    import paramiko
    host, port = SSH.split(":")
    t = paramiko.Transport((host, int(port)))
    t.start_client(timeout=10)
    key = t.get_remote_server_key()
    t.close()
    return il._fingerprint(key)


def sh(user, command):
    """Run a command on the 'server' as that user."""
    import paramiko
    host, port = SSH.split(":")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=int(port), username=user, password=USERS[user], allow_agent=False, look_for_keys=False)
    try:
        _, out, _ = c.exec_command(command)
        return out.read().decode()
    finally:
        c.close()


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    import requests
    import uvicorn
    from fastapi import Depends, FastAPI

    import auth
    import db

    db.migrate()
    host, port = SSH.split(":")
    fp = host_key()
    users = list(USERS)[:2]
    for u in users:
        wd = f"/home/{u}/pipe-{u}"
        sh(u, f"mkdir -p {wd} && cat > {wd}/main.py <<'EOF'\n{PIPELINE}\nEOF\n"
              f"cat > {wd}/stubborn.py <<'EOF'\n{STUBBORN}\nEOF\nrm -rf {wd}/pipeline.pid {wd}/logs ~/.smarthub-ingestion {wd}/alt {wd}/escape\n"
              f"mkdir -p {wd}/alt && cp {wd}/main.py {wd}/alt/ && ln -s /tmp {wd}/escape")
    local_wd = tmp_path_factory.mktemp("localpipe")
    (local_wd / "main.py").write_text(PIPELINE)
    servers = [{"id": "here", "name": "Portal box", "local": True,
                "vars": {"wd": str(local_wd), "py": sys.executable}}]
    for u in users:
        servers.append({"id": u, "name": u.title(), "host": host, "port": int(port), "username": u,
                        "password": USERS[u], "host_key": fp,
                        "vars": {"wd": f"/home/{u}/pipe-{u}", "py": f"/home/{u}/pipe-{u}/venv/bin/python"}})
    servers.append({"id": "badpw", "name": "Bad password", "host": host, "port": int(port), "username": users[0],
                    "password": "wrong", "host_key": fp, "vars": {"wd": "/tmp", "py": "python3"}})
    servers.append({"id": "nokey", "name": "Unpinned", "host": host, "port": int(port), "username": users[0],
                    "password": USERS[users[0]], "known_hosts": str(local_wd / "empty_known_hosts"),
                    "vars": {"wd": "/tmp", "py": "python3"}})
    (local_wd / "empty_known_hosts").write_text("")
    step = {"id": "pipe", "name": "Pipeline", "workdir": "${wd}", "command": ["${py}", "main.py", ["{n}"], ["{delay}"], "{fail}"],
            "inputs": [{"name": "n", "type": "number", "integer": True, "required": False},
                       {"name": "delay", "type": "text", "required": False, "pattern": "[0-9.]+"},
                       {"name": "fail", "type": "flag", "flag": "--fail"}],
            "stop_signal": "INT", "stop_grace_seconds": 30, "pidfile": "pipeline.pid",
            "log_file": "logs/pipeline.log", "nofile_limit": 1024,
            "folder_choice": True, "folder_roots": ["${wd}"], "folder_marker": "main.py"}
    stub = {"id": "stubborn", "name": "Stubborn", "workdir": "${wd}", "command": ["${py}", "stubborn.py"],
            "servers": users, "stop_signal": "INT", "stop_grace_seconds": 60}
    # times out and ignores TERM/INT, so it has to be killed: must end as timed_out, not lost
    stub_to = {**stub, "id": "stubto", "name": "Stubborn timeout", "timeout_minutes": 0.05, "stop_grace_seconds": 1}
    # fails before it starts (no such folder) after the profile file was written
    badprof = {"id": "badprof", "name": "Bad profile", "workdir": "/home/nobody-here/nope", "servers": users[:1],
               "command": ["cat", "{profile}"], "inputs": [{"name": "profile", "type": "profile"}]}
    config = {"ingestion_lab": {"servers": servers, "steps": [step, stub, stub_to, badprof]}}
    config_path = tmp_path_factory.mktemp("cfg") / "ingestion_lab.yaml"
    config_path.write_text(json.dumps(config["ingestion_lab"]))
    il.set_ingestion_lab_dependencies(str(config_path))

    perms = {"viewIngestionLab": True, "runIngestionLab": True}
    app = FastAPI()
    app.include_router(il.ingestion_router, dependencies=[Depends(auth.require_permission("viewIngestionLab"))])
    app.dependency_overrides[auth.require_auth] = lambda: {
        "user": {"email": "tester@example.com", "name": "Tester"}, "role": "admin", "permissions": perms}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        api_port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{api_port}/api/ingestion-lab", "requests": requests, "users": users,
           "config_path": config_path}
    srv.should_exit = True
    t.join(5)


def go(env, step_id, server_id, inputs=None, expect=200):
    r = env["requests"].post(f"{env['base']}/runs", json={"step_id": step_id, "server_id": server_id, "inputs": inputs or {}})
    assert r.status_code == expect, r.text
    return r.json()


def outputs(events):
    return [e["message"] for e in events if e.get("type") == "output"]


def assert_contiguous(out, upto=None):
    docs = [int(m.split()[1]) for m in out if m.startswith("doc ")]
    assert docs == list(range(1, len(docs) + 1)), "lines missing or repeated"
    if upto:
        assert docs[-1] == upto


def test_servers_and_per_server_steps(remote):
    data = remote["requests"].get(f"{remote['base']}/steps").json()
    ids = [s["id"] for s in data["servers"]]
    assert ids[:3] == ["here", *remote["users"]] and not data["errors"]
    steps = {s["id"]: s for s in data["steps"]}
    a, b = remote["users"]
    assert steps["pipe"]["servers"][a]["command"][0] == f"/home/{a}/pipe-{a}/venv/bin/python"
    assert steps["pipe"]["servers"][b]["workdir"] == f"/home/{b}/pipe-{b}"
    assert set(steps["stubborn"]["servers"]) == {a, b}   # limited to the listed servers
    dumped = json.dumps(data)
    assert all(pw not in dumped for pw in USERS.values()) and "wrong" not in dumped   # no passwords leave the server


def test_remote_run_streams_and_finishes(remote):
    a = remote["users"][0]
    t0 = time.monotonic()
    run_id = go(remote, "pipe", a, {"n": 40, "delay": "0.02"})["run_id"]
    events, done = watch(remote, run_id)
    assert done["status"] == "success" and time.monotonic() - t0 < 15
    out = outputs(events)
    assert_contiguous(out, 40)
    assert out[-1] == "all done"
    run = remote["requests"].get(f"{remote['base']}/runs/{run_id}").json()
    assert run["server_id"] == a and run["remote"] and run["pid"]
    assert "doc 40" in sh(a, f"cat ~/pipe-{a}/logs/pipeline.log")   # the step's log file is on its server
    assert sh(a, f"ls ~/pipe-{a}/pipeline.pid 2>/dev/null; ls ~/.smarthub-ingestion").strip() == ""   # cleaned up


def test_remote_exit_code(remote):
    events, done = watch(remote, go(remote, "pipe", remote["users"][1], {"n": 3, "delay": "0.01", "fail": True})["run_id"])
    assert done["status"] == "failed" and done["returncode"] == 3


def test_one_run_per_server(remote):
    a, b = remote["users"]
    ra = go(remote, "pipe", a)["run_id"]
    again = go(remote, "pipe", a, expect=409)["detail"]
    assert again["run_id"] == ra and a.title() in again["message"]
    rb = go(remote, "pipe", b)["run_id"]          # another server is fine
    rl = go(remote, "pipe", "here")["run_id"]     # and so is the portal box itself
    for rid in (ra, rb, rl):
        watch(remote, rid, until=lambda e: e.get("message") == "doc 3")
        remote["requests"].post(f"{remote['base']}/runs/{rid}/stop")
    for rid in (ra, rb, rl):
        events, done = watch(remote, rid)
        assert done["status"] == "stopped" and "clean stop" in outputs(events)


def test_remote_force_stop(remote):
    a = remote["users"][0]
    run_id = go(remote, "stubborn", a)["run_id"]
    watch(remote, run_id, until=lambda e: e.get("message") == "still going")
    remote["requests"].post(f"{remote['base']}/runs/{run_id}/stop")
    time.sleep(1)
    assert remote["requests"].get(f"{remote['base']}/runs/{run_id}").json()["status"] == "running"
    t0 = time.monotonic()
    remote["requests"].post(f"{remote['base']}/runs/{run_id}/stop", params={"force": "true"})
    _, done = watch(remote, run_id)
    assert done["status"] == "stopped" and time.monotonic() - t0 < 5
    assert "stubborn.py" not in sh(a, "ps -u $(id -un) -o cmd")


def test_remote_pidfile_blocks_outside_run(remote):
    b = remote["users"][1]
    sh(b, f"cd ~/pipe-{b} && {{ setsid venv/bin/python main.py 1000 0.1 </dev/null >/dev/null 2>&1 & echo $! > pipeline.pid; }}")
    try:
        il._OUTSIDE.clear()
        runs = remote["requests"].get(f"{remote['base']}/outside", params={"server_id": b}).json()["runs"]
        pid = int(sh(b, f"cat ~/pipe-{b}/pipeline.pid").strip())
        assert [r["pid"] for r in runs.get("pipe", [])] == [pid], runs
        detail = go(remote, "pipe", b, expect=409)["detail"]
        assert detail["code"] == "outside" and "already running outside the portal" in detail["message"]
        r = remote["requests"].post(f"{remote['base']}/outside/stop", json={"step_id": "pipe", "server_id": b, "pid": pid})
        assert r.status_code == 200, r.text
        for _ in range(50):
            if sh(b, f"{il._alive_sh(str(pid))} && echo alive || echo gone").strip() == "gone":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("outside run still alive")
    finally:
        sh(b, f"kill $(cat ~/pipe-{b}/pipeline.pid); rm -f ~/pipe-{b}/pipeline.pid")


def test_login_problems_are_explained(remote):
    events, done = watch(remote, go(remote, "pipe", "badpw")["run_id"])
    assert done["status"] == "failed"
    assert any("refused" in e.get("message", "") for e in events)
    events, done = watch(remote, go(remote, "pipe", "nokey")["run_id"])
    assert any('"host_key": "SHA256:' in e.get("message", "") for e in events)
    check = remote["requests"].get(f"{remote['base']}/servers/badpw/check").json()
    assert check["ok"] is False and "refused" in check["error"]
    assert remote["requests"].get(f"{remote['base']}/servers/{remote['users'][0]}/check").json()["ok"] is True


def test_dropped_connection_loses_nothing(remote):
    a = remote["users"][0]
    run_id = go(remote, "pipe", a, {"n": 80, "delay": "0.05"})["run_id"]
    watch(remote, run_id, until=lambda e: e.get("message") == "doc 20")
    # cut every SSH session for that login (the portal's included); the step itself keeps going
    subprocess.run(["pkill", "-KILL", "-f", f"sshd: {a}"], check=False)
    events, done = watch(remote, run_id, timeout=90)
    assert done["status"] == "success"
    out = outputs(events)
    assert_contiguous(out, 80)
    msgs = [e.get("message", "") for e in events]
    assert any("Lost the connection" in m for m in msgs) and any("Reconnected" in m for m in msgs)


def _second_portal(remote, tmp_path):
    script = tmp_path / "second.py"
    script.write_text(SECOND_SERVER)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, str(script), backend, str(remote["config_path"]), str(port)])
    other = {**remote, "base": f"http://127.0.0.1:{port}/api/ingestion-lab"}
    for _ in range(100):
        try:
            if remote["requests"].get(f"{other['base']}/active", timeout=1).ok:
                break
        except Exception:
            pass
        time.sleep(0.1)
    return proc, other


@pytest.mark.parametrize("how", ["crash", "restart"])
def test_portal_going_away_doesnt_stop_remote_runs(remote, tmp_path, monkeypatch, how):
    monkeypatch.setattr(il, "STALE", "3 seconds")
    monkeypatch.setattr(il, "ADOPT_EVERY", 0.5)
    b = remote["users"][1]
    proc, other = _second_portal(remote, tmp_path)
    try:
        run_id = go(other, "pipe", b, {"n": 120, "delay": "0.05"})["run_id"]
        watch(remote, run_id, until=lambda e: e.get("message") == "doc 15")
        if how == "crash":
            proc.kill()            # no goodbye at all
        else:
            proc.terminate()       # deploy / restart: hands the run over
        proc.wait(15)
        events, done = watch(remote, run_id, timeout=120)   # this portal picks it up and sees it through
        assert done["status"] == "success"
        out = outputs(events)
        assert_contiguous(out, 120)
        assert any("Picked up again" in e.get("message", "") for e in events)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_reaches_a_run_nobody_is_following(remote):
    import db
    a = remote["users"][0]
    sh(a, f"cd ~/pipe-{a} && {{ setsid venv/bin/python main.py 100000 0.1 </dev/null >/dev/null 2>&1 & echo $! > /tmp/orphan_{a}.pid; }}")
    pid = int(sh(a, f"cat /tmp/orphan_{a}.pid").strip())
    with db.transaction() as conn:
        conn.execute("""INSERT INTO ingestion_runs (id, step_id, step_name, server_id, server_name, remote, pid, status, finished_at)
                        VALUES (?, 'pipe', 'Pipeline', ?, ?, true, ?, 'lost', now())""", ("e" * 32, a, a.title(), pid))
    r = remote["requests"].post(f"{remote['base']}/runs/{'e' * 32}/stop", params={"force": "true"})
    assert r.status_code == 200 and r.json()["direct"]
    time.sleep(0.5)
    assert sh(a, f"{il._alive_sh(str(pid))} && echo alive || echo gone").strip() == "gone"


def test_remote_folder_picker(remote):
    a = remote["users"][0]
    wd = f"/home/{a}/pipe-{a}"
    r = remote["requests"].get(f"{remote['base']}/folders", params={"step_id": "pipe", "server_id": a}).json()
    names = {d["name"]: d["marked"] for d in r["dirs"]}
    assert r["real"] == wd and r["marked_here"] and names.get("alt") is True and "escape" in names
    r = remote["requests"].get(f"{remote['base']}/folders", params={"step_id": "pipe", "server_id": a, "path": f"{wd}/escape"})
    assert r.status_code == 422 and "outside the allowed folders" in r.json()["detail"]
    # runs in the chosen folder, and its pidfile / log file go there too
    r = remote["requests"].post(f"{remote['base']}/runs", json={"step_id": "pipe", "server_id": a, "workdir": f"{wd}/alt",
                                                                "inputs": {"n": 5, "delay": "0.01"}})
    assert r.status_code == 200, r.text
    events, done = watch(remote, r.json()["run_id"])
    assert done["status"] == "success" and "doc 5" in outputs(events)
    assert "doc 5" in sh(a, f"cat {wd}/alt/logs/pipeline.log")
    # a symlink that leads outside is caught on the server itself
    r = remote["requests"].post(f"{remote['base']}/runs", json={"step_id": "pipe", "server_id": a, "workdir": f"{wd}/escape"})
    events, done = watch(remote, r.json()["run_id"])
    assert done["status"] == "failed" and any("outside the allowed folders" in e.get("message", "") for e in events)
    r = remote["requests"].post(f"{remote['base']}/runs", json={"step_id": "pipe", "server_id": a, "workdir": "/etc"})
    assert r.status_code == 422


def test_server_config_problems():
    def load(section):
        cfg = il._Config()
        cfg.load(section)
        return cfg
    cfg = load({"servers": [{"id": "x", "host": "h", "username": "u"}], "steps": []})
    assert any("password" in e for e in cfg.errors)
    cfg = load({"servers": [{"id": "a", "local": True}, {"id": "a", "local": True}], "steps": []})
    assert any("share the id" in e for e in cfg.errors)
    base = {"id": "s", "workdir": "${wd}", "command": ["${py}"]}
    cfg = load({"servers": [{"id": "a", "local": True, "vars": {"wd": "/a", "py": "p"}}, {"id": "b", "local": True}],
                "steps": [base]})
    assert set(cfg.steps["s"].variants) == {"a"} and not cfg.errors     # b doesn't define the vars: not offered there
    cfg = load({"servers": [{"id": "a", "local": True, "vars": {"wd": "/a", "py": "p"}}, {"id": "b", "local": True}],
                "steps": [{**base, "servers": ["a", "b"]}]})
    assert any("has no ${wd}" in e or "has no ${py}" in e for e in cfg.errors)
    cfg = load({"servers": [{"id": "a", "local": True}], "steps": [{**base, "servers": ["zz"]}]})
    assert any("unknown server" in e for e in cfg.errors)
    cfg = load({"servers": [{"id": "a", "local": True, "vars": {"root": "/srv/x"}}], "folder_choice": True,
                "folder_roots": ["${root}"], "folder_marker": "main.py",
                "steps": [{"id": "one", "workdir": "/srv/x/app", "command": ["true"]},
                          {"id": "two", "workdir": "/srv/x/app", "command": ["true"], "folder_choice": False}]})
    one, two = cfg.steps["one"].variants["a"], cfg.steps["two"].variants["a"]
    assert one.folder_choice and one.folder_roots == ["/srv/x"] and one.folder_marker == "main.py"   # inherited
    assert not two.folder_choice                                                                       # its own setting wins
    cfg = load({"steps": [{"id": "plain", "workdir": "/tmp", "command": ["true"]}]})   # no servers: this one
    assert list(cfg.servers) == ["local"] and set(cfg.steps["plain"].variants) == {"local"}

def test_folder_names_never_run_as_shell(remote):
    a = remote["users"][0]
    wd = f"/home/{a}/pipe-{a}"
    sh(a, f"rm -f /tmp/il_pwned_{a}_*")
    for i, path in enumerate([f"{wd}/$(touch /tmp/il_pwned_{a}_1)", f"{wd}/`touch /tmp/il_pwned_{a}_2`"]):
        remote["requests"].get(f"{remote['base']}/folders", params={"step_id": "pipe", "server_id": a, "path": path})
        r = remote["requests"].post(f"{remote['base']}/runs", json={"step_id": "pipe", "server_id": a, "workdir": path})
        if r.status_code == 200:
            _, done = watch(remote, r.json()["run_id"])
            assert done["status"] == "failed"
    assert sh(a, f"ls /tmp/il_pwned_{a}_* 2>/dev/null").strip() == ""


def test_stop_asked_while_nobody_follows_still_stops(remote, tmp_path, monkeypatch):
    monkeypatch.setattr(il, "STALE", "3 seconds")
    monkeypatch.setattr(il, "ADOPT_EVERY", 0.5)
    b = remote["users"][1]
    proc, other = _second_portal(remote, tmp_path)
    try:
        run_id = go(other, "pipe", b, {"n": 100000, "delay": "0.05"})["run_id"]
        watch(remote, run_id, until=lambda e: e.get("message") == "doc 5")
        proc.kill()
        proc.wait(15)
        # asked while the portal that followed it is gone: whoever picks it up must deliver it
        assert remote["requests"].post(f"{remote['base']}/runs/{run_id}/stop").status_code == 200
        events, done = watch(remote, run_id, timeout=60)
        assert done["status"] == "stopped" and "clean stop" in outputs(events)
    finally:
        if proc.poll() is None:
            proc.kill()
        sh(b, "pkill -f 'main[.]py 100000' || true")

def test_remote_timeout_that_needs_a_kill_is_timed_out(remote):
    a = remote["users"][0]
    events, done = watch(remote, go(remote, "stubto", a)["run_id"], timeout=60)
    assert done["status"] == "timed_out", [e.get("message") for e in events][-5:]
    assert "stubborn.py" not in sh(a, "ps -u $(id -un) -o cmd")


def test_remote_profile_file_is_removed_when_the_step_cant_start(remote):
    a = remote["users"][0]
    run_id = go(remote, "badprof", a, {"profile": {"version": 1}})["run_id"]
    _, done = watch(remote, run_id)
    assert done["status"] == "failed"
    assert sh(a, f"ls ~/.smarthub-ingestion/{run_id}* 2>/dev/null").strip() == ""