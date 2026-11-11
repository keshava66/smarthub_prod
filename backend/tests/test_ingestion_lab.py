"""Ingestion Lab tests.

Config and input checks run anywhere. The run tests start real commands on a
real server against real PostgreSQL and skip unless a disposable database is set:

    INGESTION_TEST_PG_DSN="host=localhost port=5432 user=postgres dbname=regtest" \\
        python -m pytest tests/test_ingestion_lab.py -q
"""
import json
import os
import socket
import sys
import textwrap
import threading
import time
import uuid

import pytest

DSN = os.environ.get("INGESTION_TEST_PG_DSN")
SCHEMA = "smarthub_test_" + uuid.uuid4().hex[:8]
if DSN:
    os.environ["SMARTHUB_DB_DSN"] = DSN
    os.environ["SMARTHUB_DB_SCHEMA"] = SCHEMA

import ingestion_lab as il  # noqa: E402

needs_db = pytest.mark.skipif(not DSN or os.name != "posix", reason="INGESTION_TEST_PG_DSN not set (or not Linux)")


# ---------------------------------------------------------------------------
# Config and inputs
# ---------------------------------------------------------------------------

def step(**over):
    raw = {"id": "extract", "name": "Extraction", "workdir": "/tmp",
           "command": ["python3", "x.py", "--env", "{env}", ["--table", "{table}"], "{full}"],
           "inputs": [
               {"name": "env", "type": "select", "options": ["DEV", "QA"], "default": "DEV"},
               {"name": "table", "type": "text", "required": False, "pattern": "[A-Za-z0-9_.]+"},
               {"name": "full", "type": "flag", "flag": "--full"},
               {"name": "batch", "type": "number", "required": False, "min": 1, "max": 100, "integer": True},
               {"name": "run_date", "type": "date", "required": False},
               {"name": "password", "type": "secret", "required": False},
           ]}
    raw.update(over)
    return il.Step(raw, 0)


@pytest.mark.parametrize("over, message", [
    ({"id": "Bad Id"}, "id"),
    ({"workdir": "relative/path"}, "absolute"),
    ({"workdir": None}, "root on the server"),
    ({"command": ["python3", "{nope}"]}, "no input named nope"),
    ({"command": ["{env}", "x"]}, "must be fixed"),
    ({"shell": "echo hi"}, "exactly one"),
    ({"command": None, "shell": "python run.py --env {env}"}, "placeholders"),
    ({"inputs": [{"name": "env", "type": "select", "options": ["A"], "default": "B"}]}, "default"),
    ({"inputs": [{"name": "a"}, {"name": "a"}]}, "share a name"),
    ({"inputs": [{"name": "a", "type": "colour"}]}, "type"),
    ({"inputs": [{"name": "a", "pattern": "("}]}, "regex"),
])
def test_bad_config_is_refused(over, message):
    with pytest.raises(il.ConfigError, match=message):
        step(**over)


def test_inputs_are_checked():
    s = step()
    values, problems = s.resolve({})
    assert not problems and values["env"] == "DEV" and values["full"] == ""
    _, problems = s.resolve({"env": "PRD", "table": "-rf", "batch": "0", "run_date": "2024-13-01",
                             "full": "yes", "extra": 1})
    assert set(problems) == {"env", "table", "batch", "run_date", "full", "extra"}
    _, problems = s.resolve({"table": "a;rm -rf /"})
    assert "table" in problems
    _, problems = s.resolve({"batch": "2.5"})
    assert "whole" in problems["batch"]
    values, problems = s.resolve({"env": "QA", "table": "SALES.ORDERS", "full": True, "batch": 10,
                                  "run_date": "2024-02-29", "password": "p@ss -word"})
    assert not problems
    assert values == {"env": "QA", "table": "SALES.ORDERS", "full": "1", "batch": "10",
                      "run_date": "2024-02-29", "password": "p@ss -word"}


def test_command_is_built_without_a_shell():
    s = step()
    values, _ = s.resolve({"env": "QA"})
    assert s.argv(values) == ["python3", "x.py", "--env", "QA"]
    values, _ = s.resolve({"env": "QA", "table": "T1", "full": True})
    assert s.argv(values) == ["python3", "x.py", "--env", "QA", "--table", "T1", "--full"]


def test_secrets_never_show():
    s = step(command=["python3", "x.py", "--pw={password}"])
    values, _ = s.resolve({"password": "hunter2"})
    assert s.argv(values) == ["python3", "x.py", "--pw=hunter2"]
    assert "hunter2" not in s.display(values)
    assert s.masked_inputs(values)["password"] == il.MASK
    assert all(i.get("default") is None for i in s.public()["inputs"] if i["type"] == "secret")


@pytest.mark.parametrize("presets, message", [
    ([{"name": "x", "inputs": {"nope": 1}}], "unknown input"),
    ([{"name": "x", "inputs": {"password": "p"}}], "secret"),
    ([{"name": "x", "inputs": {"env": "PRD"}}], "allowed options"),
    ([{"inputs": {}}], "needs a name"),
])
def test_bad_presets_are_refused(presets, message):
    with pytest.raises(il.ConfigError, match=message):
        step(presets=presets)


def test_step_options():
    s = step(presets=[{"name": "QA full", "inputs": {"env": "QA", "full": True}}], stop_signal="sigint",
             pidfile="run.pid", log_file="/var/log/x.log", log_keep="last", max_log_lines=5000)
    pub = s.public()
    assert pub["graceful_stop"] and pub["presets"][0]["inputs"] == {"env": "QA", "full": True}
    assert s.pidfile == "/tmp/run.pid" and s.log_file == "/var/log/x.log"
    for bad in ({"stop_signal": "HUP"}, {"nofile_limit": 10}, {"log_keep": "middle"}, {"max_log_lines": 5}):
        with pytest.raises(il.ConfigError):
            step(**bad)
    assert il._duration(3 * 86400 + 7200) == "3d 2h"


def test_linux_paths_load_on_windows(monkeypatch):
    import ntpath
    monkeypatch.setattr(il.os.path, "isabs", ntpath.isabs)   # Python 3.13 on Windows: '/home/x' isn't absolute
    s = il.Step({"id": "x", "workdir": "/home/u/clinical", "command": ["python", "-m", "main"],
                 "pidfile": "pipeline.pid", "log_file": "logs/a.log"}, 0)
    assert s.pidfile == "/home/u/clinical/pipeline.pid" and s.log_file == "/home/u/clinical/logs/a.log"


def test_output_lines_are_cleaned():
    assert il._clean("\x1b[31mERROR\x1b[0m boom\r") == "ERROR boom"
    assert il._clean("10%\r50%\r100%") == "100%"
    assert il._clean("a\tb") == "a    b"
    assert il._clean("x" * (il.MAX_LINE + 50)).endswith("[line cut]")


class FakeConn:
    def __init__(self, fail=False):
        self.fail, self.rows = fail, []

    def execute(self, sql, params=()):
        if self.fail:
            raise RuntimeError("database down")
        if sql.lstrip().startswith("INSERT"):
            self.rows.extend(zip(params[1], params[3], params[4]))
        return self

    def fetchone(self):
        return {"stop_requested_by": None, "force_stop_requested": False}


def test_slow_database_pauses_the_reader_instead_of_dropping(monkeypatch):
    monkeypatch.setattr(il, "BACKLOG", 50)
    log = il._Log("r" * 32, 10_000)
    done = threading.Event()

    def reader():
        for i in range(500):
            log.add("output", str(i))
        done.set()
    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set() and log.backlog == 50   # waiting on us, nothing lost
    conn = FakeConn()
    while not done.is_set() or log.backlog:
        log.flush(conn)
        time.sleep(0.005)
    assert [m for _, _, m in conn.rows] == [str(i) for i in range(500)]


