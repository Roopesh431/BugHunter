"""Intentionally buggy to-do app for testing a Python QA agent.

Run it with:

    python -m uvicorn app:app --port 8000

GROUND TRUTH -- the three deliberate defects
--------------------------------------------
1. POST /api/tasks with an empty title is not guarded. The handler does
   ``title[0]`` to capitalise the first letter, which raises IndexError on "".
   The request returns 500 and the browser console reports a JSON parse error
   because ``res.json()`` receives a plain-text error body.
   Trigger: press "Add Task" with the input empty.

2. The Delete buttons never get a click handler. ``attachDeleteHandlers()``
   queries ``.delete-btn`` but the rendered buttons use ``class="delete"``, so
   the selector matches nothing. Clicking Delete sends no request at all and the
   row stays put. The DELETE endpoint itself is implemented and works.

3. GET /profile has no login check. Without the ``session_user`` cookie,
   ``USERS.get(None)`` returns None and ``user["name"]`` raises TypeError, i.e.
   a 500 instead of a redirect to /login. With the cookie it renders fine.

Everything else (listing, adding a non-empty task, login, logout) works, so the
agent has a healthy baseline to compare failures against.
"""

from __future__ import annotations

import itertools
import threading
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

USERS: dict[str, dict[str, str]] = {
    "demo": {"name": "Demo User", "plan": "free"},
}

_tasks: list[dict[str, Any]] = []
_next_id: itertools.count[int] = itertools.count(1)
_lock = threading.Lock()


def _seed() -> None:
    global _next_id, _tasks
    with _lock:
        _tasks = [
            {"id": next(_next_id), "title": "Buy milk", "done": False},
            {"id": next(_next_id), "title": "Walk the dog", "done": False},
            {"id": next(_next_id), "title": "Ship the QA agent", "done": False},
        ]


@asynccontextmanager
async def lifespan(_: FastAPI):
    _seed()
    yield


app = FastAPI(title="Buggy To-Do", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/tasks")
async def list_tasks() -> dict[str, list[dict[str, Any]]]:
    with _lock:
        return {"tasks": [dict(task) for task in _tasks]}


@app.post("/api/tasks")
async def create_task(request: Request) -> JSONResponse:
    payload = await request.json()
    title = str(payload.get("title", "")).strip()

    # BUG 1: empty titles are never rejected. The capitalisation below assumes
    # at least one character, so "" raises IndexError -> 500.
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
      
    display_title = title[0].upper() + title[1:]

    with _lock:
        task = {"id": next(_next_id), "title": display_title, "done": False}
        _tasks.append(task)

    return JSONResponse(task, status_code=201)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int) -> Response:
    with _lock:
        for index, task in enumerate(_tasks):
            if task["id"] == task_id:
                _tasks.pop(index)
                break
        else:
            raise HTTPException(status_code=404, detail="Task not found")

    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_PAGE


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> str:
    return LOGIN_PAGE.replace("{{error}}", "").replace(
        "{{next}}", request.query_params.get("next", "/profile")
    )


@app.post("/login")
async def login(request: Request) -> Response:
    # Parsed by hand so the app needs nothing beyond fastapi + uvicorn
    # (python-multipart would otherwise be required for request.form()).
    form = parse_qs((await request.body()).decode("utf-8"))
    username = form.get("username", [""])[0].strip()
    target = form.get("next", [""])[0] or "/profile"

    if username not in USERS:
        page = LOGIN_PAGE.replace(
            "{{error}}", '<p class="error">Unknown user. Try "demo".</p>'
        ).replace("{{next}}", target)
        return HTMLResponse(page, status_code=401)

    response = RedirectResponse(target, status_code=303)
    response.set_cookie("session_user", username, httponly=True, samesite="lax")
    return response


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("session_user")
    return response


@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request) -> str:
    username = request.cookies.get("session_user")
    if username not in USERS:
        return RedirectResponse("/login?next=/profile", status_code=303)
    user = USERS[username]

    # BUG 3: a missing session is not handled. user is None here, so reading
    # user["name"] raises TypeError and the page returns 500 instead of
    # redirecting an anonymous visitor to /login.
    return _profile_page(user["name"], user["plan"])


