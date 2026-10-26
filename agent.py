"""Explore a running web app, report the bugs it finds, then verify the fixes.

Two commands:

    python agent.py explore     # crawl the app, find bugs, file GitHub issues
    python agent.py verify      # replay the saved suite, close issues that pass

`explore` behaves like a curious human tester: it opens the app, walks every page
it can reach, clicks every button and link, and pumps edge-case values into every
form field. Each failure is captured with a screenshot and, after an LLM pass,
summarised into a readable title and repro steps. One GitHub issue is opened per
bug, skipping titles that are already open.

`explore` also writes a suite file (`evidence/suite.json`). Every bug becomes a
named test case with recorded, deterministic replay steps: the URL to open, the
control to click (by accessible role/name, with a CSS path as a fallback) and the
exact payload used. `verify` re-runs those same cases against the app; a case
that no longer reproduces is a fix. For a fixed bug that has an open issue,
`verify` posts a "✅ Verified fixed" comment with a before/after screenshot
comparison and closes the issue.

Both modes write a human-readable `report.md` next to the JSON evidence and print
coloured status lines such as:

    [FOUND]  3 bugs
    [ISSUE]  #1 created  https://github.com/owner/repo/issues/1
    [PASS]   test_http_500_profile
    [ISSUE]  #1 commented and closed

When the LLM call fails the run keeps going: titles fall back to the raw error
text plus the action that produced it, and the report records `summarized_by:
raw`. A missing key is not an error either; that path uses heuristic titles.

Usage:
    python agent.py explore
    python agent.py explore --url http://localhost:8000 --login demo
    python agent.py verify
    python agent.py verify --dry-run          # report only, do not touch GitHub
    python agent.py explore --headed --max-actions 30 --no-llm

Configuration (.env next to this file, or real environment variables):
    LLM_API_KEY    API key for an OpenAI-compatible chat endpoint
                   (falls back to OPENAI_API_KEY)
    LLM_BASE_URL   default https://api.openai.com/v1
    LLM_MODEL      default gpt-4o-mini
    APP_URL        default target when --url is not passed
    GITHUB_TOKEN   token with issues:write on the target repository
    GITHUB_REPO    "owner/name" repository that receives the issues
    GITHUB_API_URL default https://api.github.com (change for GitHub Enterprise)
    GITHUB_SCREENSHOT_BASE
                   optional URL prefix that serves the evidence directory, used
                   to embed screenshots in issues and comments

What counts as a failure
    1. an uncaught JavaScript error, or a console error that is not just a
       failed network request (those are covered by the HTTP check)
    2. an HTTP 5xx, a 404, or a 405 anywhere; other 4xx only when navigating,
       because a form correctly rejecting bad input is not a bug
    3. broken behaviour: a button or link that is supposed to change something
       produces no request, no navigation and no DOM change

Exit codes: explore 0 = no bugs, 1 = bugs found, 2 = the run failed.
            verify  0 = every case passes, 1 = at least one still fails,
                    2 = the run failed.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests
from dotenv import load_dotenv
from github import Auth, Github, GithubException
from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    Response,
    sync_playwright,
)

ENV_FILE = Path(__file__).with_name(".env")

DEFAULT_URL = "http://localhost:8000"
DEFAULT_EVIDENCE_DIR = "evidence"
DEFAULT_TIMEOUT_MS = 5_000
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_ACTIONS = 60
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_GITHUB_API = "https://api.github.com"
SUITE_FILENAME = "suite.json"
BASELINE_DIRNAME = "baseline"
SUITE_VERSION = 1

TEXT_INPUT_CSS = (
    "input[type=text], input[type=search], input[type=email], input[type=url], "
    "input[type=tel], input[type=number], input[type=password], "
    "input:not([type]), textarea"
)
BUTTON_CSS = "button, input[type=submit], input[type=button]"

MUTATING_LABEL_RE = re.compile(
    r"\b(add|save|create|submit|update|send|apply|confirm|register|"
    r"login|log in|sign in|delete|remove|clear)\b",
    re.I,
)
LOGIN_RE = re.compile(r"(login|log-in|signin|sign-in|auth)", re.I)
# Our own screenshot names. Anything else in evidence/ is left alone.
SHOT_FILE_RE = re.compile(r"^\d{8}-\d{6}_\d{2}_[a-z0-9-]+\.png$")
# "Failed to load resource" mirrors an HTTP status we already record; counting it
# again would duplicate (or mislabel) every 4xx/5xx as a JavaScript error.
IGNORED_CONSOLE_RE = re.compile(r"failed to load resource|favicon", re.I)
IGNORED_REQUEST_RE = re.compile(r"/favicon\.ico$", re.I)

SEVERITIES = ("critical", "high", "medium", "low")
NO_EFFECT_DETAIL = "the action had no visible effect and sent no request"

# (description, payload) -- what a tester types into a text box to break it.
EDGE_CASES: tuple[tuple[str, str], ...] = (
    ("an empty string", ""),
    ("only spaces", "   "),
    ("a very long string", "L" * 512),
    ("special characters", "!@#$%^&*()_+-=[]{}|;:',.<>/?~`\""),
    ("a <script> payload", '<script>alert("xss")</script>'),
    ("a SQL injection payload", "' OR 1=1 --"),
    ("non-ASCII text", "日本語のタスク 🚀 café"),
)

SNAPSHOT_JS = """() => ({
  url: location.href,
  text: document.body ? document.body.innerText : '',
  items: document.querySelectorAll('li, tr, .task, .item, [data-item]').length,
})"""

# Builds a CSS path that survives a reload. Prefers ids, then name/data-id, and
# falls back to positional steps so any element can be addressed.
SELECTOR_JS = """(el) => {
  if (!el || el.nodeType !== 1) return '';
  if (el.id) return '#' + CSS.escape(el.id);
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && parts.length < 6) {
    let part = node.tagName.toLowerCase();
    const name = node.getAttribute('name');
    if (name) {
      parts.unshift(part + '[name="' + name.replace(/"/g, '\\\\"') + '"]');
      break;
    }
    const dataId = node.getAttribute('data-id');
    if (dataId) part += '[data-id="' + dataId.replace(/"/g, '\\\\"') + '"]';
    const parent = node.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
    }
    parts.unshift(part);
    if (parent === document.body || !parent) break;
    node = parent;
  }
  return parts.join(' > ');
}"""

ROLE_JS = """(el) => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'a') return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'input') {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (type === 'submit' || type === 'button') return 'button';
    return 'textbox';
  }
  return 'generic';
}"""

ANSI = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}

# Tag -> ANSI styles, so every status line looks the same everywhere.
STATUS_STYLES: dict[str, tuple[str, ...]] = {
    "SCAN": ("cyan", "bold"),
    "FOUND": ("red", "bold"),
    "ISSUE": ("magenta", "bold"),
    "LLM": ("blue", "bold"),
    "SUITE": ("cyan",),
    "REPORT": ("dim",),
    "VERIFY": ("cyan", "bold"),
    "PASS": ("green", "bold"),
    "FAIL": ("red", "bold"),
    "SKIP": ("yellow", "bold"),
    "WARN": ("yellow", "bold"),
    "DONE": ("green", "bold"),
}

TAG_WIDTH = 10
_USE_COLOR = False


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------


def init_color() -> bool:
    """Enable ANSI colors when writing to a real terminal (Windows included)."""
    if os.environ.get("NO_COLOR") is not None or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetStdHandle.restype = ctypes.c_void_p
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return False
    return True


def paint(text: str, *styles: str) -> str:
    if not _USE_COLOR or not styles:
        return text
    codes = ";".join(ANSI[s] for s in styles if s in ANSI)
    return f"\x1b[{codes}m{text}\x1b[0m" if codes else text


def configure_stdout() -> None:
    """Keep CJK or emoji payloads from crashing cp1252 pipes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def status(tag: str, message: str, tone: str | None = None) -> None:
    """One aligned, colored line: `[PASS]  test_name`."""
    label = f"[{tag}]".ljust(TAG_WIDTH)
    print(f"{paint(label, *STATUS_STYLES.get(tone or tag, ('cyan',)))}{message}", flush=True)


def stamp() -> str:
    return paint(f"[{datetime.now():%H:%M:%S}]", "dim")


def info(message: str) -> None:
    print(f"{stamp()} {message}", flush=True)


def warn(message: str) -> None:
    print(f"{stamp()} {paint('!', 'yellow', 'bold')} {message}", file=sys.stderr, flush=True)