def test_database_outage_skips_output_and_says_so(monkeypatch):
    monkeypatch.setattr(il, "BACKLOG", 10)
    monkeypatch.setattr(il, "MAX_PENDING", 20)
    log = il._Log("r" * 32, 10_000)
    with pytest.raises(RuntimeError):
        log.flush(FakeConn(fail=True))
    for i in range(30):          # doesn't block: the database is down
        log.add("output", str(i))
    conn = FakeConn()
    log.flush(conn)
    msgs = [m for _, _, m in conn.rows]
    assert msgs[:20] == [str(i) for i in range(20)]
    assert "10 lines were skipped" in msgs[-1]


# ---------------------------------------------------------------------------
# Real runs
# ---------------------------------------------------------------------------

USER = {"user": {"email": "tester@example.com", "name": "Tester, Tess [X]"}, "role": "admin"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not DSN or os.name != "posix":
        pytest.skip("INGESTION_TEST_PG_DSN not set (or not Linux)")
    import requests
    import uvicorn
    from fastapi import Depends, FastAPI

    import auth
    import db

    db.migrate()
    work = tmp_path_factory.mktemp("work")
    scripts = {
        "ok.sh": """
            echo "env=$IL_ENV args=$*"
            for i in $(seq 1 5); do echo "line $i"; done
            echo "to stderr" >&2
            env | grep -c SMARTHUB_DB_DSN || true
        """,
        "fail.sh": "echo about to fail; exit 3",
        "slow.sh": """
            sleep 300 &
            echo "child $!"
            for i in $(seq 1 600); do echo "tick $i"; sleep 0.5; done
        """,
        "flood.sh": "seq 1 30000",
        "torrent.sh": "seq 1 250000",
        "detach.sh": "sleep 30 & echo started",
        "leak.sh": 'echo "connecting with $IL_PASSWORD to $1"',
    }
    py_scripts = {
        # behaves like main.py: Ctrl-C finishes in-flight work, a second one forces exit
        "graceful.py": """
            import resource, signal, sys, time
            stop = False
            def on_int(sig, frame):
                global stop
                if stop:
                    print("Second Ctrl-C, force exit.", flush=True); sys.exit(1)
                print("Ctrl-C received, finishing in-flight documents then stopping...", flush=True)
                stop = True
            signal.signal(signal.SIGINT, on_int)
            print("nofile", resource.getrlimit(resource.RLIMIT_NOFILE)[0], flush=True)
            i = 0
            while True:
                i += 1
                print(f"doc {i}", flush=True)
                if stop:
                    time.sleep(0.5); print("in-flight done", flush=True); sys.exit(0)
                time.sleep(0.1)
        """,
        "stubborn.py": """
            import signal, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                print("still going", flush=True); time.sleep(0.1)
        """,
        "longlog.py": "for i in range(1, 5001): print(f'line {i}')",
    }
    for name, body in py_scripts.items():
        (work / name).write_text(textwrap.dedent(body))
    bare = tmp_path_factory.mktemp("bare")
    (bare / "requirements.txt").write_text("")
    (work / "venv" / "bin").mkdir(parents=True)
    (work / "venv" / "bin" / "python").symlink_to(sys.executable)
    alt = work / "alt"
    alt.mkdir()
    (alt / "ok.sh").write_text("#!/bin/bash\necho \"alt folder $(pwd) env=$1\"\n")
    (alt / "ok.sh").chmod(0o755)
    (work / "empty").mkdir()
    outside = tmp_path_factory.mktemp("outside")
    (outside / "ok.sh").write_text("#!/bin/bash\necho escaped\n")
    (outside / "ok.sh").chmod(0o755)
    (work / "escape").symlink_to(outside)
    for name, body in scripts.items():
        path = work / name
        path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
        path.chmod(0o755)

    def mk(id_, script, **extra):
        return {"id": id_, "name": id_.title(), "workdir": str(work), "command": ["./" + script, "{env}"],
                "inputs": [{"name": "env", "type": "select", "options": ["DEV", "QA"], "default": "DEV"},
                           {"name": "password", "type": "secret", "required": False}], **extra}

    config_path = work / "ingestion_lab.yaml"
    config = {"ingestion_lab": {"max_log_lines": 1000000, "steps": [
        mk("ok", "ok.sh"), mk("fail", "fail.sh"), mk("slow", "slow.sh", stop_grace_seconds=2),
        mk("flood", "flood.sh"), mk("detach", "detach.sh"),
        mk("timeout", "slow.sh", timeout_minutes=0.03, stop_grace_seconds=1),
        mk("capped", "flood.sh", one_at_a_time=False, log_keep="first"),
        mk("solo", "slow.sh", one_at_a_time="all_servers", stop_grace_seconds=2),
        {"id": "novenv", "name": "No venv", "workdir": str(bare), "command": ["@python", "-c", "print('made it')"]},
        {"id": "profcat", "name": "Profile cat", "workdir": str(work), "command": ["cat", "{profile}"],
         "inputs": [{"name": "profile", "type": "profile"}]},
        {"id": "autopy", "name": "Auto python", "workdir": str(work), "pidfile": "autopy.pid",
         "command": ["@python", "-c", "import sys; print('running on', sys.executable)"]},
        mk("torrent", "torrent.sh"), mk("leak", "leak.sh"),
        {"id": "graceful", "name": "Graceful", "workdir": str(work), "command": [sys.executable, "graceful.py"],
         "stop_signal": "INT", "stop_grace_seconds": 30, "pidfile": "graceful.pid", "log_file": "logs/graceful.log",
         "nofile_limit": 1024},
        {"id": "stubborn", "name": "Stubborn", "workdir": str(work), "command": [sys.executable, "stubborn.py"],
         "stop_signal": "INT", "stop_grace_seconds": 60},
        {"id": "longlog", "name": "Long log", "workdir": str(work), "command": [sys.executable, "longlog.py"],
         "log_keep": "last", "max_log_lines": 1000},
        {**{k: v for k, v in mk("anyfolder", "ok.sh").items() if k != "workdir"}, "folder_choice": True,
         "folder_roots": [str(work)], "folder_marker": "ok.sh",
         "inputs": mk("x", "ok.sh")["inputs"] + [{"name": "profile", "type": "profile", "required": False,
                                                  "templates": "profiles/*.json"}]},
        {**mk("picker", "ok.sh"), "folder_choice": True, "folder_roots": [str(work)], "folder_marker": "ok.sh",
         "pidfile": "picker.pid"},
        {"id": "shellprof", "name": "Shell profile", "workdir": str(work), "shell": 'printf "%s\\n" "$IL_PROFILE"',
         "inputs": [{"name": "profile", "type": "profile"}]},
        {"id": "shell", "name": "Shell", "workdir": str(work), "shell": 'echo "shell env=$IL_ENV"; pwd',
         "inputs": [{"name": "env", "type": "text", "default": "DEV"}]},
    ]}}
    config_path.write_text(json.dumps(config["ingestion_lab"]))   # JSON is valid YAML
    il.set_ingestion_lab_dependencies(str(config_path))

    perms = {"viewIngestionLab": True, "runIngestionLab": True}
    app = FastAPI()
    app.include_router(il.ingestion_router, dependencies=[Depends(auth.require_permission("viewIngestionLab"))])
    app.dependency_overrides[auth.require_auth] = lambda: {**USER, "permissions": perms}

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}/api/ingestion-lab"
    yield {"base": base, "requests": requests, "perms": perms, "work": work, "config_path": config_path,
           "config": config}
    srv.should_exit = True
    t.join(5)
    with db.transaction() as conn:
        conn.execute(f'DROP SCHEMA "{SCHEMA}" CASCADE')


