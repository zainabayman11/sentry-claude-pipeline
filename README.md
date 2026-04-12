# Sentry Claude Pipeline

An automated bug-fixing pipeline that takes Sentry error clusters, uses Claude AI to plan, implement, review, and ship fixes — with minimal human involvement.

---

## Concepts

| Term | Definition |
|------|-----------|
| **Issue** | A single error event from Sentry |
| **Cluster** | A group of similar issues sharing the same root cause |
| **Debug Packet** | A structured JSON file describing one cluster in a debugging-ready format |
| **Fix Unit** | One cluster = one worktree = one branch = one PR |

---

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude auth login`)
- [GitHub CLI](https://cli.github.com) installed and authenticated (`gh auth login`)
- Git installed

> **Authentication:** This pipeline uses your Claude subscription (Pro/Max) via Claude Code CLI.
> No `ANTHROPIC_API_KEY` is required if you are logged in via `claude auth login`.

---

## Installation

```bash
git clone <this-repo>
cd sentry-claude-pipeline
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Configuration — what you MUST change

### 1. `.claude/CLAUDE.md`
Update these sections to match your project:

```markdown
## Core Rules
- Never modify core <YOUR_FRAMEWORK> files unless explicitly allowed.
- Respect existing project patterns and module boundaries.
```

If you are not using Odoo, remove the **Odoo-Specific Safety** section entirely.

### 2. Your Debug Packet (`data/your_cluster.json`)
Create one JSON file per cluster. See `data/sample_debug_packet.json` for the full schema.

Key fields to update:
```json
{
  "code_hints": {
    "repo": "/absolute/path/to/your/repo",   ← MUST be an absolute path on this machine
    "files": ["path/to/file/relative/to/repo"]
  }
}
```

### 3. `memory/incidents.json`
Start with an empty array `[]` — the pipeline fills it automatically after each fix.

---

## Usage

Run each phase in order for a single cluster:

```bash
# 1. Analyze the bug and produce a fix plan (read-only)
python -m app.pipeline plan data/your_cluster.json

# 2. Implement the fix in an isolated git worktree
python -m app.pipeline execute data/your_cluster.json

# 3. Review the changes (read-only)
python -m app.pipeline review data/your_cluster.json

# 4. Commit, push, and open a GitHub PR
python -m app.pipeline push data/your_cluster.json

# Optional: save fix to memory without pushing
python -m app.pipeline memory data/your_cluster.json

# Optional: resume an interrupted execute phase
python -m app.pipeline resume-execute data/your_cluster.json
```

---

## Output Structure

```
.pipeline_state/
└── <cluster_id>/
    ├── plan.md               ← Claude's fix plan
    ├── execution_summary.md  ← what was implemented
    ├── review.md             ← code review + PR body
    ├── session_notes.md      ← full session log
    └── meta.json             ← worktree path + session IDs

<your_repo>/
└── worktrees/
    └── fix-<cluster_id>/     ← isolated git worktree
        ├── pr_draft.md
        └── plan.md

memory/
└── incidents.json            ← past fixes (used for future matching)
```

---

## How Memory Works

After each successful push, the pipeline saves the fix to `memory/incidents.json`.

Next time a similar cluster arrives, the plan phase:
1. Scores similarity using error type, module, and file location
2. If score ≥ 0.4, sends past fix as **context** (not a directive) to Claude
3. Claude decides whether to reuse, adapt, or investigate from scratch

---

## Debug Packet Schema

```json
{
  "summary": {
    "cluster_id": "cluster-unique-id",
    "title": "Short human-readable title",
    "priority": "P1",
    "projects": ["your-project-name"],
    "issue_ids": ["PROJ-001", "PROJ-002"]
  },
  "common_patterns": {
    "platform": "python",
    "error_type": "OperationalError",
    "exc_module": "sql_db",
    "location": "path/to/file.py",
    "environment": "production",
    "release": "1.0.0"
  },
  "diagnostic_context": {
    "primary_error_message": "The full error message",
    "culprit": "module.function",
    "transaction": "/endpoint/path",
    "logger": "your.logger.name"
  },
  "representative_traces": [
    {
      "issue_id": "PROJ-001",
      "stack_trace": [
        {
          "file": "path/to/file.py",
          "line": 42,
          "function": "function_name",
          "context": "line of code here"
        }
      ]
    }
  ],
  "code_hints": {
    "repo": "/absolute/path/to/repo",
    "module": "module/path",
    "files": ["path/to/file.py"],
    "search_terms": ["term1", "term2"]
  },
  "sentry_links": [
    "https://sentry.example/issue/001"
  ]
}
```

---

## Project Structure

```
sentry-claude-pipeline/
├── app/
│   ├── pipeline.py        # CLI entrypoint — orchestrates all phases
│   ├── claude_runner.py   # Claude Agent SDK wrapper + UI
│   ├── prompts.py         # Prompts for each phase
│   ├── memory_store.py    # Fuzzy incident matching + persistence
│   ├── git_ops.py         # Worktree, commit, push, PR creation
│   └── models.py          # Debug packet validation
├── .claude/
│   ├── CLAUDE.md          # Rules injected into every Claude session
│   └── skills/            # Custom skills (sentry-debug, odoo-fix)
├── data/
│   └── sample_debug_packet.json
├── memory/
│   └── incidents.json
└── requirements.txt
```