def error(message: str) -> None:
    print(f"{paint('error:', 'red', 'bold')} {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass
class Signals:
    """Everything the browser reported during a single action."""

    http: list[tuple[int, str]] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    uncaught: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    navigations: list[tuple[str, int]] = field(default_factory=list)
    popups: list[str] = field(default_factory=list)

    def clear(self) -> None:
        self.http.clear()
        self.console.clear()
        self.uncaught.clear()
        self.dialogs.clear()
        self.navigations.clear()
        self.popups.clear()

    def evidence(self) -> list[str]:
        items: list[str] = []
        items += [f"HTTP {status} on {detail}" for status, detail in self.http]
        items += [f"uncaught error: {text}" for text in self.uncaught]
        items += [f"console error: {text}" for text in self.console]
        items += [f"dialog: {text}" for text in self.dialogs]
        items += [f"opened a new tab: {url}" for url in self.popups]
        return _unique(items)


@dataclass
class Bug:
    bug_title: str
    url: str
    action_that_failed: str
    error_message: str
    severity: str = "medium"
    repro_steps: list[str] = field(default_factory=list)
    screenshot: str = ""
    evidence: list[str] = field(default_factory=list)
    observed: str = ""
    payload: str | None = None
    variants: list[str] = field(default_factory=list)
    summarized_by: str = "heuristic"
    fingerprint: str = ""
    issue_url: str | None = None
    # Recorded so the same defect can be replayed without crawling.
    steps: list[dict[str, Any]] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Keys the bug report is promised, then the supporting detail."""
        return {
            "bug_title": self.bug_title,
            "url": self.url,
            "action_that_failed": self.action_that_failed,
            "error_message": self.error_message,
            "severity": self.severity,
            "repro_steps": self.repro_steps,
            "screenshot": self.screenshot,
            "evidence": self.evidence,
            "observed": self.observed,
            "payload": self.payload,
            "variants": self.variants,
            "summarized_by": self.summarized_by,
            "issue_url": self.issue_url,
            "steps": self.steps,
        }


@dataclass
class TestCase:
    """One named, replayable test: a bug that used to fail, plus how to retry it."""

    name: str
    bug_title: str
    steps: list[dict[str, Any]]
    expect: dict[str, Any]
    url: str = ""
    issue_url: str | None = None
    before_screenshot: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bug_title": self.bug_title,
            "url": self.url,
            "issue_url": self.issue_url,
            "before_screenshot": self.before_screenshot,
            "expect": self.expect,
            "steps": self.steps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestCase":
        return cls(
            name=str(data.get("name") or ""),
            bug_title=str(data.get("bug_title") or ""),
            steps=list(data.get("steps") or []),
            expect=dict(data.get("expect") or {}),
            url=str(data.get("url") or ""),
            issue_url=data.get("issue_url") or None,
            before_screenshot=str(data.get("before_screenshot") or ""),
        )


@dataclass
class VerifyResult:
    case: TestCase
    status: str  # fixed | still_failing | inconclusive
    detail: str = ""
    after_screenshot: str = ""
    issue_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "test": self.case.name,
            "bug_title": self.case.bug_title,
            "status": self.status,
            "detail": self.detail,
            "issue_url": self.case.issue_url,
            "issue_action": self.issue_action,
            "before_screenshot": self.case.before_screenshot,
            "after_screenshot": self.after_screenshot,
        }


# --------------------------------------------------------------------------
# browser session shared by explore and verify
# --------------------------------------------------------------------------


class BrowserSession:
    """Signals, snapshots and screenshots for one page."""

    def __init__(
        self,
        page: Page,
        start_url: str,
        *,
        evidence_dir: Path,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.page = page
        self.context = page.context
        self.start_url = start_url
        self.start_origin = _origin(start_url)
        self.evidence_dir = evidence_dir
        self.timeout = timeout_ms

        self.signals = Signals()
        self._shot_seq = 0

        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("response", self._on_response)
        page.on("dialog", self._on_dialog)
        page.on("popup", self._on_popup)

    # -- browser events ----------------------------------------------------

    def _on_console(self, message: Any) -> None:
        if message.type != "error":
            return
        text = str(message.text or "").strip()
        if not text or IGNORED_CONSOLE_RE.search(text):
            return
        self.signals.console.append(_clip(text, 400))

    def _on_pageerror(self, exc: Any) -> None:
        text = str(exc or "").strip()
        if text:
            self.signals.uncaught.append(_clip(text, 400))

    def _on_response(self, response: Response) -> None:
        try:
            request = response.request
        except PlaywrightError:
            return
        if request.resource_type == "document":
            self.signals.navigations.append((response.url, response.status))
        if IGNORED_REQUEST_RE.search(response.url):
            return
        if not _same_origin(response.url, self.start_origin):
            return
        if response.status < 400:
            return
        entry = (response.status, f"{request.method} {_path_of(response.url)}")
        if entry not in self.signals.http:
            self.signals.http.append(entry)

    def _on_dialog(self, dialog: Any) -> None:
        self.signals.dialogs.append(_clip(str(dialog.message or ""), 200))
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass

    def _on_popup(self, popup: Page) -> None:
        self.signals.popups.append(popup.url)
        try:
            popup.close()
        except PlaywrightError:
            pass

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _is_bug_status(status_code: int, action_kind: str) -> bool:
        if status_code >= 500:
            return True
        if status_code in (404, 405):
            return True
        # 400/401/403/409/422 during navigation means a broken link or missing
        # auth guard. The same codes from a form usually mean the app correctly
        # rejected the junk we typed, so they are not reported.
        return action_kind == "navigate" and 400 <= status_code < 500

    def _snapshot(self) -> dict[str, Any]:
        try:
            data = self.page.evaluate(SNAPSHOT_JS)
        except PlaywrightError:
            return {"url": self.page.url, "text": "", "items": -1}
        return {
            "url": str(data.get("url", self.page.url)),
            "text": _normalize_text(str(data.get("text", ""))),
            "items": int(data.get("items", 0)),
        }

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=1_500)
        except PlaywrightError:
            pass
        try:
            self.page.wait_for_timeout(250)
        except PlaywrightError:
            pass

    def _primary(
        self, action_kind: str, *, changed: bool, expect_effect: bool
    ) -> tuple[str, str, int | None] | None:
        """The first thing that went wrong, or None when the action was fine."""
        signals = self.signals
        failing = [s for s in signals.http if self._is_bug_status(s[0], action_kind)]
        if failing:
            status_code, detail = failing[0]
            return ("http", f"{detail} returned {status_code}", status_code)
        if signals.uncaught:
            return ("js", signals.uncaught[0], None)
        if signals.console:
            return ("js", signals.console[0], None)
        if signals.dialogs:
            return ("dialog", signals.dialogs[0], None)
        if expect_effect and not changed:
            return ("no_effect", NO_EFFECT_DETAIL, None)
        return None

    def _screenshot(self, *, kind: str, action: str, subdir: str = "") -> str:
        self._shot_seq += 1
        base = action if action.startswith(("click ", "submit ", "open ")) else f"{kind} {action}"
        slug = _slugify(base)[:48] or "action"
        name = f"{datetime.now():%Y%m%d-%H%M%S}_{self._shot_seq:02d}_{slug}.png"
        folder = self.evidence_dir / subdir if subdir else self.evidence_dir
        path = folder / name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
        except (PlaywrightError, OSError) as exc:
            warn(f"could not save a screenshot: {_first_line(str(exc))}")
            return ""
        return _display_path(path)


class Explorer(BrowserSession):
    """Crawl the app, act like a tester, and record what breaks."""

    def __init__(
        self,
        page: Page,
        start_url: str,
        *,
        evidence_dir: Path,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_actions: int = DEFAULT_MAX_ACTIONS,
    ) -> None:
        super().__init__(page, start_url, evidence_dir=evidence_dir, timeout_ms=timeout_ms)
        self.max_pages = max_pages
        self.max_actions = max_actions

        self.bugs: dict[str, Bug] = {}
        self.pages: list[str] = []
        self.actions = 0
        self.login_url: str | None = None
        self.notes: list[str] = []
        self._shots: list[Path] = []
        self._used_shots: set[Path] = set()

    # -- exploration -------------------------------------------------------

    def explore(self) -> list[Bug]:
        queue = [self.start_url]
        seen: set[str] = set()

        while queue and len(self.pages) < self.max_pages:
            url = queue.pop(0)
            key = _page_key(url)
            if key in seen:
                continue
            seen.add(key)

            if not self._open(url):
                continue
            self.pages.append(self.page.url)
            status("SCAN", f"exploring {paint(self.page.url, 'blue')}")
            page_url = self.page.url

            self._visit_links(page_url, queue)
            if self._limit_reached():
                break
            self._visit_buttons(page_url)
            if self._limit_reached():
                break
            self._visit_forms(page_url)

        self._prune_unused_screenshots()
        return list(self.bugs.values())

    def _open(self, url: str) -> bool:
        """Load a page, recording a 4xx/5xx status as a bug on its own."""
        self.signals.clear()
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
        except PlaywrightError as exc:
            warn(f"could not open {url}: {_first_line(str(exc))}")
            return False
        self._settle()

        status_code = response.status if response is not None else None
        if status_code is not None and status_code >= 400:
            self.actions += 1
            self.signals.http.append((status_code, f"GET {_path_of(url)}"))
            self._record(
                action_kind="navigate",
                action="open the page directly",
                primary=("http", f"GET {_path_of(url)} returned {status_code}", status_code),
                url=self.page.url,
                observed=_snapshot_text(self.page),
                steps=[{"do": "goto", "url": url}],
            )
        return True

    def _visit_links(self, page_url: str, queue: list[str]) -> None:
        links = self.page.locator("a[href]")
        try:
            total = links.count()
        except PlaywrightError:
            return

        tested: set[str] = set()
        for index in range(total):
            if self._limit_reached():
                return
            locator = links.nth(index)
            try:
                if not locator.is_visible():
                    continue
                href = (locator.get_attribute("href") or "").strip()
                label = _label_of(locator)
            except PlaywrightError:
                continue

            target = urljoin(page_url, href.split("#")[0])
            if not target or not self._is_crawlable(target):
                continue
            key = _page_key(target)
            if key in tested:
                continue
            tested.add(key)

            if LOGIN_RE.search(href) or LOGIN_RE.search(label):
                self.login_url = self.login_url or target

            control = self._control(locator, role="link")
            self._step(
                action_kind="navigate",
                action=f'click link "{_clip(label or href, 60)}"',
                perform=lambda loc=locator: loc.click(timeout=self.timeout),
                expect_effect=True,
                steps=[{"do": "goto", "url": page_url}, {"do": "click", **control}],
            )

            if not _same_page(self.page.url, page_url):
                if self._is_crawlable(self.page.url):
                    queue.append(self.page.url)
                self._restore(page_url)

    def _visit_buttons(self, page_url: str) -> None:
        buttons = self.page.locator(BUTTON_CSS)
        try:
            total = buttons.count()
        except PlaywrightError:
            return

        for index in range(total):
            if self._limit_reached():
                return
            locator = buttons.nth(index)
            try:
                if not locator.is_visible():
                    continue
                label = _label_of(locator) or "button"
            except PlaywrightError:
                continue

            # Only actions that ought to change something can be "broken" by
            # doing nothing; a plain button may legitimately be a no-op.
            expect_effect = bool(MUTATING_LABEL_RE.search(label))
            control = self._control(locator, role="button")
            self._step(
                action_kind="click",
                action=f'click button "{_clip(label, 60)}"',
                perform=lambda loc=locator: loc.click(timeout=self.timeout),
                expect_effect=expect_effect,
                steps=[{"do": "goto", "url": page_url}, {"do": "click", **control}],
            )
            if not _same_page(self.page.url, page_url):
                self._restore(page_url)

    def _visit_forms(self, page_url: str) -> None:
        forms = self.page.locator("form")
        try:
            total = forms.count()
        except PlaywrightError:
            return

        for index in range(total):
            if self._limit_reached():
                return
            form = forms.nth(index)
            try:
                if not form.is_visible():
                    continue
                if form.locator(TEXT_INPUT_CSS).count() == 0:
                    continue
            except PlaywrightError:
                continue

            # Credential forms are not fuzzed: seven wrong passwords tell us
            # nothing, and a 401 is the server behaving correctly.
            login_form = self._looks_like_login(form)
            cases = EDGE_CASES[:1] if login_form else EDGE_CASES

            field = form.locator(TEXT_INPUT_CSS).first
            submit = form.locator("button[type=submit], input[type=submit], button").first
            field_step = self._control(field, role="textbox")
            button_step = self._control(submit, role="button") if submit.count() > 0 else None

            for description, payload in cases:
                if self._limit_reached():
                    return
                self._step(
                    action_kind="form",
                    action=f"submit the form with {description}",
                    perform=lambda f=form, p=payload: self._fill_and_submit(f, p),
                    expect_effect=True,
                    payload=payload,
                    steps=[
                        {"do": "goto", "url": page_url},
                        {
                            "do": "submit_form",
                            "field": field_step,
                            "button": button_step,
                            "payload": payload,
                        },
                    ],
                )
                if not _same_page(self.page.url, page_url):
                    self._restore(page_url)

    def _fill_and_submit(self, form: Locator, payload: str) -> None:
        field = form.locator(TEXT_INPUT_CSS).first
        field.fill(payload, timeout=self.timeout)

        submit = form.locator(
            "button[type=submit], input[type=submit], button"
        ).first
        if submit.count() > 0:
            submit.click(timeout=self.timeout)
        else:
            field.press("Enter", timeout=self.timeout)

    def _looks_like_login(self, form: Locator) -> bool:
        try:
            if form.locator("input[type=password]").count() > 0:
                return True
            action = form.get_attribute("action") or ""
            if LOGIN_RE.search(action):
                return True
            submit = form.locator("button[type=submit], button, input[type=submit]").first
            return bool(LOGIN_RE.search(_label_of(submit)))
        except PlaywrightError:
            return False

    # -- one action, judged ------------------------------------------------

    def _step(
        self,
        *,
        action_kind: str,
        action: str,
        perform: Callable[[], None],
        expect_effect: bool,
        steps: list[dict[str, Any]],
        payload: str | None = None,
    ) -> None:
        if self._limit_reached():
            return
        self.actions += 1
        self.signals.clear()

        before = self._snapshot()
        action_error: str | None = None
        try:
            perform()
        except PlaywrightError as exc:
            # A harness limitation (detached node, timeout) is not proof of a
            # product bug, so it is logged rather than reported.
            action_error = _first_line(str(exc))
        self._settle()
        after = self._snapshot()

        if action_error:
            warn(f"{action} could not be performed: {action_error}")
            return

        primary = self._primary(
            action_kind,
            changed=_changed(before, after, self.signals),
            expect_effect=expect_effect,
        )
        if primary is None:
            return

        # For a navigation failure, report the destination: that is the page a
        # human would open to reproduce it.
        url = before["url"]
        for nav_url, nav_status in self.signals.navigations:
            if self._is_bug_status(nav_status, action_kind) and _same_origin(
                nav_url, self.start_origin
            ):
                url = nav_url
                break

        extra = (
            ["no DOM change, no navigation and no network request was made"]
            if primary[0] == "no_effect"
            else None
        )
        self._record(
            action_kind=action_kind,
            action=action,
            primary=primary,
            url=url,
            observed=_clip(after["text"], 400),
            payload=payload,
            steps=steps,
            extra_evidence=extra,
        )

    def _record(
        self,
        *,
        action_kind: str,
        action: str,
        primary: tuple[str, str, int | None],
        url: str,
        observed: str,
        steps: list[dict[str, Any]],
        payload: str | None = None,
        extra_evidence: list[str] | None = None,
    ) -> None:
        primary_kind, detail, status_code = primary
        fingerprint = f"{primary_kind}:{_path_of(url)}:{detail if primary_kind != 'no_effect' else action}"

        existing = self.bugs.get(fingerprint)
        if existing is not None:
            self._merge(existing, action, payload)
            return

        screenshot = self._screenshot(kind=action_kind, action=action)
        if screenshot:
            self._used_shots.add(Path(screenshot))
        evidence = _unique([*self.signals.evidence(), *(extra_evidence or [])])
        bug = Bug(
            bug_title=_heuristic_title(primary_kind, detail, action, status_code),
            url=url,
            action_that_failed=action,
            error_message=_clip(detail, 300),
            severity=_severity(primary_kind, status_code),
            repro_steps=_heuristic_steps(url, action, detail),
            screenshot=screenshot,
            evidence=evidence,
            observed=observed,
            payload=payload,
            fingerprint=fingerprint,
            steps=steps,
            expect={
                "kind": primary_kind,
                "action_kind": action_kind,
                "detail": detail,
                "status": status_code,
            },
        )
        self.bugs[fingerprint] = bug
        status(
            "FOUND",
            f"{paint(bug.bug_title, 'bold')}  {paint('(' + bug.severity + ')', 'dim')}",
        )

    def _merge(self, bug: Bug, action: str, payload: str | None) -> None:
        if action != bug.action_that_failed and action not in bug.variants:
            bug.variants.append(action)
        for item in self.signals.evidence():
            if item not in bug.evidence:
                bug.evidence.append(item)
        if payload is not None and bug.payload is None:
            bug.payload = payload

    # -- helpers -----------------------------------------------------------

    def _control(self, locator: Locator, *, role: str | None = None) -> dict[str, Any]:
        """Record how to find this element again after a reload."""
        selector = ""
        try:
            selector = str(locator.evaluate(SELECTOR_JS) or "")
        except PlaywrightError:
            pass
        if role is None:
            try:
                role = str(locator.evaluate(ROLE_JS) or "generic")
            except PlaywrightError:
                role = "generic"
        return {"role": role, "name": _label_of(locator), "selector": selector}

    def _restore(self, url: str) -> None:
        if _same_page(self.page.url, url):
            return
        try:
            self.page.go_back(wait_until="domcontentloaded", timeout=self.timeout)
        except PlaywrightError:
            pass
        if not _same_page(self.page.url, url):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            except PlaywrightError:
                return
        self._settle()

    def _is_crawlable(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if not _same_origin(url, self.start_origin):
            return False
        return not IGNORED_REQUEST_RE.search(parsed.path)

    def _limit_reached(self) -> bool:
        if self.actions >= self.max_actions:
            if not self.notes or not self.notes[-1].startswith("stopped after"):
                self.notes.append(f"stopped after the {self.max_actions} action budget was spent")
                warn(self.notes[-1])
            return True
        return False

    def _screenshot(self, *, kind: str, action: str, subdir: str = "") -> str:
        path = super()._screenshot(kind=kind, action=action, subdir=subdir)
        if path:
            self._shots.append(Path(path))
        return path

    def _prune_unused_screenshots(self) -> None:
        for path in self._shots:
            if path in self._used_shots:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    # -- post-crawl checks -------------------------------------------------

    def sign_in(self, credentials: str) -> bool:
        """Best-effort login, used to see whether a failure is auth-specific."""
        username, _, password = credentials.partition(":")
        if not self.login_url:
            warn("no login link found, skipping the authenticated re-check")
            return False

        self.signals.clear()
        try:
            self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=self.timeout)
            self._settle()
            form = self.page.locator("form").first
            form.locator(TEXT_INPUT_CSS).first.fill(username, timeout=self.timeout)
            if password and form.locator("input[type=password]").count() > 0:
                form.locator("input[type=password]").first.fill(password, timeout=self.timeout)
            submit = form.locator("button[type=submit], input[type=submit], button").first
            submit.click(timeout=self.timeout)
            self._settle()
        except PlaywrightError as exc:
            warn(f"sign-in attempt failed: {_first_line(str(exc))}")
            return False

        statuses = [status_code for status_code, _ in self.signals.http]
        if any(status_code >= 500 for status_code in statuses):
            warn("sign-in returned a server error")
            return False
        return not _same_page(self.page.url, self.login_url)

    def explain_with_session(self, bugs: Iterable[Bug], credentials: str) -> None:
        """Note which failures disappear once signed in (e.g. missing guards)."""
        if not self.sign_in(credentials):
            return
        seen: set[str] = set()
        for bug in bugs:
            # Only page loads can become healthy after signing in; a failing
            # POST does not care who is logged in.
            if not any(item.startswith("HTTP 5") and " on GET " in item for item in bug.evidence):
                continue
            if bug.url in seen:
                continue
            seen.add(bug.url)
            try:
                response = self.page.goto(bug.url, wait_until="domcontentloaded", timeout=self.timeout)
                self._settle()
            except PlaywrightError:
                continue
            if response is not None and response.status < 400:
                note = (
                    f"returns HTTP {response.status} after signing in as "
                    f'"{credentials.split(":")[0]}", so the failure affects anonymous visitors'
                )
                if note not in bug.evidence:
                    bug.evidence.append(note)


class Verifier(BrowserSession):
    """Replay saved test cases and report which ones now pass."""

    def run(self, cases: list[TestCase]) -> list[VerifyResult]:
        results: list[VerifyResult] = []
        for index, case in enumerate(cases, start=1):
            status("VERIFY", f"{case.name}  ({index}/{len(cases)})")
            results.append(self._replay(case))
        return results

    def _replay(self, case: TestCase) -> VerifyResult:
        self.signals.clear()
        url_base = self.start_url
        before: dict[str, Any] | None = None
        # The replay's own page load must not count as an effect of the action,
        # but its HTTP status does (a 500 page load is the bug in some cases).
        nav_baseline = 0

        for step in case.steps:
            action = str(step.get("do") or "")
            try:
                if action == "goto":
                    # Replay against the app under test, not necessarily the host
                    # the suite was recorded on (staging vs local, new port...).
                    url = _rebase(str(step.get("url") or case.url), url_base)
                    self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                    self._settle()
                    before = self._snapshot()
                    nav_baseline = len(self.signals.navigations)
                elif action == "click":
                    locator = self._locate(step)
                    if locator is None:
                        return self._inconclusive(case, f'control not found: {_control_label(step)}')
                    before = self._snapshot()
                    locator.click(timeout=self.timeout)
                    self._settle()
                elif action == "submit_form":
                    field = self._locate(step.get("field") or {})
                    if field is None:
                        return self._inconclusive(case, "form field not found")
                    before = self._snapshot()
                    field.fill(str(step.get("payload") or ""), timeout=self.timeout)
                    button = step.get("button")
                    target = self._locate(button) if button else None
                    if target is not None:
                        target.click(timeout=self.timeout)
                    else:
                        field.press("Enter", timeout=self.timeout)
                    self._settle()
                else:
                    return self._inconclusive(case, f"unknown step: {action or '(empty)'}")
            except PlaywrightError as exc:
                return self._inconclusive(case, _first_line(str(exc)))

        after = self._snapshot()
        if before is None:
            return self._inconclusive(case, "no steps recorded for this test")

        changed = (
            after["url"] != before["url"]
            or after["items"] != before["items"]
            or after["text"] != before["text"]
            or bool(self.signals.navigations[nav_baseline:])
            or bool(self.signals.popups)
        )
        failure = self._failure_detail(case.expect, changed=changed)

        if failure is None:
            screenshot = self._screenshot(kind="verify", action=f"fixed {case.name}", subdir="after")
            return VerifyResult(
                case=case,
                status="fixed",
                detail=f'no longer reproduces: {case.expect.get("detail") or "previous failure"}',
                after_screenshot=screenshot,
            )

        return VerifyResult(
            case=case,
            status="still_failing",
            detail=f"still fails: {failure}",
        )

    def _failure_detail(self, expect: dict[str, Any], *, changed: bool) -> str | None:
        """Is the *recorded* failure class still present? None means it is gone.

        Each case asserts one specific defect. A different symptom on the same
        action (say a 400 where a 500 used to be) is not the same bug.
        """
        kind = str(expect.get("kind") or "")
        if kind == "http":
            action_kind = str(expect.get("action_kind") or "navigate")
            failing = [s for s in self.signals.http if self._is_bug_status(s[0], action_kind)]
            if failing:
                status_code, detail = failing[0]
                return f"{detail} returned {status_code}"
            return None
        if kind == "js":
            errors = self.signals.uncaught or self.signals.console
            return errors[0] if errors else None
        if kind == "dialog":
            return self.signals.dialogs[0] if self.signals.dialogs else None
        if kind == "no_effect":
            return None if changed else NO_EFFECT_DETAIL
        return None

    def _inconclusive(self, case: TestCase, reason: str) -> VerifyResult:
        return VerifyResult(case=case, status="inconclusive", detail=reason)

    def _locate(self, step: dict[str, Any]) -> Locator | None:
        """Find a recorded control by role/name first, then by CSS path."""
        if not step:
            return None
        role = str(step.get("role") or "")
        name = str(step.get("name") or "").strip()
        if role and name:
            try:
                candidate = self.page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
                if candidate.count() > 0:
                    return candidate.first
            except PlaywrightError:
                pass
        selector = str(step.get("selector") or "")
        if selector:
            try:
                candidate = self.page.locator(selector)
                if candidate.count() > 0:
                    return candidate.first
            except PlaywrightError:
                pass
        return None


# --------------------------------------------------------------------------
# heuristics (used as the fallback, and as the base the LLM improves on)
# --------------------------------------------------------------------------


def _heuristic_title(kind: str, detail: str, action: str, status_code: int | None) -> str:
    if kind == "http" and status_code is not None:
        return f"HTTP {status_code} when you {action}"
    if kind == "js":
        return f"JavaScript error when you {action}"
    if kind == "dialog":
        return f"Unexpected dialog when you {action}"
    label = re.sub(r"^(click button|click link)\s+", "", action).strip().strip('"')
    return f'"{label or action}" does nothing'


def _heuristic_steps(url: str, action: str, detail: str) -> list[str]:
    return [f"Open {url}", _capitalize(action), f"Observe: {_clip(detail, 160)}"]


def _capitalize(text: str) -> str:
    """Uppercase the first character, leaving the rest (and quoting) alone."""
    return text[:1].upper() + text[1:]


def _severity(kind: str, status_code: int | None) -> str:
    if kind == "http":
        if status_code is not None and status_code >= 500:
            return "high"
        return "medium"
    if kind == "js":
        return "high"
    if kind == "dialog":
        return "high"
    return "medium"


def _apply_raw_fallback(bugs: list[Bug]) -> None:
    """No LLM summary: keep the raw error text so the title stays specific."""
    for bug in bugs:
        if bug.summarized_by == "llm":
            continue
        if bug.expect.get("kind") == "no_effect":
            label = re.sub(r"^(click button|click link)\s+", "", bug.action_that_failed).strip().strip('"')
            bug.bug_title = f'"{_clip(label, 80)}" does nothing'
        else:
            raw = bug.error_message or bug.action_that_failed
            bug.bug_title = _clip(f"{raw} - {bug.action_that_failed}", 110)
        bug.summarized_by = "raw"


# --------------------------------------------------------------------------
# test suite
# --------------------------------------------------------------------------


def build_suite(bugs: list[Bug], *, app_url: str, evidence_dir: Path) -> list[TestCase]:
    """Turn each bug into a named, replayable case with a baseline screenshot."""
    baseline_dir = evidence_dir / BASELINE_DIRNAME
    cases: list[TestCase] = []
    used: set[str] = set()

    for bug in bugs:
        name = _test_name(bug, used)
        used.add(name)

        before = ""
        if bug.screenshot:
            source = Path(bug.screenshot)
            if source.is_file():
                baseline_dir.mkdir(parents=True, exist_ok=True)
                target = baseline_dir / f"{name}.png"
                try:
                    shutil.copyfile(source, target)
                    before = _display_path(target)
                except OSError as exc:
                    warn(f"could not save the baseline screenshot for {name}: {exc}")

        cases.append(
            TestCase(
                name=name,
                bug_title=bug.bug_title,
                steps=bug.steps,
                expect=bug.expect,
                url=bug.url,
                issue_url=bug.issue_url,
                before_screenshot=before,
            )
        )
    return cases


def _test_name(bug: Bug, used: set[str]) -> str:
    kind = str(bug.expect.get("kind") or "bug")
    status_code = bug.expect.get("status")
    prefix = f"{kind} {status_code}" if kind == "http" and status_code else kind
    action = re.sub(
        r"^(click button|click link|submit the form with|open the page directly)\s*",
        "",
        bug.action_that_failed,
    )
    slug = _slugify(f"{prefix} {action}")[:60].strip("-") or "bug"
    name = f"test_{slug.replace('-', '_')}"
    if name not in used:
        return name
    counter = 2
    while f"{name}_{counter}" in used:
        counter += 1
    return f"{name}_{counter}"


def write_suite(path: Path, cases: list[TestCase], *, app_url: str) -> Path:
    data = {
        "version": SUITE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app_url": app_url,
        "cases": [case.as_dict() for case in cases],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_suite(path: Path) -> tuple[list[TestCase], str]:
    """Return the cases and the app URL the suite was recorded against."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the suite file is not a JSON object")
    version = data.get("version")
    if version != SUITE_VERSION:
        raise ValueError(f"unsupported suite version {version!r} (expected {SUITE_VERSION})")
    raw = data.get("cases")
    if not isinstance(raw, list):
        raise ValueError("the suite file has no cases list")
    cases = [TestCase.from_dict(item) for item in raw if isinstance(item, dict)]
    return cases, str(data.get("app_url") or "")