def gone(pid, wait=3.0):
    """True once pid has exited (a zombie waiting for init to reap it counts as exited)."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


def start(srv, step_id, inputs=None, expect=200):
    r = srv["requests"].post(f"{srv['base']}/runs", json={"step_id": step_id, "inputs": inputs or {}})
    assert r.status_code == expect, r.text
    return r.json()


def watch(srv, run_id, after=0, timeout=60, until=None):
    """Read the stream; returns (events, done)."""
    events, done = [], None
    with srv["requests"].get(f"{srv['base']}/runs/{run_id}/stream", params={"after": after},
                             stream=True, timeout=timeout) as r:
        assert r.status_code == 200
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            evt = json.loads(raw[6:])
            if evt.get("type") == "done":
                done = evt
                break
            events.append(evt)
            if until and until(evt):
                break
    return events, done


@needs_db
def test_successful_run(server):
    run_id = start(server, "ok", {"env": "QA", "password": "hunter2"})["run_id"]
    t0 = time.monotonic()
    events, done = watch(server, run_id)
    assert done["status"] == "success" and done["returncode"] == 0
    assert time.monotonic() - t0 < 5
    out = [e["message"] for e in events if e["type"] == "output"]
    assert out[:6] == ["env=QA args=QA", "line 1", "line 2", "line 3", "line 4", "line 5"]
    assert "to stderr" in out
    assert out[-1] == "0", "the portal's database DSN leaked into the command's environment"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all("hunter2" not in e.get("message", "") for e in events)
    run = server["requests"].get(f"{server['base']}/runs/{run_id}").json()
    assert run["status"] == "success" and run["inputs"]["password"] == il.MASK
    assert run["started_by"] == "Tester, Tess" and "hunter2" not in run["command"]
    log = server["requests"].get(f"{server['base']}/runs/{run_id}/log.txt")
    assert log.status_code == 200 and "line 5" in log.text and "attachment" in log.headers["content-disposition"]


@needs_db
def test_resume_after_seq(server):
    run_id = start(server, "ok")["run_id"]
    events, _ = watch(server, run_id)
    middle = events[len(events) // 2]["seq"]
    later, done = watch(server, run_id, after=middle)
    assert done["status"] == "success"
    assert [e["seq"] for e in later] == [e["seq"] for e in events if e["seq"] > middle]


@needs_db
def test_failed_run(server):
    run_id = start(server, "fail")["run_id"]
    events, done = watch(server, run_id)
    assert done["status"] == "failed" and done["returncode"] == 3
    assert any("exit code 3" in e.get("message", "") for e in events)


@needs_db
def test_shell_step_gets_inputs_as_env(server):
    run_id = start(server, "shell", {"env": "it's; $(rm -rf ~)"})["run_id"]
    events, done = watch(server, run_id)
    out = [e["message"] for e in events if e["type"] == "output"]
    assert done["status"] == "success"
    assert out[0] == "shell env=it's; $(rm -rf ~)" and out[1] == str(server["work"])


@needs_db
def test_stop_kills_the_whole_process_group(server):
    run_id = start(server, "slow")["run_id"]
    events, _ = watch(server, run_id, until=lambda e: e.get("message", "").startswith("tick 2"))
    child = int(next(e["message"] for e in events if e.get("message", "").startswith("child ")).split()[1])
    os.kill(child, 0)   # alive
    again = start(server, "slow", expect=409).get("detail")
    assert again["run_id"] == run_id
    r = server["requests"].post(f"{server['base']}/runs/{run_id}/stop")
    assert r.status_code == 200
    t0 = time.monotonic()
    _, done = watch(server, run_id)
    assert done["status"] == "stopped"
    assert time.monotonic() - t0 < 5
    assert gone(child), "a process the step started survived the stop"
    assert server["requests"].post(f"{server['base']}/runs/{run_id}/stop").status_code == 409


@needs_db
def test_timeout(server):
    run_id = start(server, "timeout")["run_id"]
    events, done = watch(server, run_id)
    assert done["status"] == "timed_out"
    assert any("time limit of 1s" in e.get("message", "") for e in events)


@needs_db
def test_flood_keeps_every_line_in_order(server):
    run_id = start(server, "flood")["run_id"]
    t0 = time.monotonic()
    events, done = watch(server, run_id)
    assert done["status"] == "success"
    out = [e["message"] for e in events if e["type"] == "output"]
    assert out == [str(i) for i in range(1, 30001)]
    assert time.monotonic() - t0 < 20


@needs_db
def test_output_faster_than_the_database_loses_nothing(server):
    run_id = start(server, "torrent")["run_id"]
    events, done = watch(server, run_id, timeout=120)
    assert done["status"] == "success"
    out = [e["message"] for e in events if e["type"] == "output"]
    assert len(out) == 250000 and out[0] == "1" and out[-1] == "250000"
    assert not any("skipped" in e.get("message", "") for e in events)


@needs_db
def test_log_cap(server):
    il._cfg.settings["max_log_lines"] = 100
    try:
        run_id = start(server, "capped")["run_id"]
        events, done = watch(server, run_id)
    finally:
        il._cfg.settings["max_log_lines"] = 1000000
    assert done["status"] == "success"
    assert len([e for e in events if e["type"] == "output"]) == 100
    run = server["requests"].get(f"{server['base']}/runs/{run_id}").json()
    assert run["log_truncated"] and run["log_lines"] == 30000


@needs_db
def test_background_child_does_not_hang_the_run(server):
    run_id = start(server, "detach")["run_id"]
    t0 = time.monotonic()
    _, done = watch(server, run_id)
    assert done["status"] == "success" and time.monotonic() - t0 < 6


@needs_db
def test_bad_inputs_and_unknown_step(server):
    r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "ok", "inputs": {"env": "PRD"}})
    assert r.status_code == 422 and "env" in r.json()["detail"]["fields"]
    r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "nope"})
    assert r.status_code == 404
    assert server["requests"].get(f"{server['base']}/runs/not-a-run").status_code == 404


@needs_db
def test_run_permission_is_required(server):
    server["perms"]["runIngestionLab"] = False
    try:
        assert server["requests"].post(f"{server['base']}/runs", json={"step_id": "ok"}).status_code == 403
        steps = server["requests"].get(f"{server['base']}/steps").json()
        assert steps["can_run"] is False
    finally:
        server["perms"]["runIngestionLab"] = True


@needs_db
def test_history_filters_and_pages(server):
    for _ in range(3):
        watch(server, start(server, "ok")["run_id"])
    page = server["requests"].get(f"{server['base']}/runs", params={"step_id": "ok", "limit": 2}).json()
    assert len(page["runs"]) == 2 and page["next"]
    more = server["requests"].get(f"{server['base']}/runs", params={"step_id": "ok", "limit": 2,
                                                                    "before": page["next"]}).json()
    assert {r["id"] for r in more["runs"]}.isdisjoint({r["id"] for r in page["runs"]})
    failed = server["requests"].get(f"{server['base']}/runs", params={"status": "failed"}).json()["runs"]
    assert failed and all(r["status"] == "failed" for r in failed)
    mine = server["requests"].get(f"{server['base']}/runs", params={"mine": "true"}).json()["runs"]
    assert mine and all(r["started_by_email"] == USER["user"]["email"] for r in mine)


@needs_db
def test_dead_server_runs_are_marked_lost(server):
    import db
    with db.transaction() as conn:
        conn.execute("""INSERT INTO ingestion_runs (id, step_id, step_name, host, heartbeat_at, lock_key)
                        VALUES (?, 'ok', 'Ok', 'gone-host', now() - interval '5 minutes', 'ok')""", ("f" * 32,))
    active = server["requests"].get(f"{server['base']}/active").json()
    assert all(r["id"] != "f" * 32 for r in active)
    run = server["requests"].get(f"{server['base']}/runs/{'f' * 32}").json()
    assert run["status"] == "lost"
    watch(server, start(server, "ok")["run_id"])   # the lock was released


SECOND_SERVER = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import uvicorn
from fastapi import Depends, FastAPI
import auth, ingestion_lab as il
il.set_ingestion_lab_dependencies(sys.argv[2])
app = FastAPI()
app.include_router(il.ingestion_router, dependencies=[Depends(auth.require_permission("viewIngestionLab"))])
app.dependency_overrides[auth.require_auth] = lambda: {"user": {"email": "other@example.com", "name": "Other"},
    "role": "admin", "permissions": {"viewIngestionLab": True, "runIngestionLab": True}}
app.router.add_event_handler("shutdown", il.shutdown)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[3]), log_level="warning")
"""


