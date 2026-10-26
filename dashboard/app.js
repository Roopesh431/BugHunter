
const $ = id => document.getElementById(id);

function toast(message) {
  $("toast").textContent = message;
  $("toast").style.display = "block";
  setTimeout(() => $("toast").style.display = "none", 3500);
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[ch]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

async function refresh() {
  try {
    const data = await api("/api/status");
    const run = data.run;
    const findings = data.findings;

    $("connection").textContent = "Backend connected";
    $("target").value = $("target").value || data.target_url;
    $("bug-count").textContent = findings.count;
    $("pages").textContent = Array.isArray(findings.pages)
      ? findings.pages.length : (findings.pages || "—");
    $("run-status").textContent = run.running
      ? "Running…" : (run.kind ? "Finished" : "Idle");
    $("output").textContent = run.error || run.output || "No run output yet.";

    const bugs = findings.bugs || [];
    $("findings-list").innerHTML = bugs.length
      ? bugs.map(b => `
        <div class="finding">
          <strong>${escapeHTML(b.bug_title || b.title || b.message || "Detected issue")}</strong>
          <p>${escapeHTML(b.description || b.error_message || b.message || "")}</p>
        </div>`).join("")
      : "No findings in the latest report.";
  } catch (error) {
    $("connection").textContent = "Backend unavailable";
    $("output").textContent = error.message;
  }
}

async function startRun(kind) {
  try {
    const result = await api("/api/run/" + kind, {
      method: "POST",
      body: JSON.stringify({ target_url: $("target").value.trim() })
    });
    toast(result.message);
    refresh();
  } catch (error) {
    toast(error.message);
  }
}

$("start-target").onclick = async () => {
  try {
    const result = await api("/api/target/start", { method: "POST" });
    toast(result.message);
    refresh();
  } catch (error) {
    toast(error.message);
  }
};

$("explore").onclick = () => startRun("explore");
$("verify").onclick = () => startRun("verify");
$("open-report").onclick = () => window.open("/api/report", "_blank");

$("load-evidence").onclick = async () => {
  try {
    const data = await api("/api/evidence");
    $("evidence-list").innerHTML = data.files.map(file =>
      `<div class="evidence-item">
        <a href="${escapeHTML(file.url)}" target="_blank">
          ${escapeHTML(file.name)}
        </a>
      </div>`
    ).join("") || "No evidence files found.";
  } catch (error) {
    toast(error.message);
  }
};

refresh();
setInterval(refresh, 3000);