# --------------------------------------------------------------------------
# LLM summarisation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str


LLM_INSTRUCTIONS = """You are a senior QA engineer turning raw automated findings into a bug report.

For every finding, in the same order, return:
  - id: the finding id you were given
  - bug_title: at most 80 characters, specific, plain language, no "Bug:" prefix.
    Describe the visible symptom, not the implementation.
  - repro_steps: 3 to 5 short steps a person can follow in a browser.
  - error_message: the key technical evidence in one line, at most 200 characters.
  - severity: one of critical, high, medium, low. Server errors and uncaught
    JavaScript errors are at least high; a silent no-op is medium.
  - expected: one sentence describing what should have happened.
  - actual: one sentence describing what happened.

Rules:
  - Only use the evidence provided. Never invent behaviour, endpoints or causes.
  - Do not suggest code fixes.
  - Return ONLY a JSON array. No prose, no markdown fences.

Each finding has: id, url, action, kind, evidence, observed_text, payload, variants.
Here are the findings:
"""


def load_llm_config() -> LLMConfig | None:
    api_key = (
        os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not api_key:
        return None
    base_url = (
        os.environ.get("LLM_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    )
    model = (
        os.environ.get("LLM_MODEL", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or DEFAULT_LLM_MODEL
    )
    return LLMConfig(api_key=api_key, base_url=base_url.rstrip("/"), model=model)


def summarize_with_llm(bugs: list[Bug], config: LLMConfig) -> tuple[bool, str]:
    """Rewrite titles, steps and severities.

    Returns (ok, reason). On failure the caller falls back to raw error text;
    this function never raises for network, HTTP or malformed-reply problems.
    """
    findings = [
        {
            "id": index,
            "url": bug.url,
            "action": bug.action_that_failed,
            "kind": str(bug.expect.get("kind") or _kind_of(bug)),
            "evidence": bug.evidence[:8],
            "observed_text": bug.observed,
            "payload": _clip(bug.payload or "", 120),
            "variants": bug.variants[:5],
        }
        for index, bug in enumerate(bugs, start=1)
    ]
    prompt = LLM_INSTRUCTIONS + json.dumps(findings, ensure_ascii=False, indent=2)

    try:
        content = _chat(config, prompt)
        updates = _parse_json_array(content)
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        return False, _first_line(str(exc)) or type(exc).__name__

    by_id: dict[int, dict[str, Any]] = {}
    for update in updates:
        if isinstance(update, dict):
            try:
                by_id[int(update.get("id"))] = update
            except (TypeError, ValueError):
                continue

    applied = 0
    for index, bug in enumerate(bugs, start=1):
        update = by_id.get(index)
        if not update:
            continue
        if _apply_update(bug, update):
            applied += 1

    if applied == 0:
        return False, "the reply had no usable entries"
    if applied < len(bugs):
        warn(f"the LLM summarised {applied} of {len(bugs)} findings")
    return True, ""


def _apply_update(bug: Bug, update: dict[str, Any]) -> bool:
    title = str(update.get("bug_title") or "").strip()
    if not title:
        return False
    bug.bug_title = _clip(title, 120)

    steps = update.get("repro_steps")
    if isinstance(steps, list):
        clean = [_clip(str(step).strip(), 200) for step in steps if str(step).strip()]
        if clean:
            bug.repro_steps = clean[:6]

    message = str(update.get("error_message") or "").strip()
    if message:
        bug.error_message = _clip(message, 300)

    severity = str(update.get("severity") or "").strip().lower()
    if severity in SEVERITIES:
        bug.severity = severity

    expected = str(update.get("expected") or "").strip()
    actual = str(update.get("actual") or "").strip()
    if expected or actual:
        text = f"expected: {expected}" if expected else ""
        if actual:
            text = f"{text}; actual: {actual}" if text else f"actual: {actual}"
        note = _clip(text, 400)
        if note not in bug.evidence:
            bug.evidence.append(note)

    bug.summarized_by = "llm"
    return True


def _chat(config: LLMConfig, prompt: str) -> str:
    payload = {
        "model": config.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a precise QA engineer. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{config.base_url}/chat/completions",
        json=payload,
        headers=headers,
        timeout=90,
    )
    if response.status_code >= 400:
        raise ValueError(
            f"HTTP {response.status_code} from {config.base_url}: {_clip(response.text, 160)}"
        )
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("the LLM returned no text content")
    return content


def _parse_json_array(text: str) -> list[Any]:
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end < start:
        raise ValueError("no JSON array in the LLM reply")
    parsed = json.loads(body[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("the LLM reply was not a JSON array")
    return parsed


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubConfig:
    token: str
    repo: str
    api_url: str
    screenshot_base: str | None


@dataclass
class IssueOutcome:
    repo: str = ""
    created: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    def as_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        rows += [{"bug_title": t, "status": "created", "url": u} for t, u in self.created]
        rows += [{"bug_title": t, "status": "duplicate", "url": u} for t, u in self.skipped]
        rows += [{"bug_title": t, "status": "failed", "detail": d} for t, d in self.failed]
        return rows


def load_github_config(repo_override: str | None, screenshot_base: str | None) -> GitHubConfig | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = (repo_override or os.environ.get("GITHUB_REPO", "")).strip()
    if not token or not repo:
        return None
    base = (screenshot_base or os.environ.get("GITHUB_SCREENSHOT_BASE", "").strip() or None)
    api_url = os.environ.get("GITHUB_API_URL", "").strip() or DEFAULT_GITHUB_API
    return GitHubConfig(
        token=token,
        repo=repo,
        api_url=api_url.rstrip("/"),
        screenshot_base=base.rstrip("/") if base else None,
    )


def _open_repository(config: GitHubConfig) -> tuple[Github, Any]:
    github = Github(auth=Auth.Token(config.token), base_url=config.api_url)
    try:
        repository = github.get_repo(config.repo)
    except (GithubException, requests.RequestException):
        github.close()
        raise
    return github, repository


def file_issues(
    bugs: list[Bug],
    config: GitHubConfig,
    *,
    app_url: str,
    browser_version: str,
    headless: bool,
    evidence_dir: Path,
) -> IssueOutcome:
    """Open one issue per bug, skipping titles that are already open."""
    outcome = IssueOutcome(repo=config.repo)
    try:
        github, repository = _open_repository(config)
    except (GithubException, requests.RequestException) as exc:
        outcome.error = _describe_github_error(exc)
        warn(f"cannot reach {config.repo}: {outcome.error}")
        return outcome

    try:
        screenshots = _ScreenshotUrls(repository, config.screenshot_base, evidence_dir)
        filed: set[str] = set()

        for bug in bugs:
            title_key = _normalize_title(bug.bug_title)
            if title_key in filed:
                outcome.skipped.append((bug.bug_title, bug.issue_url or "filed earlier in this run"))
                continue

            existing = _find_open_issue(github, config.repo, bug.bug_title)
            if existing is not None:
                bug.issue_url = existing
                outcome.skipped.append((bug.bug_title, existing))
                filed.add(title_key)
                continue

            screenshot_url = screenshots.resolve(bug.screenshot)
            body = build_issue_body(
                bug,
                app_url=app_url,
                browser_version=browser_version,
                headless=headless,
                screenshot_url=screenshot_url,
            )
            try:
                issue = repository.create_issue(title=bug.bug_title, body=body)
            except (GithubException, requests.RequestException) as exc:
                detail = _describe_github_error(exc)
                outcome.failed.append((bug.bug_title, detail))
                warn(f'could not file "{bug.bug_title}": {detail}')
                continue

            bug.issue_url = issue.html_url
            outcome.created.append((bug.bug_title, issue.html_url))
            filed.add(title_key)
    finally:
        github.close()

    return outcome


def _find_open_issue(github: Github, repo_name: str, title: str) -> str | None:
    """Return the URL of an open issue whose title matches, if there is one."""
    needle = _normalize_title(title)
    # Search is token based, so punctuation in the title would break the quoted
    # phrase. Search on the normalised words and confirm with a second compare.
    phrase = " ".join(needle.split()[:12])
    query = f'repo:{repo_name} is:issue is:open in:title "{phrase}"'
    try:
        # `is:issue` keeps pull requests out, so no extra per-result request is
        # needed; each attribute read here is already in the search payload.
        for issue in github.search_issues(query=query):
            if issue.state == "open" and _normalize_title(issue.title) == needle:
                return issue.html_url
    except (GithubException, requests.RequestException) as exc:
        # Search can fail on rate limits; creating the issue is still better
        # than dropping the finding, so warn and carry on.
        warn(f'duplicate check failed for "{title}": {_describe_github_error(exc)}')
    return None


def publish_verifications(
    results: list[VerifyResult],
    config: GitHubConfig,
    *,
    app_url: str,
    browser_version: str,
    headless: bool,
    evidence_dir: Path,
    dry_run: bool,
) -> None:
    """Comment on and close the issues whose bug no longer reproduces."""
    targets = [r for r in results if r.status == "fixed" and r.case.issue_url]
    for result in results:
        if result.status == "fixed" and not result.case.issue_url:
            result.issue_action = "no issue linked"
    if not targets:
        return

    try:
        github, repository = _open_repository(config)
    except (GithubException, requests.RequestException) as exc:
        detail = _describe_github_error(exc)
        warn(f"cannot reach {config.repo}: {detail}")
        for result in targets:
            result.issue_action = f"failed: {detail}"
        return

    try:
        screenshots = _ScreenshotUrls(repository, config.screenshot_base, evidence_dir)
        for result in targets:
            number = _issue_number(result.case.issue_url or "")
            if number is None:
                result.issue_action = "skipped: cannot read the issue number"
                continue
            if dry_run:
                result.issue_action = "would comment and close (dry run)"
                continue
            try:
                issue = repository.get_issue(number)
                if issue.state != "open":
                    result.issue_action = f"skipped: issue is already {issue.state}"
                    continue
                body = build_verify_comment(
                    result,
                    app_url=app_url,
                    browser_version=browser_version,
                    headless=headless,
                    before_url=screenshots.resolve(result.case.before_screenshot),
                    after_url=screenshots.resolve(result.after_screenshot),
                )
                issue.create_comment(body)
                issue.edit(state="closed")
                result.issue_action = "commented and closed"
            except (GithubException, requests.RequestException) as exc:
                result.issue_action = f"failed: {_describe_github_error(exc)}"
    finally:
        github.close()


def build_verify_comment(
    result: VerifyResult,
    *,
    app_url: str,
    browser_version: str,
    headless: bool,
    before_url: str | None,
    after_url: str | None,
) -> str:
    before_cell = _image_cell(before_url, result.case.before_screenshot, "before")
    after_cell = _image_cell(after_url, result.after_screenshot, "after")
    mode = "headless" if headless else "headed"
    detected = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    lines = [
        "### ✅ Verified fixed",
        "",
        f"`agent.py verify` re-ran **{result.case.name}** and the failure no longer reproduces.",
        "",
        f"- Previous failure: `{_md_inline(str(result.case.expect.get('detail') or 'unknown'))}`",
        f"- Result: {_md_inline(result.detail)}",
        "",
        "### 📸 Before / After",
        "| Before (bug present) | After (fixed) |",
        "| --- | --- |",
        f"| {before_cell} | {after_cell} |",
        "",
        "### Environment",
        "| Field | Value |",
        "| --- | --- |",
        f"| App URL | {_md_cell(app_url)} |",
        f"| Browser | {_md_cell(browser_version)} ({mode}) |",
        f"| OS | {_md_cell(platform.platform())} |",
        f"| Python | {platform.python_version()} |",
        f"| Verified | {detected} |",
        "| Tool | `agent.py verify` |",
        "",
        "_Closing automatically because the recorded test now passes. Reopen if the"
        " regression comes back._",
    ]
    return "\n".join(lines)


def build_issue_body(
    bug: Bug,
    *,
    app_url: str,
    browser_version: str,
    headless: bool,
    screenshot_url: str | None,
) -> str:
    steps = bug.repro_steps or _heuristic_steps(bug.url, bug.action_that_failed, bug.error_message)
    repro = "\n".join(
        f"{index}. {_strip_step_number(step)}" for index, step in enumerate(steps, 1)
    )
    error_text = (bug.error_message or "no message captured").replace("```", "'''")
    mode = "headless" if headless else "headed"
    detected = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    lines = [
        "### Repro steps",
        repro,
        "",
        "### Error message",
        "```text",
        error_text,
        "```",
        "",
        "### Environment",
        "| Field | Value |",
        "| --- | --- |",
        f"| App URL | {_md_cell(app_url)} |",
        f"| Browser | {_md_cell(browser_version)} ({mode}) |",
        f"| OS | {_md_cell(platform.platform())} |",
        f"| Python | {platform.python_version()} |",
        f"| Detected | {detected} |",
        f"| Summarised by | {_md_cell(bug.summarized_by)} |",
        "",
    ]

    lines.append("### Screenshot")
    if screenshot_url:
        lines += [f"![screenshot]({screenshot_url})", ""]
    elif bug.screenshot:
        lines += [
            f"`{bug.screenshot}` is saved with the run, but it is not embedded here:",
            "the file is not published at a URL GitHub can render. Commit `evidence/`",
            "or set `GITHUB_SCREENSHOT_BASE` to the folder's public URL.",
            "",
        ]
    else:
        lines += ["No screenshot was captured for this finding.", ""]

    lines.append(f"_Filed automatically by `agent.py` after: {bug.action_that_failed}._")
    return "\n".join(lines)


class _ScreenshotUrls:
    """Resolve a local evidence path to something GitHub can render."""

    def __init__(self, repository: Any, base: str | None, evidence_dir: Path) -> None:
        self.repository = repository
        self.base = base
        self.evidence_dir = evidence_dir
        self._cache: dict[str, str | None] = {}

    def resolve(self, path: str) -> str | None:
        if not path:
            return None
        if path in self._cache:
            return self._cache[path]

        url: str | None = None
        if self.base:
            relative = self._relative(path)
            url = f"{self.base}/{quote(relative)}" if relative else None
        elif not Path(path).is_absolute():
            url = self._repo_url(path)

        self._cache[path] = url
        return url

    def _relative(self, path: str) -> str:
        try:
            resolved = Path(path).resolve()
            return resolved.relative_to(self.evidence_dir.resolve()).as_posix()
        except (ValueError, OSError):
            return Path(path).name

    def _repo_url(self, path: str) -> str | None:
        relative = path[2:] if path.startswith("./") else path
        try:
            content = self.repository.get_contents(relative)
        except GithubException as exc:
            if exc.status != 404:
                warn(f"cannot look up {relative} in the repository: {_describe_github_error(exc)}")
            return None
        except requests.RequestException as exc:
            warn(f"cannot look up {relative} in the repository: {_first_line(str(exc))}")
            return None
        if content is None:
            return None

        full_name = self.repository.full_name
        branch = self.repository.default_branch or "main"
        return f"https://raw.githubusercontent.com/{full_name}/{branch}/{quote(relative)}"


def print_issues(outcome: IssueOutcome) -> None:
    if outcome.error:
        status("ISSUE", f"skipped: {outcome.error}", "WARN")
        return
    if not (outcome.created or outcome.skipped or outcome.failed):
        return

    for title, url in outcome.created:
        number = _issue_number(url)
        prefix = f"#{number} created" if number else "created"
        status("ISSUE", f"{prefix}  {paint(url, 'blue')}")
    for title, url in outcome.skipped:
        number = _issue_number(url)
        prefix = f"#{number} already open" if number else "already open"
        status("ISSUE", f"{prefix}  {paint(url, 'dim')}", "WARN")
    for title, detail in outcome.failed:
        status("ISSUE", f'failed: {title} - {detail}', "WARN")

    tone = "PASS" if not outcome.failed else "WARN"
    status(
        "ISSUE",
        f"{len(outcome.created)} created, {len(outcome.skipped)} skipped, "
        f"{len(outcome.failed)} failed",
        tone,
    )


def _describe_github_error(exc: Exception) -> str:
    if isinstance(exc, GithubException):
        data = exc.data if isinstance(exc.data, dict) else {}
        message = str(data.get("message") or exc)
        return f"{message} (HTTP {exc.status})" if exc.status else message
    return _first_line(str(exc))


def _issue_number(url: str) -> int | None:
    match = re.search(r"/issues/(\d+)(?:$|[?#])", url)
    return int(match.group(1)) if match else None


def _normalize_title(title: str) -> str:
    """Case- and punctuation-insensitive form used to compare issue titles."""
    text = re.sub(r"[^a-z0-9\s]+", " ", str(title).casefold())
    return " ".join(text.split())


def _strip_step_number(step: str) -> str:
    return re.sub(r"^\s*\d+[.)]\s*", "", str(step)).strip()


def _md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _md_inline(text: str) -> str:
    return str(text).replace("`", "'").replace("\n", " ").strip()


def _image_cell(url: str | None, path: str, label: str) -> str:
    if url:
        return f"![{label}]({url})"
    if path:
        return f"`{path}` (local file, not published)"
    return "not captured"


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------


def print_bug_table(bugs: list[Bug]) -> None:
    if not bugs:
        return
    header = f"{'#':>2}  {'SEVERITY':<8}  {'BUG':<54}  ISSUE"
    print(paint(header, "dim"))
    colors = {
        "critical": ("red", "bold"),
        "high": ("red",),
        "medium": ("yellow",),
        "low": ("cyan",),
    }
    for index, bug in enumerate(bugs, start=1):
        number = _issue_number(bug.issue_url or "")
        issue = f"#{number}" if number else "-"
        severity = f"{bug.severity:<8}"
        print(
            f"{index:>2}  {paint(severity, *colors[bug.severity])}  "
            f"{_clip(bug.bug_title, 54):<54}  {paint(issue, 'blue')}"
        )


def print_verification(results: list[VerifyResult], *, dry_run: bool) -> None:
    print()
    if not results:
        status("VERIFY", "nothing to verify: the suite is empty", "WARN")
        return

    for result in results:
        tag = {"fixed": "PASS", "still_failing": "FAIL", "inconclusive": "SKIP"}[result.status]
        message = result.case.name
        if result.status != "fixed":
            message += f"  {result.detail}"
        status(tag, message)
        if result.issue_action:
            number = _issue_number(result.case.issue_url or "")
            prefix = f"#{number} " if number else ""
            tone = "PASS" if "closed" in result.issue_action else "WARN"
            status("ISSUE", f"{prefix}{result.issue_action}", tone)

    fixed = sum(1 for r in results if r.status == "fixed")
    failing = sum(1 for r in results if r.status == "still_failing")
    unknown = sum(1 for r in results if r.status == "inconclusive")
    suffix = "  (dry run: GitHub untouched)" if dry_run else ""
    status(
        "VERIFY",
        f"{fixed} fixed, {failing} still failing, {unknown} inconclusive of {len(results)}{suffix}",
        "PASS" if not failing else "FAIL",
    )


def write_report(
    bugs: list[Bug],
    *,
    evidence_dir: Path,
    actions: int,
    pages: list[str],
    notes: list[str],
    summarized_by: str,
    issues: list[dict[str, Any]] | None = None,
) -> Path:
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "explore",
        "actions": actions,
        "pages_visited": pages,
        "bugs": [bug.as_dict() for bug in bugs],
        "issues": issues or [],
        "notes": notes,
        "summarized_by": summarized_by,
    }
    path = evidence_dir / "report.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_explore_markdown(
    path: Path,
    bugs: list[Bug],
    *,
    app_url: str,
    actions: int,
    pages: list[str],
    summarized_by: str,
    suite_path: Path,
    notes: list[str],
) -> Path:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# QA report - {app_url}",
        "",
        f"- **Mode:** explore",
        f"- **Generated:** {now}",
        f"- **Actions:** {actions} across {len(pages)} page(s)",
        f"- **Bugs found:** {len(bugs)}",
        f"- **Summaries:** {summarized_by}",
        f"- **Suite:** `{suite_path.as_posix()}`",
        "",
        "## Summary",
        "",
    ]

    if bugs:
        lines += ["| # | Severity | Bug | Issue |", "| --- | --- | --- | --- |"]
        for index, bug in enumerate(bugs, start=1):
            number = _issue_number(bug.issue_url or "")
            issue = f"[#{number}]({bug.issue_url})" if number else "-"
            lines.append(
                f"| {index} | {bug.severity} | {_md_cell(_clip(bug.bug_title, 90))} | {issue} |"
            )
    else:
        lines.append("No bugs found.")

    lines += ["", "## Findings", ""]
    for index, bug in enumerate(bugs, start=1):
        lines += [
            f"### {index}. {bug.bug_title}",
            "",
            f"- **Severity:** {bug.severity}",
            f"- **URL:** {bug.url}",
            f"- **Action:** {bug.action_that_failed}",
            f"- **Error:** `{_md_inline(bug.error_message)}`",
            f"- **Screenshot:** `{bug.screenshot or 'none'}`",
            f"- **Issue:** {bug.issue_url or 'not filed'}",
            f"- **Summarised by:** {bug.summarized_by}",
        ]
        if bug.variants:
            lines.append(f"- **Also seen via:** {'; '.join(bug.variants[:4])}")
        if bug.evidence:
            lines += ["", "**Evidence**", ""]
            lines += [f"- {_md_inline(item)}" for item in bug.evidence]
        if bug.repro_steps:
            lines += ["", "**Repro**", ""]
            lines += [
                f"{step_index}. {step}"
                for step_index, step in enumerate(bug.repro_steps, 1)
            ]
        lines.append("")

    lines += ["## Pages visited", ""]
    lines += [f"- {url}" for url in pages] or ["- none"]
    if notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in notes]
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_verify_report(results: list[VerifyResult], *, evidence_dir: Path, suite_path: Path) -> Path:
    counts: dict[str, int] = {"fixed": 0, "still_failing": 0, "inconclusive": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "verify",
        "suite": suite_path.as_posix(),
        "summary": counts,
        "results": [result.as_dict() for result in results],
    }
    path = evidence_dir / "verify-report.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_verify_markdown(
    path: Path,
    results: list[VerifyResult],
    *,
    app_url: str,
    suite_path: Path,
) -> Path:
    fixed = sum(1 for r in results if r.status == "fixed")
    failing = sum(1 for r in results if r.status == "still_failing")
    unknown = sum(1 for r in results if r.status == "inconclusive")
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    lines = [
        f"# Verification report - {app_url}",
        "",
        f"- **Mode:** verify",
        f"- **Generated:** {now}",
        f"- **Suite:** `{suite_path.as_posix()}`",
        f"- **Result:** {fixed} fixed, {failing} still failing, {unknown} inconclusive",
        "",
        "## Results",
        "",
        "| Test | Status | Detail | Issue | GitHub action |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        number = _issue_number(result.case.issue_url or "")
        issue = f"[#{number}]({result.case.issue_url})" if number else "-"
        lines.append(
            f"| `{result.case.name}` | {result.status} | "
            f"{_md_cell(_clip(result.detail, 90))} | {issue} | "
            f"{_md_cell(result.issue_action or '-')} |"
        )

    if fixed:
        lines += ["", "## Fixed", ""]
        for result in results:
            if result.status != "fixed":
                continue
            lines.append(f"- **{result.case.name}** - {result.detail}")
            if result.after_screenshot:
                lines.append(f"  - After: `{result.after_screenshot}`")
            if result.issue_action:
                lines.append(f"  - GitHub: {result.issue_action}")

    if failing:
        lines += ["", "## Still failing", ""]
        for result in results:
            if result.status == "still_failing":
                lines.append(f"- **{result.case.name}** - {result.detail}")

    if unknown:
        lines += ["", "## Inconclusive", ""]
        for result in results:
            if result.status == "inconclusive":
                lines.append(f"- **{result.case.name}** - {result.detail}")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Explore a web app for bugs, then verify the fixes.",
    )
    subparsers = parser.add_subparsers(dest="command")

    explore = subparsers.add_parser(
        "explore", help="crawl the app, report bugs, file issues and save a test suite"
    )
    verify = subparsers.add_parser(
        "verify", help="re-run the saved suite and close the issues that now pass"
    )

    for name, sub in (("explore", explore), ("verify", verify)):
        sub.add_argument("--url", help=f"app to test (default {DEFAULT_URL}, or APP_URL)")
        sub.add_argument(
            "--evidence-dir",
            default=DEFAULT_EVIDENCE_DIR,
            help=f"default {DEFAULT_EVIDENCE_DIR}",
        )
        sub.add_argument(
            "--suite",
            help=f"test suite file (default <evidence-dir>/{SUITE_FILENAME})",
        )
        sub.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT_MS / 1000,
            metavar="SECONDS",
            help=f"per-action timeout (default {DEFAULT_TIMEOUT_MS / 1000:g}s)",
        )
        sub.add_argument("--headed", action="store_true", help="show the browser window")
        sub.add_argument(
            "--repo",
            metavar="OWNER/NAME",
            help="GitHub repository that receives the issues (default GITHUB_REPO)",
        )
        sub.add_argument(
            "--screenshot-base",
            metavar="URL",
            help="public URL prefix for evidence/ so screenshots embed in issues",
        )
        sub.add_argument("--no-issues", action="store_true", help="do not touch GitHub")

    explore.add_argument(
        "--login",
        metavar="USER[:PASS]",
        help="sign in after the anonymous pass to see which failures are auth-only",
    )
    explore.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help=f"default {DEFAULT_MAX_PAGES}")
    explore.add_argument("--max-actions", type=int, default=DEFAULT_MAX_ACTIONS, help=f"default {DEFAULT_MAX_ACTIONS}")
    explore.add_argument("--no-llm", action="store_true", help="skip the LLM and use heuristic summaries")

    verify.add_argument(
        "--dry-run",
        action="store_true",
        help="replay the suite but do not comment on or close issues",
    )

    return parser, parser.parse_args(argv)