def second_server(server, tmp_path):
    import subprocess
    script = tmp_path / "second.py"
    script.write_text(SECOND_SERVER)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, str(script), backend, str(server["config_path"]), str(port)],
                            env={**os.environ})
    other = {**server, "base": f"http://127.0.0.1:{port}/api/ingestion-lab"}
    for _ in range(100):
        try:
            if server["requests"].get(f"{other['base']}/active", timeout=1).ok:
                break
        except Exception:
            pass
        time.sleep(0.1)
    return proc, other


@needs_db
def test_server_restart_stops_its_runs_and_marks_them_lost(server, tmp_path):
    proc, other = second_server(server, tmp_path)
    try:
        run_id = start(other, "slow")["run_id"]
        events, _ = watch(server, run_id, until=lambda e: e.get("message", "").startswith("tick 2"))
        child = int(next(e["message"] for e in events if e.get("message", "").startswith("child ")).split()[1])
        proc.terminate()   # SIGTERM: what systemd sends on restart
        proc.wait(15)
        _, done = watch(server, run_id, timeout=20)
        assert done["status"] == "lost"
        assert gone(child), "the step kept running after its server went away"
        log = server["requests"].get(f"{server['base']}/runs/{run_id}/log.txt").text
        assert "portal server restarted" in log and "exit code -15" not in log
        start(server, "slow", expect=200)   # the step isn't stuck as 'running'
        server["requests"].post(f"{server['base']}/runs/{server['requests'].get(server['base'] + '/active').json()[0]['id']}/stop")
    finally:
        if proc.poll() is None:
            proc.kill()


@needs_db
def test_second_server_watches_and_stops_instantly(server, tmp_path):
    proc, other = second_server(server, tmp_path)
    try:
        def live_watch(run_id):
            """Watch on the other server from the start; note when 'done' arrives."""
            box = {"events": [], "done": None, "at": None}

            def go():
                events, done = watch(other, run_id, timeout=60)
                box.update(events=events, done=done, at=time.monotonic())
            th = threading.Thread(target=go, daemon=True)
            th.start()
            return box, th

        # natural finish: 'done' must reach the other server right away
        run_id = start(server, "ok")["run_id"]
        box, th = live_watch(run_id)
        th.join(30)
        finished = server["requests"].get(f"{server['base']}/runs/{run_id}").json()
        assert box["done"]["status"] == "success" and finished["status"] == "success"

        # stop asked on the other server while it's watching
        for _ in range(3):
            run_id = start(server, "slow")["run_id"]
            seen = {"ticks": False}
            box, th = live_watch(run_id)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                r = server["requests"].get(f"{server['base']}/runs/{run_id}").json()
                if r["log_lines"] >= 3:
                    seen["ticks"] = True
                    break
                time.sleep(0.05)
            assert seen["ticks"]
            time.sleep(0.3)
            t0 = time.monotonic()
            assert server["requests"].post(f"{other['base']}/runs/{run_id}/stop").status_code == 200
            th.join(10)
            assert box["done"]["status"] == "stopped"
            assert box["at"] - t0 < 0.6, "the other server only noticed through its fallback poll"
    finally:
        proc.terminate()
        proc.wait(10)


@needs_db
def test_graceful_stop_sends_ctrl_c_and_lets_work_finish(server):
    pidfile = server["work"] / "graceful.pid"
    run_id = start(server, "graceful")["run_id"]
    events, _ = watch(server, run_id, until=lambda e: e.get("message", "") == "doc 3")
    out = [e["message"] for e in events if e["type"] == "output"]
    assert out[0] == "nofile 1024"
    run_pid = int(pidfile.read_text())
    assert gone(run_pid, wait=0) is False   # the pidfile names the live run
    t0 = time.monotonic()
    assert server["requests"].post(f"{server['base']}/runs/{run_id}/stop").status_code == 200
    events, done = watch(server, run_id)
    msgs = [e.get("message", "") for e in events]
    assert done["status"] == "stopped" and time.monotonic() - t0 < 5
    assert any("Ctrl-C received" in m for m in msgs) and "in-flight done" in msgs
    assert not any("Force-stopped" in m for m in msgs)
    assert not pidfile.exists()   # cleaned up after the run
    copy = (server["work"] / "logs" / "graceful.log").read_text()
    assert "doc 3" in copy and "in-flight done" in copy


@needs_db
def test_force_stop_kills_a_step_that_ignores_ctrl_c(server):
    run_id = start(server, "stubborn")["run_id"]
    watch(server, run_id, until=lambda e: e.get("message") == "still going")
    assert server["requests"].post(f"{server['base']}/runs/{run_id}/stop").status_code == 200
    time.sleep(1.0)
    run = server["requests"].get(f"{server['base']}/runs/{run_id}").json()
    assert run["status"] == "running" and run["stopping"]   # waiting out its grace period
    t0 = time.monotonic()
    r = server["requests"].post(f"{server['base']}/runs/{run_id}/stop", params={"force": "true"})
    assert r.status_code == 200 and r.json()["force"] is True
    events, done = watch(server, run_id)
    assert done["status"] == "stopped" and time.monotonic() - t0 < 3
    assert any("Force-stopped by" in e.get("message", "") for e in events)


@needs_db
def test_pidfile_blocks_a_run_started_outside_the_portal(server):
    import subprocess
    pidfile = server["work"] / "graceful.pid"
    outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pidfile.write_text(f"{outside.pid}\n")
        r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "graceful"})
        assert r.status_code == 409 and "outside the portal" in r.json()["detail"]["message"]
    finally:
        outside.kill()
        outside.wait()
    # dead pid, or a live pid that's some other program (recycled): both are stale
    other = subprocess.Popen(["sleep", "30"])
    try:
        for pid in (outside.pid, other.pid):
            pidfile.write_text(str(pid))
            run_id = start(server, "graceful")["run_id"]
            watch(server, run_id, until=lambda e: e.get("message") == "doc 2")
            server["requests"].post(f"{server['base']}/runs/{run_id}/stop", params={"force": "true"})
            watch(server, run_id)
    finally:
        other.kill()
        other.wait()


@needs_db
def test_keep_last_trims_the_oldest_lines(server):
    run_id = start(server, "longlog")["run_id"]
    _, done = watch(server, run_id)
    assert done["status"] == "success"
    events, _ = watch(server, run_id)
    seqs = [e["seq"] for e in events]
    out = [e["message"] for e in events if e["type"] == "output"]
    assert seqs[:il.KEEP_HEAD] == list(range(1, il.KEEP_HEAD + 1))   # the head is kept
    assert out[-1] == "line 5000" and "line 4100" in out           # so is the latest output
    assert "line 1000" not in out and len(events) < 1300             # the middle is gone
    run = server["requests"].get(f"{server['base']}/runs/{run_id}").json()
    assert run["log_truncated"] and run["log_lines"] == 5000
    text = server["requests"].get(f"{server['base']}/runs/{run_id}/log.txt").text
    assert "printed 5,000 lines; not all of them were saved" in text.split("\n\n", 1)[0]


