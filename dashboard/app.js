
const $ = (id) => document.getElementById(id);

let refreshTimer = null;
let isRefreshing = false;
let isStartingRun = false;
let isStartingTarget = false;
let eventsConnected = false;

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  })[char]);
}

function toast(message) {
  const element = $("toast");

  if (!element) {
    console.log(message);
    return;
  }

  element.textContent = String(message ?? "");
  element.style.display = "block";

  clearTimeout(toast.timer);

  toast.timer = setTimeout(() => {
    element.style.display = "none";
  }, 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(
      data.detail ||
      data.message ||
      `Request failed with status ${response.status}`
    );
  }

  return data;
}

function setText(id, value) {
  const element = $(id);

  if (element) {
    element.textContent = value ?? "";
  }
}

function renderFindings(findings = {}) {
  const container = $("findings-list");

  if (!container) return;

  const bugs = Array.isArray(findings.bugs)
    ? findings.bugs
    : [];

  if (!findings.available && bugs.length === 0) {
    container.textContent =
      "No report available yet. Start an exploration run first.";
    return;
  }

  if (bugs.length === 0) {
    container.textContent =
      "The latest report contains no findings.";
    return;
  }

  container.innerHTML = bugs.map((bug, index) => {
    const title =
      bug.bug_title ||
      bug.title ||
      bug.name ||
      "Untitled finding";

    const severity = String(
      bug.severity || "unknown"
    ).toLowerCase();

    const description =
      bug.error_message ||
      bug.action_that_failed ||
      bug.description ||
      bug.message ||
      "No additional details provided.";

    const url = bug.url || "Unknown";
    const screenshot = bug.screenshot;

    return `
      <article class="finding">
        <strong>
          ${index + 1}. ${escapeHTML(title)}
        </strong>

        <p>
          Severity:
          <span class="severity ${escapeHTML(severity)}">
            ${escapeHTML(severity.toUpperCase())}
          </span>
        </p>

        <p>URL: ${escapeHTML(url)}</p>

        <p>${escapeHTML(description)}</p>

        ${
          screenshot
            ? `<p>Screenshot: ${escapeHTML(screenshot)}</p>`
            : ""
        }
      </article>
    `;
  }).join("");
}

function renderRunStatus(run = {}) {
  if (run.running) return "Running…";

  if (run.exit_code === 0) return "Completed";

  if (run.exit_code !== null && run.exit_code !== undefined) {
    return "Finished with errors";
  }

  if (run.error) return "Finished with errors";

  return "Idle";
}

async function refresh() {
  if (isRefreshing) return;

  isRefreshing = true;

  try {
    const data = await api("/api/status");

    const run = data.run || {};
    const findings = data.findings || {};

    setText("connection", "Backend connected");
    setText("bug-count", findings.count ?? findings.bugs?.length ?? 0);

    const pages = findings.pages;

    setText(
      "pages",
      Array.isArray(pages)
        ? pages.length
        : (pages ?? "—")
    );

    setText("run-status", renderRunStatus(run));

    setText(
      "output",
      run.error ||
      run.output ||
      "Connected. Start an exploration to see agent output."
    );

    const target = $("target");

    if (target && data.target_url && !target.value.trim()) {
      target.value = data.target_url;
    }

    renderFindings(findings);

    const exploreButton = $("explore");
    const verifyButton = $("verify");

    if (exploreButton) {
      exploreButton.disabled =
        Boolean(run.running) || isStartingRun;
    }

    if (verifyButton) {
      verifyButton.disabled =
        Boolean(run.running) || isStartingRun;
    }

    const startButton = $("start-target");

    if (startButton) {
      startButton.disabled =
        Boolean(data.target_reachable) || isStartingTarget;

      startButton.textContent = data.target_reachable
        ? "Demo app is running"
        : isStartingTarget
          ? "Starting demo app…"
          : "Start demo app";
    }
  } catch (error) {
    setText("connection", "Backend unavailable");
    setText("output", error.message || "Unable to connect to backend.");
  } finally {
    isRefreshing = false;
  }
}