def prepare_evidence_dir(path: Path) -> int:
    """Drop screenshots from earlier runs so one run = one evidence set.

    Only files matching this agent's own naming pattern are removed, and only at
    the top level: `evidence/baseline/` holds the before-screenshots that the
    saved suite needs, so it is left alone.
    """
    path.mkdir(parents=True, exist_ok=True)
    removed = 0
    for item in path.glob("*.png"):
        if not SHOT_FILE_RE.match(item.name):
            continue
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def reachable(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return False, _first_line(str(exc))
    if response.status_code >= 500:
        return False, f"the server answered HTTP {response.status_code}"
    return True, f"HTTP {response.status_code}"


def _suite_path(args: argparse.Namespace, evidence_dir: Path) -> Path:
    return Path(args.suite) if args.suite else evidence_dir / SUITE_FILENAME


def main(argv: list[str] | None = None) -> int:
    global _USE_COLOR
    configure_stdout()
    _USE_COLOR = init_color()
    load_dotenv(ENV_FILE)

    raw = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatible: "python agent.py --url ..." still means explore.
    if not raw or raw[0] not in ("explore", "verify", "-h", "--help"):
        raw = ["explore", *raw]

    parser, args = parse_args(raw)
    if args.command is None:
        parser.print_usage(sys.stderr)
        error("choose a command: explore or verify")
        return 2

    url = (args.url or os.environ.get("APP_URL", "") or DEFAULT_URL).strip()
    if args.timeout <= 0:
        error("--timeout must be positive")
        return 2

    ok, why = reachable(url)
    if not ok:
        error(f"cannot reach {url}: {why}")
        error("start the app first, for example: python -m uvicorn app:app --port 8000")
        return 2

    evidence_dir = Path(args.evidence_dir)
    suite_path = _suite_path(args, evidence_dir)
    stale = prepare_evidence_dir(evidence_dir)
    if stale:
        status("SCAN", f"cleared {stale} screenshot(s) from the previous run", "SCAN")

    status(
        "SCAN",
        f"testing {paint(url, 'blue')} (headless={paint(str(not args.headed), 'bold')})",
    )
    status("SCAN", f"evidence -> {evidence_dir.as_posix()}")

    if args.command == "verify":
        return _run_verify(args, url=url, evidence_dir=evidence_dir, suite_path=suite_path)

    if args.max_pages < 1 or args.max_actions < 1:
        parser.print_usage(sys.stderr)
        error("--max-pages and --max-actions must be at least 1")
        return 2
    return _run_explore(args, url=url, evidence_dir=evidence_dir, suite_path=suite_path)


def _run_explore(args: argparse.Namespace, *, url: str, evidence_dir: Path, suite_path: Path) -> int:
    explorer: Explorer
    browser_version = "unknown"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            browser_version = browser.version
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
            )
            context.set_default_timeout(args.timeout * 1000)
            context.set_default_navigation_timeout(args.timeout * 1000)
            page = context.new_page()

            explorer = Explorer(
                page,
                url,
                evidence_dir=evidence_dir,
                timeout_ms=int(args.timeout * 1000),
                max_pages=args.max_pages,
                max_actions=args.max_actions,
            )
            bugs = explorer.explore()
            if args.login:
                explorer.explain_with_session(bugs, args.login)
            browser.close()
    except PlaywrightError as exc:
        error(f"browser failure: {_first_line(str(exc))}")
        error("if Chromium is missing, run: python -m playwright install chromium")
        return 2

    summarized_by = "heuristic"
    config = None if args.no_llm else load_llm_config()
    if args.no_llm:
        status("LLM", "disabled (--no-llm); heuristic titles", "WARN")
    elif config is None:
        status("LLM", "no API key; heuristic titles", "WARN")
    elif bugs:
        status("LLM", f"summarising {len(bugs)} finding(s) with {config.model}")
        ok, reason = summarize_with_llm(bugs, config)
        if ok:
            summarized_by = "llm"
            status("LLM", f"{len(bugs)} finding(s) summarised", "PASS")
        else:
            status("LLM", f"unavailable: {_clip(reason, 100)}", "WARN")
            _apply_raw_fallback(bugs)
            summarized_by = "raw"
            status("LLM", "falling back to raw error text", "WARN")
    else:
        summarized_by = "llm"

    issues: IssueOutcome | None = None
    if args.no_issues:
        status("ISSUE", "disabled (--no-issues)", "WARN")
    elif bugs:
        github_config = load_github_config(args.repo, args.screenshot_base)
        if github_config is None:
            status("ISSUE", "no GITHUB_TOKEN/GITHUB_REPO; skipping issue filing", "WARN")
        else:
            status("ISSUE", f"filing {len(bugs)} issue(s) in {paint(github_config.repo, 'blue')}")
            try:
                issues = file_issues(
                    bugs,
                    github_config,
                    app_url=url,
                    browser_version=browser_version,
                    headless=not args.headed,
                    evidence_dir=evidence_dir,
                )
            except Exception as exc:  # noqa: BLE001 - filing must never lose findings
                status("ISSUE", f"filing failed: {_first_line(str(exc))}", "WARN")

    # The suite is written after the issue pass so each case carries its issue URL.
    cases = build_suite(bugs, app_url=url, evidence_dir=evidence_dir)
    write_suite(suite_path, cases, app_url=url)

    report = write_report(
        bugs,
        evidence_dir=evidence_dir,
        actions=explorer.actions,
        pages=explorer.pages,
        notes=explorer.notes,
        summarized_by=summarized_by,
        issues=issues.as_rows() if issues else [],
    )
    markdown = write_explore_markdown(
        evidence_dir / "report.md",
        bugs,
        app_url=url,
        actions=explorer.actions,
        pages=explorer.pages,
        summarized_by=summarized_by,
        suite_path=suite_path,
        notes=explorer.notes,
    )

    print()
    status("FOUND", f"{len(bugs)} bug{'' if len(bugs) == 1 else 's'}", "FOUND" if bugs else "PASS")
    print_bug_table(bugs)
    if issues is not None:
        print_issues(issues)
    status("SUITE", f"{len(cases)} test case(s) -> {paint(suite_path.as_posix(), 'blue')}")
    status("REPORT", f"{report.as_posix()}  {markdown.as_posix()}")
    status(
        "DONE",
        f"{len(bugs)} bug(s), {explorer.actions} actions, {len(explorer.pages)} page(s)",
        "FAIL" if bugs else "PASS",
    )

    return 1 if bugs else 0