@needs_db
def test_folder_picker_lists_and_runs_in_a_chosen_folder(server):
    base = server["base"]
    r = server["requests"].get(f"{base}/folders", params={"step_id": "picker", "server_id": "local"}).json()
    names = {d["name"]: d["marked"] for d in r["dirs"]}
    assert r["path"] == str(server["work"]) and r["marked_here"] is True and r["parent"] is None
    assert names["alt"] is True and names["empty"] is False and "escape" in names
    alt = str(server["work"] / "alt")
    sub = server["requests"].get(f"{base}/folders", params={"step_id": "picker", "server_id": "local", "path": alt}).json()
    assert sub["parent"] == str(server["work"]) and sub["marked_here"] is True
    r = server["requests"].post(f"{base}/runs", json={"step_id": "picker", "inputs": {"env": "QA"}, "workdir": alt})
    assert r.status_code == 200, r.text
    events, done = watch(server, r.json()["run_id"])
    out = [e["message"] for e in events if e.get("type") == "output"]
    assert done["status"] == "success" and out[0] == f"alt folder {alt} env=QA"
    run = server["requests"].get(f"{base}/runs/{r.json()['run_id']}").json()
    assert run["workdir"] == alt


@needs_db
@pytest.mark.parametrize("folder, why", [
    ("/tmp", "Only folders under"),
    ("{work}/alt/../..", "Only folders under"),
    ("relative/path", "full path"),
    ("{work}/escape", "really"),
])
def test_folder_picker_refuses_folders_outside_the_roots(server, folder, why):
    folder = folder.format(work=server["work"])
    r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "picker", "inputs": {}, "workdir": folder})
    assert r.status_code == 422 and why in r.json()["detail"]["fields"]["workdir"]
    r = server["requests"].get(f"{server['base']}/folders", params={"step_id": "picker", "server_id": "local", "path": folder})
    assert r.status_code == 422


@needs_db
def test_steps_without_folder_choice_keep_their_folder(server):
    r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "ok", "inputs": {}, "workdir": "/tmp"})
    assert r.status_code == 422 and "own folder" in r.json()["detail"]["fields"]["workdir"]
    r = server["requests"].get(f"{server['base']}/folders", params={"step_id": "ok", "server_id": "local"})
    assert r.status_code == 409


def test_with_workdir_moves_relative_files():
    s = step(pidfile="run.pid", log_file="logs/x.log", folder_choice=True, folder_roots=["/data"], workdir="/data/a")
    t = s.with_workdir("/data/b")
    assert (t.workdir, t.pidfile, t.log_file) == ("/data/b", "/data/b/run.pid", "/data/b/logs/x.log")
    assert s.pidfile == "/data/a/run.pid"   # the original is untouched
    with pytest.raises(il.ConfigError, match="whole disk"):
        step(folder_choice=True, folder_roots=["/"])


@needs_db
def test_config_changes_are_picked_up(server):
    config = json.loads(json.dumps(server["config"]))
    config["ingestion_lab"]["steps"].append({"id": "added", "name": "Added", "workdir": str(server["work"]),
                                             "command": ["./ok.sh"]})
    config["ingestion_lab"]["steps"].append({"id": "broken", "workdir": "relative"})
    server["config_path"].write_text(json.dumps(config["ingestion_lab"]))
    os.utime(server["config_path"], (time.time() + 5, time.time() + 5))
    il._cfg.checked = 0
    steps = server["requests"].get(f"{server['base']}/steps").json()
    assert "added" in {s["id"] for s in steps["steps"]}
    assert any("absolute" in e for e in steps["errors"])

def test_secrets_printed_by_the_command_are_masked():
    log = il._Log("r" * 32, 1000)
    log.secrets = ["hunter2"]
    log.add("output", "login hunter2 ok hunter2")
    assert log.pending[-1][3] == f"login {il.MASK} ok {il.MASK}"


def test_allow_dash_reaches_the_page():
    s = step(inputs=[{"name": "args", "allow_dash": True}, {"name": "plain"}], command=["x", "{args}", "{plain}"])
    pub = {i["name"]: i for i in s.public()["inputs"]}
    assert pub["args"]["allow_dash"] is True and pub["plain"]["allow_dash"] is False


@needs_db
def test_secret_echoed_by_the_step_is_masked(server):
    run_id = start(server, "leak", {"env": "QA", "password": "s3cr3t-value"})["run_id"]
    events, done = watch(server, run_id)
    assert done["status"] == "success"
    assert f"connecting with {il.MASK} to QA" in [e["message"] for e in events]
    text = server["requests"].get(f"{server['base']}/runs/{run_id}/log.txt").text
    assert "s3cr3t-value" not in text


@needs_db
def test_log_download_streams_every_line(server):
    run_id = start(server, "capped")["run_id"]
    _, done = watch(server, run_id)
    assert done["status"] == "success"
    r = server["requests"].get(f"{server['base']}/runs/{run_id}/log.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    nums = [line.split("  ", 1)[1] for line in r.text.splitlines() if "  " in line]
    nums = [int(n) for n in nums if n.isdigit()]
    assert nums == list(range(1, 30001))   # crosses the download batch size


@needs_db
def test_stop_on_a_dead_run_says_so(server):
    import db
    with db.transaction() as conn:
        conn.execute("""INSERT INTO ingestion_runs (id, step_id, step_name, host, heartbeat_at)
                        VALUES (?, 'ok', 'Ok', 'gone-host', now() - interval '5 minutes')""", ("d" * 32,))
    r = server["requests"].post(f"{server['base']}/runs/{'d' * 32}/stop")
    assert r.status_code == 409
    assert server["requests"].get(f"{server['base']}/runs/{'d' * 32}").json()["status"] == "lost"


@needs_db
def test_old_runs_are_cleared(server):
    import asyncio
    import db
    with db.transaction() as conn:
        conn.execute("""INSERT INTO ingestion_runs (id, step_id, step_name, started_at, finished_at, status)
                        VALUES (?, 'ok', 'Ok', now() - interval '400 days', now() - interval '400 days', 'success')""",
                     ("c" * 32,))
        conn.execute("INSERT INTO ingestion_run_logs (run_id, seq, ts, type, message) VALUES (?, 1, now(), 'output', 'x')",
                     ("c" * 32,))
    keep = il._cfg.settings["keep_days"]
    try:
        il._cfg.settings["keep_days"] = 0   # the default: history is kept forever
        asyncio.run(il._cleanup_old())
        assert server["requests"].get(f"{server['base']}/runs/{'c' * 32}").status_code == 200
        il._cfg.settings["keep_days"] = 90
        asyncio.run(il._cleanup_old())
        assert server["requests"].get(f"{server['base']}/runs/{'c' * 32}").status_code == 404
    finally:
        il._cfg.settings["keep_days"] = keep


@needs_db
def test_runs_per_server_are_limited(server, monkeypatch):
    monkeypatch.setitem(il._cfg.settings, "max_concurrent_runs", 1)
    first = start(server, "slow")["run_id"]
    try:
        r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "timeout", "inputs": {}})
        assert r.status_code == 429 and "limit is 1" in r.json()["detail"]
    finally:
        server["requests"].post(f"{server['base']}/runs/{first}/stop", params={"force": "true"})
        watch(server, first)
    monkeypatch.setitem(il._cfg.settings, "max_concurrent_runs", 4)
    watch(server, start(server, "ok")["run_id"])