async function startRun(kind) {
  if (isStartingRun) return;

  const target = $("target");
  const targetUrl = target ? target.value.trim() : "";

  if (!targetUrl) {
    toast("Enter the target application URL first.");
    return;
  }

  isStartingRun = true;

  const exploreButton = $("explore");
  const verifyButton = $("verify");

  if (exploreButton) exploreButton.disabled = true;
  if (verifyButton) verifyButton.disabled = true;

  try {
    const result = await api(`/api/run/${kind}`, {
      method: "POST",
      body: JSON.stringify({
        target_url: targetUrl
      })
    });

    toast(result.message || `${kind} started`);

    await refresh();
  } catch (error) {
    toast(error.message || `Unable to start ${kind}.`);
  } finally {
    isStartingRun = false;
    await refresh();
  }
}

async function startTarget() {
  if (isStartingTarget) return;

  isStartingTarget = true;

  const button = $("start-target");

  if (button) {
    button.disabled = true;
    button.textContent = "Starting demo app…";
  }

  try {
    const result = await api("/api/target/start", {
      method: "POST"
    });

    toast(result.message || "Target app start requested.");

    await refresh();
  } catch (error) {
    toast(error.message || "Unable to start the target application.");
  } finally {
    isStartingTarget = false;
    await refresh();
  }
}

async function openReport() {
  let objectUrl = null;

  try {
    const response = await fetch("/api/report");

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));

      throw new Error(
        error.detail || "No report available yet."
      );
    }

    const report = await response.json();

    const blob = new Blob(
      [JSON.stringify(report, null, 2)],
      { type: "application/json" }
    );

    objectUrl = URL.createObjectURL(blob);

    const link = document.createElement("a");

    link.href = objectUrl;
    link.download = "bughunter-report.json";

    document.body.appendChild(link);
    link.click();
    link.remove();

    toast("Report download requested.");

    // Give the browser time to begin downloading before cleanup.
    setTimeout(() => {
      URL.revokeObjectURL(objectUrl);
    }, 1000);
  } catch (error) {
    if (objectUrl) URL.revokeObjectURL(objectUrl);

    toast(error.message || "Unable to download the report.");
  }
}

async function loadEvidence() {
  const container = $("evidence-list");

  if (!container) return;

  container.textContent = "Loading evidence…";

  try {
    const data = await api("/api/evidence");
    const files = Array.isArray(data.files) ? data.files : [];

    if (files.length === 0) {
      container.textContent =
        "No evidence files found. Run an exploration first.";
      return;
    }

    container.innerHTML = files.map((file) => `
      <div class="evidence-item">
        <a
          href="${escapeHTML(file.url)}"
          target="_blank"
          rel="noopener noreferrer"
        >
          ${escapeHTML(file.name || "Evidence file")}
        </a>
        <small>${escapeHTML(file.path || "")}</small>
      </div>
    `).join("");
  } catch (error) {
    container.textContent =
      error.message || "Unable to load evidence.";
  }
}

function connectEvents() {
  if (eventsConnected) return;

  eventsConnected = true;

  const exploreButton = $("explore");
  const verifyButton = $("verify");
  const targetButton = $("start-target");
  const refreshButton = $("refresh");
  const reportButton = $("open-report");
  const evidenceButton = $("load-evidence");

  if (exploreButton) {
    exploreButton.addEventListener("click", () => {
      startRun("explore");
    });
  }

  if (verifyButton) {
    verifyButton.addEventListener("click", () => {
      startRun("verify");
    });
  }

  if (targetButton) {
    targetButton.addEventListener("click", startTarget);
  }

  if (refreshButton) {
    refreshButton.addEventListener("click", refresh);
  }

  if (reportButton) {
    reportButton.addEventListener("click", openReport);
  }

  if (evidenceButton) {
    evidenceButton.addEventListener("click", loadEvidence);
  }
}

function initializeDashboard() {
  connectEvents();

  refresh();

  if (refreshTimer !== null) {
    clearInterval(refreshTimer);
  }

  refreshTimer = setInterval(refresh, 2500);
}

if (document.readyState === "loading") {
  document.addEventListener(
    "DOMContentLoaded",
    initializeDashboard,
    { once: true }
  );
} else {
  initializeDashboard();
}

window.addEventListener("beforeunload", () => {
  if (refreshTimer !== null) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
});