def _profile_page(name: str, plan: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #f4f5f7; color: #1c1f23; }}
  main {{ background: #fff; padding: 32px 36px; border-radius: 12px; width: min(420px, 90vw);
          box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.08); }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  dl {{ display: grid; grid-template-columns: auto 1fr; gap: 8px 16px; margin: 20px 0; }}
  dt {{ color: #667085; }}
  dd {{ margin: 0; font-weight: 600; }}
  a {{ color: #2563eb; }}
</style>
</head>
<body>
<main>
  <h1>Signed in as {name}</h1>
  <dl>
    <dt>Plan</dt><dd>{plan}</dd>
    <dt>Session</dt><dd>cookie-based</dd>
  </dl>
  <a href="/">Back to tasks</a> &middot; <a href="/logout">Log out</a>
</main>
</body>
</html>"""


INDEX_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>To-Do</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; padding: 48px 20px;
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #f4f5f7; color: #1c1f23; }
  .card { width: min(560px, 100%); margin: 0 auto; background: #fff; border-radius: 14px;
          padding: 28px 28px 12px;
          box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.08); }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
  nav { display: flex; gap: 14px; font-size: 14px; }
  nav a { color: #2563eb; text-decoration: none; }
  nav a:hover { text-decoration: underline; }
  form { display: flex; gap: 8px; margin: 22px 0 8px; }
  input[type=text] { flex: 1; min-width: 0; padding: 10px 12px; font: inherit;
                     border: 1px solid #d0d5dd; border-radius: 8px; background: #fff; }
  input[type=text]:focus { outline: 2px solid #bfd3ff; border-color: #2563eb; }
  button { font: inherit; border-radius: 8px; border: 1px solid transparent; cursor: pointer; }
  #add-form button { padding: 10px 16px; background: #2563eb; color: #fff; font-weight: 600; }
  #add-form button:hover { background: #1d4ed8; }
  ul { list-style: none; margin: 12px 0 0; padding: 0; }
  .task { display: flex; align-items: center; gap: 12px; padding: 12px 4px;
          border-top: 1px solid #eaecf0; }
  .task:first-child { border-top: 0; }
  .task .title { flex: 1; overflow-wrap: anywhere; }
  .delete { padding: 6px 12px; font-size: 14px; background: #fff; color: #b42318;
            border-color: #f1c4bf; }
  .delete:hover { background: #fef3f2; }
  .empty { color: #667085; font-size: 15px; padding: 20px 4px; }
</style>
</head>
<body>
  <main class="card">
    <header>
      <h1>To-Do</h1>
      <nav><a href="/profile">Profile</a><a href="/login">Login</a></nav>
    </header>

    <form id="add-form">
      <input id="new-task" type="text" name="title" placeholder="What needs doing?" autocomplete="off">
      <button type="submit">Add Task</button>
    </form>

    <ul id="task-list"></ul>
    <p id="empty-state" class="empty" hidden>Nothing here yet.</p>
  </main>

<script>
  const form = document.getElementById('add-form');
  const input = document.getElementById('new-task');
  const list = document.getElementById('task-list');
  const emptyState = document.getElementById('empty-state');

  function render(tasks) {
    list.innerHTML = '';
    for (const task of tasks) {
      const row = document.createElement('li');
      row.className = 'task';

      const title = document.createElement('span');
      title.className = 'title';
      title.textContent = task.title;

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'delete';
      remove.dataset.id = task.id;
      remove.textContent = 'Delete';

      row.append(title, remove);
      list.append(row);
    }
    emptyState.hidden = tasks.length > 0;
  }

  async function loadTasks() {
    const response = await fetch('/api/tasks');
    const data = await response.json();
    render(data.tasks);
  }

  function attachDeleteHandlers() {
    // BUG 2: the rendered buttons are class="delete", this selector asks for
    // ".delete-btn". It matches nothing, so Delete never does anything.
    document.querySelectorAll('.delete').forEach((button) => {
      button.addEventListener('click', async () => {
        await fetch('/api/tasks/' + button.dataset.id, { method: 'DELETE' });
        await loadTasks();
      });
    });
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();

    // BUG 1: no client-side check for an empty title -- an empty submit is
    // sent to the server, which 500s, and res.json() throws on the plain-text
    // error body. The task row is never added and the input keeps its value.
    const response = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: input.value }),
    });
    await response.json();

    input.value = '';
    await loadTasks();
    attachDeleteHandlers();
    input.focus();
  });

  loadTasks().then(attachDeleteHandlers);
</script>
</body>
</html>"""


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #f4f5f7; color: #1c1f23; }
  form { background: #fff; padding: 32px 36px; border-radius: 12px; width: min(360px, 90vw);
         box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.08); }
  h1 { font-size: 20px; margin: 0 0 20px; }
  input { width: 100%; padding: 10px 12px; font: inherit; border: 1px solid #d0d5dd;
          border-radius: 8px; margin-bottom: 12px; }
  button { width: 100%; padding: 10px 16px; font: inherit; font-weight: 600; color: #fff;
           background: #2563eb; border: 0; border-radius: 8px; cursor: pointer; }
  .hint { color: #667085; font-size: 14px; margin: 14px 0 0; }
  .error { color: #b42318; font-size: 14px; margin: 0 0 12px; }
</style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Sign in</h1>
    {{error}}
    <input type="text" name="username" placeholder="demo" autocomplete="username" autofocus>
    <input type="hidden" name="next" value="{{next}}">
    <button type="submit">Sign in</button>
    <p class="hint">Demo account: <code>demo</code></p>
  </form>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)