@pytest.mark.parametrize("bad", [{}, "abc", "5", True, float("nan"), float("inf")])
def test_number_bounds_must_be_numbers(bad):
    with pytest.raises(il.ConfigError, match="must be a number"):
        il.InputSpec({"name": "n", "type": "number", "min": bad}, "x")
    with pytest.raises(il.ConfigError, match="min is bigger than max"):
        il.InputSpec({"name": "n", "type": "number", "min": 5, "max": 1}, "x")
    with pytest.raises(il.ConfigError, match="max_length"):
        il.InputSpec({"name": "t", "max_length": -1}, "x")


def test_whole_numbers_stay_exact():
    spec = il.InputSpec({"name": "n", "type": "number", "integer": True, "min": 1}, "x")
    assert spec.validate("9007199254740993") == "9007199254740993"
    assert spec.validate("08") == "8" and spec.validate("1e3") == "1000"
    with pytest.raises(ValueError, match="at least 1"):
        spec.validate("0")

def test_one_at_a_time_scope_and_advanced_inputs():
    s = step(one_at_a_time="all_servers", inputs=[{"name": "deep", "required": False, "advanced": True}],
             command=["python3", "x.py"])
    assert s.exclusive and s.lock_everywhere
    pub = s.public()
    assert pub["one_at_a_time_everywhere"] and pub["inputs"][0]["advanced"]
    assert not step(one_at_a_time=False).exclusive and not step().lock_everywhere
    with pytest.raises(il.ConfigError, match="one_at_a_time"):
        step(one_at_a_time="yes")


def test_example_config_loads_cleanly():
    """The shipped ingestion_lab example must load with no errors and build the commands main.py expects."""
    section = il._read_config(os.path.join(os.path.dirname(il.__file__), "ingestion_lab.example.yaml"))
    for srv in section["servers"]:
        if not srv.get("local"):
            srv["password"] = "x"
    cfg = il._Config()
    cfg.load(section)
    assert cfg.errors == []
    pipe = cfg.steps["immbot-pipeline"].variants["rdi-1"]
    values, problems = pipe.resolve({"profile": {"version": 1, "chunking": {"max_tokens": 600}}})
    assert not problems
    argv = pipe.argv(values)
    assert "--source" not in argv   # empty source = config.ini's
    assert argv[-2:] == ["--profile", il.FILE_MARK + "profile"]     # the file path goes in at launch
    assert pipe.argv(values, files={"profile": "/x/p.json"})[-2:] == ["--profile", "/x/p.json"]
    assert "--profile" not in pipe.argv(pipe.resolve({})[0])   # no profile: the legacy run
    embed = cfg.steps["embed-existing"].variants["rdi-2"]
    _, problems = embed.resolve({})
    assert "profile" in problems   # main.py refuses --embed-existing without one
    preset = json.loads(embed.resolve(embed.presets[0]["inputs"])[0]["profile"])
    assert preset["existing_chunks"]["vector_column"] == "question_embedding" and preset["embedding"]["dimensions"] == 3072


@needs_db
def test_one_at_a_time_everywhere_locks_on_the_step(server):
    run_id = start(server, "solo")["run_id"]
    with il.db.transaction() as conn:
        assert conn.execute("SELECT lock_key FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()["lock_key"] == "solo"
    again = start(server, "solo", expect=409)["detail"]
    assert again["run_id"] == run_id
    assert server["requests"].post(f"{server['base']}/runs/{run_id}/stop?force=true").status_code == 200
    _, done = watch(server, run_id)
    assert done["status"] == "stopped"


def test_own_config_file_legacy_fallback_and_typos(tmp_path):
    saved = (il._cfg.path, il._cfg.mtime, il._cfg.settings, il._cfg.steps, il._cfg.servers, il._cfg.errors,
             il._cfg.configured)
    try:
        path = tmp_path / "ingestion_lab.yaml"
        legacy = {"steps": [{"id": "old", "workdir": "/tmp", "command": ["true"]}]}
        il.set_ingestion_lab_dependencies(str(path), legacy_section=legacy)
        assert set(il._cfg.steps) == {"old"}                 # no file yet: dq_config.json's section
        path.write_text("# comments are fine\nsteps:\n  - {id: new, workdir: /tmp, command: [\"true\"]}\n")
        il._cfg.checked = 0
        assert set(il._cfg.snapshot()[1]) == {"new"}         # the file wins as soon as it exists
        path.write_text("steps: [oops")
        os.utime(path, (time.time() + 5, time.time() + 5))
        il._cfg.checked = 0
        _, steps, errors, _ = il._cfg.snapshot()
        assert set(steps) == {"new"} and "couldn't be read" in errors[0]
    finally:
        (il._cfg.path, il._cfg.mtime, il._cfg.settings, il._cfg.steps, il._cfg.servers, il._cfg.errors,
         il._cfg.configured) = saved


def test_find_venv_looks_near_the_run_folder_and_never_falls_back(tmp_path):
    deep = tmp_path / "repo" / "a" / "b"
    deep.mkdir(parents=True)
    found, looked = il._find_venv(str(deep))
    assert found is None and len(looked) == 9                       # 3 names x the folder and 2 above
    assert looked[0] == str(deep / ".venv" / "bin" / "python")
    assert str(tmp_path / "repo" / "env" / "bin" / "python") in looked
    py = tmp_path / "repo" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    assert il._find_venv(str(deep))[0] == str(py)
    too_deep = deep / "c"
    too_deep.mkdir()
    assert il._find_venv(str(too_deep))[0] is None


@pytest.mark.skipif(os.name != "posix", reason="bash")
def test_remote_launch_script_uses_the_run_folders_virtualenv(tmp_path):
    import subprocess
    work = tmp_path / "repo"
    (work / "venv" / "bin").mkdir(parents=True)
    fake = work / "venv" / "bin" / "python"
    fake.write_text('#!/bin/sh\necho "venv python $*"\n')
    fake.chmod(0o755)
    srv = il.Server({"id": "far", "host": "h", "username": "u", "password": "p", "vars": {"python": "/nope"}}, 0)
    s = il.Step({"id": "x", "workdir": str(work), "command": ["@python", "-m", "main", "{env}"],
                 "inputs": [{"name": "env", "default": "DEV"}]}, 0)
    s.server = srv
    values, _ = s.resolve({})
    run_id = uuid.uuid4().hex
    script = il._launch_script(run_id, s, values, s.argv(values))
    out = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, timeout=30,
                         env={**os.environ, "HOME": str(tmp_path)}).stdout
    assert f"__IL_PY__ {fake} (virtualenv in {work})" in out, out
    spool = next(line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("__IL_SPOOL__ "))
    exit_file = tmp_path / ".smarthub-ingestion" / f"{run_id}.exit"
    for _ in range(100):
        if exit_file.exists():
            break
        time.sleep(0.05)
    assert open(spool).read().strip() == "venv python -m main DEV"
    fake.unlink()   # no virtualenv any more: the launch refuses instead of picking another python
    res = subprocess.run(["bash", "-s"], input=il._launch_script(uuid.uuid4().hex, s, values, s.argv(values)),
                         capture_output=True, text=True, timeout=30, env={**os.environ, "HOME": str(tmp_path)})
    assert res.returncode == 94 and "No virtualenv" in res.stdout and "__IL_PID__" not in res.stdout


@needs_db
def test_auto_python_runs_the_folders_virtualenv(server):
    run_id = start(server, "autopy")["run_id"]
    events, done = watch(server, run_id)
    assert done["status"] == "success"
    venv_py = str(server["work"] / "venv" / "bin" / "python")
    msgs = [e.get("message", "") for e in events]
    assert any(m.startswith("Command:  " + venv_py + " -c") for m in msgs)
    assert any(m.startswith("Python:   " + venv_py) for m in msgs)
    assert f"running on {venv_py}" in msgs