def _run_verify(
    args: argparse.Namespace, *, url: str, evidence_dir: Path, suite_path: Path
) -> int:
    try:
        cases, recorded_url = load_suite(suite_path)
    except FileNotFoundError:
        error(f"no test suite at {suite_path.as_posix()}")
        error("run `python agent.py explore` first to record one")
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        error(f"cannot read {suite_path.as_posix()}: {exc}")
        return 2

    if not cases:
        status("VERIFY", "the suite has no test cases; nothing to verify", "WARN")
        write_verify_report([], evidence_dir=evidence_dir, suite_path=suite_path)
        write_verify_markdown(
            evidence_dir / "verify-report.md", [], app_url=url, suite_path=suite_path
        )
        return 0

    if recorded_url and _origin(recorded_url) != _origin(url):
        status("VERIFY", f"suite recorded against {recorded_url}; rebasing onto {url}", "WARN")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            browser_version = browser.version
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
            )
            context.set_default_timeout(args.timeout * 1000)
            context.set_default_navigation_timeout(args.timeout * 1000)
            page = context.new_page()

            verifier = Verifier(
                page,
                url,
                evidence_dir=evidence_dir,
                timeout_ms=int(args.timeout * 1000),
            )
            results = verifier.run(cases)
            browser.close()
    except PlaywrightError as exc:
        error(f"browser failure: {_first_line(str(exc))}")
        error("if Chromium is missing, run: python -m playwright install chromium")
        return 2

    fixed = [r for r in results if r.status == "fixed"]
    if args.no_issues:
        status("ISSUE", "disabled (--no-issues)", "WARN")
    else:
        github_config = load_github_config(args.repo, args.screenshot_base)
        if github_config is None:
            if fixed:
                status("ISSUE", "no GITHUB_TOKEN/GITHUB_REPO; not updating issues", "WARN")
        elif fixed:
            status("ISSUE", f"updating {len(fixed)} issue(s) in {paint(github_config.repo, 'blue')}")
            try:
                publish_verifications(
                    results,
                    github_config,
                    app_url=url,
                    browser_version=browser_version,
                    headless=not args.headed,
                    evidence_dir=evidence_dir,
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001 - reporting must never lose results
                status("ISSUE", f"update failed: {_first_line(str(exc))}", "WARN")

    report = write_verify_report(results, evidence_dir=evidence_dir, suite_path=suite_path)
    markdown = write_verify_markdown(
        evidence_dir / "verify-report.md",
        results,
        app_url=url,
        suite_path=suite_path,
    )

    print_verification(results, dry_run=args.dry_run)
    status("REPORT", f"{report.as_posix()}  {markdown.as_posix()}")

    still_failing = any(r.status == "still_failing" for r in results)
    return 1 if still_failing else 0


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------


def _kind_of(bug: Bug) -> str:
    if bug.evidence and bug.evidence[0].startswith("HTTP"):
        return "http"
    if any(item.startswith("uncaught error") for item in bug.evidence):
        return "javascript"
    if any(item.startswith("dialog") for item in bug.evidence):
        return "dialog"
    if "no visible effect" in bug.error_message:
        return "no_effect"
    return "unknown"


def _changed(before: dict[str, Any], after: dict[str, Any], signals: Signals) -> bool:
    return (
        after["url"] != before["url"]
        or after["items"] != before["items"]
        or after["text"] != before["text"]
        or bool(signals.navigations)
        or bool(signals.popups)
    )


def _control_label(step: dict[str, Any]) -> str:
    name = str(step.get("name") or "").strip()
    selector = str(step.get("selector") or "").strip()
    return name or selector or "unknown control"


def _rebase(url: str, target: str) -> str:
    """Keep the recorded path, but send it to the host being verified."""
    parsed = urlparse(url)
    goal = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return urljoin(target, url)
    return urlunparse(
        (goal.scheme, goal.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _same_origin(url: str, origin: str) -> bool:
    return _origin(url) == origin


def _path_of(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _page_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def _same_page(left: str, right: str) -> bool:
    return _page_key(left) == _page_key(right)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _snapshot_text(page: Page) -> str:
    try:
        data = page.evaluate(SNAPSHOT_JS)
        return _clip(_normalize_text(str(data.get("text", ""))), 400)
    except PlaywrightError:
        return ""


def _label_of(locator: Locator) -> str:
    """Readable name for a control, whatever kind of node it is."""
    try:
        text = (locator.inner_text(timeout=1_000) or "").strip()
        if text:
            return _normalize_text(text)
    except PlaywrightError:
        pass
    for attribute in ("aria-label", "value", "name", "placeholder", "title"):
        try:
            value = (locator.get_attribute(attribute) or "").strip()
        except PlaywrightError:
            continue
        if value:
            return _normalize_text(value)
    return ""


def _unique(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()[:200]
    return "unknown error"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())