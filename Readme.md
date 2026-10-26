# BugHunter — AI-Powered Autonomous QA Agent

BugHunter is a Python-based QA agent that explores web applications through a real browser, identifies potential bugs, captures screenshots, generates structured bug reports, and can create GitHub issues automatically.

Its goal is to reduce repetitive manual testing and make software defects easier to reproduce, investigate, and verify.

## Problem

Manual testing requires developers and QA engineers to repeatedly navigate pages, click controls, test form inputs, inspect browser errors, and document failures.

Important edge cases can be missed, and reporting bugs with reliable reproduction steps takes time.

BugHunter automates much of this process using browser automation, failure detection, and LLM-assisted reporting.

## Key Features

- **Autonomous browser exploration:** Uses Playwright and Chromium to explore reachable pages and interact with visible controls.
- **Edge-case testing:** Tests empty inputs, whitespace, long strings, special characters, non-ASCII text, and other input variations.
- **JavaScript error detection:** Captures uncaught browser errors and relevant console errors.
- **HTTP failure detection:** Records relevant HTTP 4xx and 5xx responses.
- **Behavioral checks:** Detects controls that appear to do nothing, such as a Delete button that triggers no request or visible change.
- **Screenshot evidence:** Saves screenshots of detected failures in the `evidence/` directory.
- **Structured reporting:** Generates `evidence/report.json` with bug details, reproduction steps, severity, and evidence.
- **AI-assisted summaries:** Uses an LLM to turn raw browser signals into understandable bug titles and reproduction instructions. Heuristic summaries provide a fallback when the LLM is unavailable.
- **GitHub issue integration:** Can create one issue per bug and skip duplicates with matching open issue titles.
- **Regression verification:** Replays saved test cases to check whether previously detected bugs still reproduce.

## Architecture

```text
Target Web Application
          |
          v
  Playwright + Chromium
          |
          v
  Automated Exploration
          |
          v
  Failure Detection
  - JavaScript errors
  - HTTP failures
  - Broken interactions
          |
          v
  Screenshot + Test Evidence
          |
          v
  LLM-Assisted Bug Summaries
          |
          v
  report.json + GitHub Issues
          |
          v
  Replay Tests to Verify Fixes
```

## Tech Stack

- Python 3.13
- FastAPI and Uvicorn for the demo application
- Playwright and Chromium for browser automation
- Requests and python-dotenv
- PyGithub for GitHub issue integration
- An OpenAI-compatible LLM API for optional AI-generated summaries

## Project Structure

```text
bughunter/
├── app.py
├── agent.py
├── requirements.txt
├── .env.example
├── .gitignore
├── AGENTS.md
└── evidence/
    ├── report.json
    ├── suite.json
    └── screenshots/
```

The exact evidence filenames and directory layout depend on your implementation. Screenshots and generated reports should not be committed unless you intentionally want to publish demo evidence.

## Setup

### 1. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Configure environment variables

```powershell
copy .env.example .env
```

Example configuration:

```dotenv
APP_URL=http://localhost:8000

# Optional: LLM-assisted bug summaries
LLM_API_KEY=your_llm_api_key
LLM_MODEL=your_supported_model
LLM_BASE_URL=https://api.openai.com/v1

# Optional: GitHub issue creation
GITHUB_TOKEN=your_github_token
GITHUB_REPO=YOUR_USERNAME/YOUR_REPOSITORY

# Optional: externally accessible screenshot hosting
# GITHUB_SCREENSHOT_BASE=https://your-public-host/evidence
```

Use a GitHub token with the required issue-creation permissions. Keep secrets out of source control.

An LLM key is optional if your agent supports heuristic summaries. For screenshot embedding in GitHub issues, the image URL must be accessible to GitHub viewers; a local file path alone cannot display an image remotely.

## Running BugHunter

### Terminal 1 — Start the demo application

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 in your browser.

The included to-do application is intentionally buggy and is meant to serve as a controlled testing target.

### Terminal 2 — Explore and report bugs

```bash
python agent.py explore --login demo
```

Optional variants:

```bash
python agent.py explore --headed --max-actions 30
python agent.py explore --no-llm
```

The headed mode lets you watch Chromium interact with the application. The no-LLM mode is useful for demonstrating that core failure detection does not depend on an AI API being available.

### Verify fixes

After correcting the application bugs, restart the application if necessary and run:

```bash
python agent.py verify
```

To preview verification results without modifying GitHub issues:

```bash
python agent.py verify --dry-run
```

Use the exact command-line options exposed by your current `agent.py` if your local version differs.

## Demonstration Bugs

The included demo application contains three deliberate defects:

| Bug | Expected detection |
|---|---|
| Adding a task with an empty title causes a server error | HTTP 500 and a browser-side error |
| Delete buttons have no effective click handler | No DELETE request or expected state change |
| Opening `/profile` without a session causes a server error | HTTP 500 instead of redirecting to login |

These defects provide known ground truth against which to demonstrate the agent's findings.

## Example Report

A simplified representation of the structured output:

```json
{
  "bugs": [
    {
      "bug_title": "Adding an empty task causes a server error",
      "url": "http://localhost:8000/",
      "action_that_failed": "Submit Add Task with an empty input",
      "error_message": "HTTP 500 Internal Server Error"
    }
  ]
}
```

This is an illustrative example, not a copy of an actual run. Consult `evidence/report.json` for your real output and the complete schema.

## GitHub Issue Automation

When issue integration is enabled, BugHunter can:

1. Summarize detected failures.
2. Search for an existing open issue with the same normalized title.
3. Create an issue when no duplicate is found.
4. Include reproduction steps, the error message, and environment information.
5. Link or embed screenshot evidence when a valid, accessible image URL is configured.

## Regression Testing

BugHunter can save reproducible test cases and replay them after a developer changes the application.

A useful demonstration is to:

1. Run `explore` against the buggy application.
2. Inspect the report and the created GitHub issues.
3. Fix one of the defects.
4. Run `verify` against the fixed application.
5. Show the updated verification result and any corresponding GitHub issue comment or closure.

This makes BugHunter more than a one-time bug detector: it can also help validate whether a reported defect has been fixed.

## Limitations

- The agent can only discover routes it can reach or that are explicitly supplied.
- Automated exploration cannot guarantee discovery of every defect.
- Heuristic and LLM-generated summaries may require human review.
- Some interactions require application-specific test logic.
- Regression verification depends on saved test cases and reproducible application state.
- GitHub screenshot rendering requires publicly or otherwise appropriately accessible image URLs.

## Future Improvements

- CI/CD integration and automated regression checks.
- Support for authenticated applications and additional browser states.
- Better test coverage metrics and severity prioritization.
- HTML reports with clickable screenshots.
- Support for additional application frameworks and API testing.

## Author

**Roopesh Lingam**

Built as a hackathon project exploring autonomous software testing and AI-assisted defect reporting.