@needs_db
def test_no_virtualenv_is_an_error_then_one_can_be_made(server):
    detail = start(server, "novenv", expect=409)["detail"]
    assert detail["code"] == "no_venv" and len(detail["looked"]) == 9 and "Looked for" in detail["message"]
    info = server["requests"].get(f"{server['base']}/python", params={"step_id": "novenv", "server_id": "local"}).json()
    assert info["venv"] is None and info["looked"] == detail["looked"] and info["requirements"]
    usable = [p for p in info["pythons"] if p["can_create"]]
    assert usable and all(p["version"].startswith("3.") for p in usable)
    body = {"step_id": "novenv", "server_id": "local", "python": "/bin/sh"}
    assert server["requests"].post(f"{server['base']}/venv", json=body).status_code == 422   # only listed pythons
    body["python"] = usable[0]["path"]
    r = server["requests"].post(f"{server['base']}/venv", json=body)
    assert r.status_code == 200, r.text
    events, done = watch(server, r.json()["run_id"], timeout=180)
    assert done["status"] == "success", [e.get("message") for e in events]
    again = server["requests"].post(f"{server['base']}/venv", json=body)
    assert again.status_code == 409 and "already has a virtualenv" in again.json()["detail"]
    events, done = watch(server, start(server, "novenv")["run_id"])
    assert done["status"] == "success" and "made it" in [e.get("message") for e in events]


@needs_db
def test_step_without_a_workdir_runs_where_the_user_picks(server):
    r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "anyfolder", "inputs": {}})
    assert r.status_code == 422 and r.json()["detail"]["fields"]["workdir"] == "Pick the folder to run in"
    listing = server["requests"].get(f"{server['base']}/folders",
                                     params={"step_id": "anyfolder", "server_id": "local"}).json()
    assert listing["path"] == str(server["work"]) and listing["marked_here"]   # starts at the root
    r = server["requests"].post(f"{server['base']}/runs", json={"step_id": "anyfolder", "inputs": {},
                                                               "workdir": str(server["work"] / "alt")})
    assert r.status_code == 200, r.text
    events, done = watch(server, r.json()["run_id"])
    assert done["status"] == "success"
    assert any(e.get("message", "").startswith(f"alt folder {server['work']}/alt") for e in events)


def test_server_root_is_where_steps_without_a_workdir_run():
    cfg = il._Config()
    cfg.load({"servers": [{"id": "a", "local": True, "root": "/srv/ingest"},
                          {"id": "b", "local": True, "root": ["/data/one", "/data/two"]},
                          {"id": "c", "local": True}],
              "steps": [{"id": "s", "command": ["@python", "-m", "main"]}]})
    assert cfg.errors == ["step 's': set workdir (an absolute path), or a root on the server so people pick the folder (on server 'c')"]
    cfg.load({"servers": [{"id": "a", "local": True, "root": "/srv/ingest"},
                          {"id": "b", "local": True, "root": ["/data/one", "/data/two"]}],
              "steps": [{"id": "s", "command": ["@python", "-m", "main"]}]})
    assert cfg.errors == []
    a, b = cfg.steps["s"].variants["a"], cfg.steps["s"].variants["b"]
    assert a.folder_choice and a.folder_roots == ["/srv/ingest"] and a.workdir == ""
    assert b.folder_roots == ["/data/one", "/data/two"]
    with pytest.raises(il.ConfigError, match="root"):
        il.Server({"id": "x", "local": True, "root": "/"}, 0)


def test_profile_inputs_take_an_object_or_its_json():
    spec = il.InputSpec({"name": "profile", "type": "profile", "profile_mode": "existing", "templates": "profiles/*.json"}, "t")
    assert spec.validate({"version": 1, "embedding": {"batch_size": 8}}) == '{"version":1,"embedding":{"batch_size":8}}'
    assert spec.validate('{ "version": 1 }') == '{"version":1}'
    assert spec.public()["profile_mode"] == "existing" and spec.public()["templates"] == "profiles/*.json"
    for bad, why in (("{oops", "JSON"), ("[1]", "object"), ({"nope": 1}, "unknown"), ({"name": "x" * 140000}, "too big")):
        with pytest.raises(ValueError, match=why):
            spec.validate(bad)
    with pytest.raises(ValueError, match="required"):
        spec.validate("")
    for bad in ({"profile_mode": "other"}, {"templates": "/etc/*.json"}, {"templates": "../*.json"},
                {"templates": "a b/*.json"}, {"templates": "$(rm)"}):
        with pytest.raises(il.ConfigError):
            il.InputSpec({"name": "profile", "type": "profile", **bad}, "t")
    with pytest.raises(il.ConfigError, match="templates"):
        il.InputSpec({"name": "n", "type": "text", "templates": "*.json"}, "t")


@needs_db
def test_templates_read_the_profile_files_in_the_run_folder(server):
    prof = server["work"] / "alt" / "profiles"
    prof.mkdir(exist_ok=True)
    (prof / "b.json").write_text('{"version": 1, "name": "b"}')
    (prof / "a.json").write_text('{"version": 1}')
    (prof / "broken.json").write_text("{not json")
    (prof / "notes.txt").write_text("{}")
    (prof / "dir.json").mkdir(exist_ok=True)
    r = server["requests"].get(f"{server['base']}/templates", params={
        "step_id": "anyfolder", "server_id": "local", "workdir": str(server["work"] / "alt")})
    assert r.status_code == 200, r.text
    got = r.json()["profile"]
    assert [g["path"] for g in got] == ["profiles/a.json", "profiles/b.json", "profiles/broken.json"]
    assert got[1]["profile"] == {"version": 1, "name": "b"} and "error" in got[2]
    r = server["requests"].get(f"{server['base']}/templates", params={"step_id": "anyfolder", "server_id": "local"})
    assert r.status_code == 422   # no folder picked yet
    r = server["requests"].post(f"{server['base']}/runs", json={
        "step_id": "anyfolder", "workdir": str(server["work"] / "alt"),
        "inputs": {"profile": {"version": 1, "chunking": {"max_tokens": 600}}}})
    assert r.status_code == 200, r.text
    events, done = watch(server, r.json()["run_id"])
    msgs = [e.get("message", "") for e in events]
    assert done["status"] == "success" and 'Profile:  {"version":1,"chunking":{"max_tokens":600}}' in msgs


def test_profile_placeholder_must_be_its_own_item():
    with pytest.raises(il.ConfigError, match="own"):
        il.Step({"id": "x", "workdir": "/tmp", "command": ["python3", "--profile={profile}"],
                 "inputs": [{"name": "profile", "type": "profile"}]}, 0)


@needs_db
def test_profile_is_written_to_a_private_file_for_the_run_then_removed(server):
    run_id = start(server, "profcat", {"profile": {"version": 1, "name": "mine"}})["run_id"]
    events, done = watch(server, run_id)
    msgs = [e.get("message", "") for e in events]
    assert done["status"] == "success", msgs
    assert '{"version":1,"name":"mine"}' in msgs          # cat printed the file main.py would read
    path = os.path.join(os.path.expanduser("~"), ".smarthub-ingestion", f"{run_id}-profile.json")
    assert any(m.startswith("Command:  cat " + path) for m in msgs)
    assert not os.path.exists(path)                        # gone once the run ended


