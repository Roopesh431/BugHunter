
"""Local dashboard API for BugHunter."""
from pathlib import Path
import json
import os
import subprocess
import sys
import threading
import time

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

UI = ROOT / "dashboard"
EVIDENCE = ROOT / "evidence"
APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000").rstrip("/")
PORT = int(os.getenv("DASHBOARD_PORT", "8010"))

app = FastAPI(title="BugHunter Dashboard")
app.mount("/static", StaticFiles(directory=str(UI)), name="static")

lock = threading.Lock()
state = {
    "running": False,
    "kind": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "output": "",
    "error": None,
}


class RunRequest(BaseModel):
    target_url: str | None = None


def reachable(url):
    try:
        return requests.get(url, timeout=2).status_code < 500
    except requests.RequestException:
        return False


def read_json(filename):
    path = EVIDENCE / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run_agent(command, kind):
    with lock:
        state.update(
            running=True,
            kind=kind,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=None,
            exit_code=None,
            output="",
            error=None,
        )

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env={**os.environ, "APP_URL": APP_URL},
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n" + result.stderr

        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=result.returncode,
                output=output[-18000:],
            )
    except Exception as exc:
        with lock:
            state.update(
                running=False,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                exit_code=1,
                error=str(exc),
            )


def start_run(kind, request):
    target = (request.target_url or APP_URL).strip().rstrip("/")

    if not target.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter a valid HTTP or HTTPS URL.")

    with lock:
        if state["running"]:
            raise HTTPException(409, "A test is already running.")

    command = [
        sys.executable,
        "agent.py",
        kind,
        "--url",
        target,
        "--no-issues",
    ]

    threading.Thread(
        target=run_agent,
        args=(command, kind),
        daemon=True,
    ).start()

    return {"ok": True, "message": f"{kind} started"}


@app.get("/")
def index():
    return FileResponse(UI / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    with lock:
        current = dict(state)

    report = read_json("report.json")
    suite = read_json("suite.json")
    bugs = report.get("bugs", [])

    if not isinstance(bugs, list):
        bugs = []

    return {
        "run": current,
        "target_url": APP_URL,
        "target_reachable": reachable(APP_URL),
        "findings": {
            "available": bool(report),
            "count": len(bugs),
            "bugs": bugs,
            "actions": report.get("actions", 0),
            "pages": report.get("pages_visited", []),
        },
        "suite_available": bool(suite),
    }


@app.post("/api/run/explore")
def explore(request: RunRequest):
    return start_run("explore", request)


@app.post("/api/run/verify")
def verify(request: RunRequest):
    return start_run("verify", request)


@app.get("/api/report")
def report():
    data = read_json("report.json")
    if not data:
        raise HTTPException(404, "Run exploration first.")
    return data


@app.get("/api/evidence")
def evidence():
    files = []
    if EVIDENCE.exists():
        for path in EVIDENCE.rglob("*"):
            if path.is_file() and path.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".json", ".md"
            }:
                relative = path.relative_to(EVIDENCE).as_posix()
                files.append({
                    "name": path.name,
                    "path": relative,
                    "url": "/evidence/" + relative,
                })
    return {"files": files}


@app.get("/evidence/{path:path}")
def evidence_file(path: str):
    target = (EVIDENCE / path).resolve()

    if EVIDENCE.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "Evidence file not found.")

    return FileResponse(target)


@app.post("/api/target/start")
def start_target():
    if reachable(APP_URL):
        return {"ok": True, "message": "Target app is already running."}

    from urllib.parse import urlparse
    parsed = urlparse(APP_URL)

    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise HTTPException(
            400,
            "Automatic startup is only supported for a local target."
        )

    port = parsed.port or 8000
    log = EVIDENCE / "target-app.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    with open(log, "a", encoding="utf-8") as output:
        subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "app:app",
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
        )

    for _ in range(20):
        time.sleep(0.5)
        if reachable(APP_URL):
            return {"ok": True, "message": "Demo app started."}

    raise HTTPException(
        503,
        "App did not start. Check evidence/target-app.log."
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