@pytest.mark.skipif(os.name != "posix", reason="bash")
def test_remote_launch_script_writes_the_profile_file(tmp_path):
    import subprocess
    srv = il.Server({"id": "far", "host": "h", "username": "u", "password": "p"}, 0)
    s = il.Step({"id": "x", "workdir": str(tmp_path), "command": ["cat", "{profile}"],
                 "inputs": [{"name": "profile", "type": "profile"}]}, 0)
    s.server = srv
    values, _ = s.resolve({"profile": {"version": 1, "name": "it's \"quoted\" $HOME"}})
    run_id = uuid.uuid4().hex
    out = subprocess.run(["bash", "-s"], input=il._launch_script(run_id, s, values, s.argv(values)),
                         capture_output=True, text=True, timeout=30, env={**os.environ, "HOME": str(tmp_path)}).stdout
    spool = next(line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("__IL_SPOOL__ "))
    exit_file = tmp_path / ".smarthub-ingestion" / f"{run_id}.exit"
    for _ in range(100):
        if exit_file.exists():
            break
        time.sleep(0.05)
    assert open(spool).read() == values["profile"]
    written = tmp_path / ".smarthub-ingestion" / f"{run_id}-profile.json"
    assert oct(written.stat().st_mode & 0o777) == "0o600"

def test_portal_secrets_stay_out_of_the_steps_environment(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://:hunter2@cache:6379/0")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k1")
    monkeypatch.setenv("SPEECH_KEY", "k2")
    monkeypatch.setenv("DB_CONNECTION_STRING", "Server=x;Password=y")
    monkeypatch.setenv("PLAIN_URL", "https://example.com/path")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")   # id only; boto3 may still need it from the env
    monkeypatch.setenv("HTTPS_PROXY", "http://svc:pw@proxy:8080")   # pip and the script need it
    s = il.Step({"id": "envy", "workdir": "/tmp", "shell": "env"}, 0)
    s.server = il._default_server()
    env = il._Run("a" * 32, s, {}, il._Log("a" * 32, 1000)).build()[1]
    for gone_key in ("REDIS_URL", "AZURE_OPENAI_KEY", "SPEECH_KEY", "DB_CONNECTION_STRING"):
        assert gone_key not in env
    assert env["PLAIN_URL"] == "https://example.com/path" and env["AWS_ACCESS_KEY_ID"] == "AKIA"
    assert env["HTTPS_PROXY"] == "http://svc:pw@proxy:8080"


def test_the_biggest_profile_still_fits_in_the_environment():
    spec = il.InputSpec({"name": "p" * 40, "type": "profile"}, "t")
    text = spec.validate({"name": "x" * (il.PROFILE_MAX - 11)})
    assert len(text.encode()) == il.PROFILE_MAX
    with pytest.raises(ValueError, match="too big"):
        spec.validate({"name": "x" * (il.PROFILE_MAX - 10)})
    s = il.Step({"id": "big", "workdir": "/tmp", "command": ["true", "{" + "p" * 40 + "}"],
                 "inputs": [{"name": "p" * 40, "type": "profile"}]}, 0)
    env = s.env_inputs({"p" * 40: text})
    if os.name == "posix":
        subprocess_run = __import__("subprocess").run
        assert subprocess_run(["true"], env=env).returncode == 0   # E2BIG would raise here


def test_log_file_needs_one_run_at_a_time():
    with pytest.raises(il.ConfigError, match="one_at_a_time"):
        il.Step({"id": "x", "workdir": "/tmp", "command": ["true"], "log_file": "x.log", "one_at_a_time": False}, 0)
    il.Step({"id": "x", "workdir": "/tmp", "command": ["true"], "log_file": "x.log"}, 0)


@needs_db
def test_requirements_install_into_the_folders_virtualenv(server):
    base, rq = server["base"], server["requests"]
    info = rq.get(f"{base}/python", params={"step_id": "novenv", "server_id": "local"}).json()
    if not info["venv"]:
        py = next(p["path"] for p in info["pythons"] if p["can_create"])
        r = rq.post(f"{base}/venv", json={"step_id": "novenv", "server_id": "local", "python": py})
        assert watch(server, r.json()["run_id"], timeout=180)[1]["status"] == "success"
        info = rq.get(f"{base}/python", params={"step_id": "novenv", "server_id": "local"}).json()
    r = rq.post(f"{base}/requirements", json={"step_id": "novenv", "server_id": "local"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    events, done = watch(server, run_id, timeout=180)
    messages = [e.get("message", "") for e in events]
    assert done["status"] == "success", messages
    assert f"Command:  {info['venv']} -m pip install -r requirements.txt" in messages
    assert "Requirements installed." in messages
    run = rq.get(f"{base}/runs/{run_id}").json()
    assert run["step_id"] == "venv-requirements" and run["workdir"] == info["folder"]
    # no requirements.txt there: refused before anything runs
    r = rq.post(f"{base}/requirements", json={"step_id": "autopy", "server_id": "local"})
    assert r.status_code == 409 and "no requirements.txt" in r.json()["detail"]

@needs_db
def test_shell_step_can_take_a_profile(server):
    events, done = watch(server, start(server, "shellprof", {"profile": {"version": 1, "name": "sh"}})["run_id"])
    assert done["status"] == "success"
    assert '{"version":1,"name":"sh"}' in [e.get("message") for e in events if e["type"] == "output"]


def test_canon_and_signature_spot_the_same_script():
    assert il._canon(["-u", "-m", "main", "--force"]) == ["main", "--force"]
    assert il._canon(["/x/main.py", "--stats"]) == ["main", "--stats"]
    s = il.Step({"id": "x", "workdir": "/tmp", "command": ["@python", "-m", "main", "--rechunk-extracted",
                 ["--max-files", "{n}"]], "inputs": [{"name": "n", "required": False}]}, 0)
    assert il._signature(s) == ["main", "--rechunk-extracted"]
    assert il._signature(il.Step({"id": "y", "workdir": "/tmp", "command": ["true"], "one_at_a_time": False}, 0)) is None


@needs_db
def test_manual_run_outside_the_portal_is_shown_blocks_and_can_be_stopped(server):
    import subprocess
    base, rq = server["base"], server["requests"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("IL_")}
    manual = subprocess.Popen(["nohup", sys.executable, "graceful.py"], cwd=server["work"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(0.5)
        il._OUTSIDE.clear()
        got = rq.get(f"{base}/outside", params={"server_id": "local"}).json()
        assert got["supported"] and [r["pid"] for r in got["runs"].get("graceful", [])] == [manual.pid], got
        assert "graceful.py" in got["runs"]["graceful"][0]["command"]
        assert "stubborn" not in got["runs"]                       # other steps aren't confused with it
        r = rq.post(f"{base}/runs", json={"step_id": "graceful"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "outside"
        assert rq.post(f"{base}/outside/stop", json={"step_id": "graceful", "server_id": "local",
                                                     "pid": os.getpid()}).status_code == 409   # not that one
        r = rq.post(f"{base}/outside/stop", json={"step_id": "graceful", "server_id": "local", "pid": manual.pid})
        assert r.status_code == 200, r.text
        assert manual.wait(10) == 0                                # Ctrl-C: it finished its in-flight work
        il._OUTSIDE.clear()
        assert "graceful" not in rq.get(f"{base}/outside", params={"server_id": "local"}).json()["runs"]
    finally:
        if manual.poll() is None:
            manual.kill()
    run_id = start(server, "graceful")["run_id"]              # the portal's own run isn't "outside"
    watch(server, run_id, until=lambda e: e.get("message") == "doc 2")
    il._OUTSIDE.clear()
    assert "graceful" not in rq.get(f"{base}/outside", params={"server_id": "local"}).json()["runs"]
    rq.post(f"{base}/runs/{run_id}/stop", params={"force": "true"})
    watch(server, run_id)