# CLAUDE_RULES.md — Master Coding Contract

Read this at the start of every session. Acknowledge these rules before writing any code. These rules override defaults and apply to every answer, every file, and every modification.

---

## 1. Infrastructure — Hetzner
- All projects share a central dashboard hosted on Hetzner
- Individual project dashboards are accessed through the central dashboard
- The central dashboard must show a Hetzner or Local tag per project
- No project may be deployed to Hetzner without explicit prior approval — always ask first
- Some projects run locally — link them in the central dashboard but do not deploy to Hetzner
- Maintain a `port-registry.md` — no port conflicts allowed between any services
- Never bind to a port already in use; verify with `lsof` or `ss` before assigning
- After stopping a service, verify the port is actually free before restarting — launchd and similar process managers can leave stale processes holding a port; use `lsof -i :<port>` and `kill -9 <PID>` if needed
- Never assume a service restarts cleanly; avoid code that creates stale locks or processes
- When launching services via launchd or system-level daemons on macOS: system Python and other interpreters run in a restricted sandbox without access to `~/Documents/` or user home directories; copy required files into a system-accessible path or use a user-level launch agent instead

## 2. Git / Code Workflow
- All code lives in Git + private GitHub repos — not in Google Drive or any cloud sync folder
- Use Git + private GitHub repos for cross-machine sync
- Before each commit, write/update `HANDOFF.md` in the project root:
  - What was done, files changed, what's pending, key decisions
- Use descriptive commit messages
- Before any git operation: check for stale `.git/HEAD.lock` files (caused by cloud drive sync)
- Never embed tokens or credentials in remote URLs
- Never assume GitHub auth or token scopes are correct — call out if a push or workflow might fail
- Never generate files that conflict with existing `.gitignore` rules
- Never assume repo location; avoid absolute paths

## 3. General Coding Behavior
Top priority: correct, robust, production-ready code — never speculative or placeholder code.
- At the start of any coding task, scan the current session history for decisions, features, or fixes that were discussed but not yet implemented. List any open items found and ask the user whether to proceed, defer, or drop them before writing new code.
- Always plan before coding:
  - Summarize the task in your own words
  - List key files and modules to touch
  - List likely failure modes (paths, ports, env vars, locks, pagination, auth, build)
- Do not start writing code until the plan is shown and confirmed
- Prefer small, focused changes over big rewrites
- Respect existing architecture and behavior
- Never introduce new dependencies without explicitly stating them

Output structure for each task:
1. Goal restatement (1–3 sentences)
2. Implementation plan (numbered steps)
3. Risks / edge cases / deployment implications
4. Code changes, grouped by file, with complete updated files where non-trivial
5. Short checklist of commands to run (build, migrate, restart, tests)

If any instruction conflicts with this file, point out the conflict and ask which to follow.

## 4. Repo Awareness and File Handling
- Never invent directories, files, or commands that don't exist
- When ambiguous, ask to run `ls`, `tree`, `find`, or paste relevant files
- When making non-trivial edits: show the full updated file, not fragments
- No leftover old blocks, duplicated functions, or half-applied patches

## 5. Modifying Existing Code (Critical)
When asked for any change:
1. Read the existing code carefully
2. Summarize what it currently does
3. Summarize what needs to change
4. Apply changes surgically — preserve all unrelated logic
5. Never remove imports, variables, or functions unless explicitly approved
6. Never create duplicate declarations
7. Never leave stale references or broken indentation
8. Never rewrite entire files unless explicitly requested

When refactoring:
- First list all dependencies (imports, env vars, paths, ports, DB tables, APIs)
- Propose the refactor and wait for confirmation before writing code
- If renaming or moving symbols: update every visible reference and list files needing search/replace
- If there's a risk of port conflicts, stale services, or leftover processes: say exactly what to restart or stop

## 6. Module Systems, Builds, and Paths
- Never mix CommonJS and ESM in the same file
- If a file uses `import`/`export`: no `require` or `module.exports`
- Never assume `__dirname` resolves to the same depth after compilation — verify it
- When using `__dirname` in compiled output, compute depth carefully (e.g. `../..` not `..`)
- For any external file (schema.sql, data dirs, static bundles), confirm:
  - It exists in the repo
  - It will exist in the build output
  - Docker `COPY` includes it
  - How runtime code resolves its path
- Assume macOS `tar`/zip may omit directories, add `._*` files, or break symlinks — use `rsync` or explicit `COPY` instead
- When running `npx tsc`, use `npx -p typescript tsc` to avoid picking up unrelated packages

## 7. Docker and Deployment Safety
- Ensure Docker `COPY` includes all required files (SQL, data, config, static assets)
- Ensure no file referenced at runtime is missing from the container
- Ensure no `ENOENT` errors can occur due to missing files or wrong paths
- No deployment to Hetzner without explicit user approval (see §1)

## 8. Databases and Locks
- DB initialization must run exactly once at startup — never on every request
- Never recreate tables on every request
- Never open a new DB connection unnecessarily — use one shared connection or small pool per process
- Wrap schema creation in idempotent blocks (`IF NOT EXISTS`)
- Be conscious of write locks — background pollers holding a lock will block web requests
- Avoid duplicate patches or indentation errors that change control flow
- Never copy a SQLite database file while it is in use — always stop the service first, then copy, then restart
- When moving or syncing SQLite files (e.g. via Google Drive, rsync, scp): ensure the WAL file (`.db-wal`) and shared memory file (`.db-shm`) are also copied or cleanly absent — copying only the `.db` file while WAL exists will corrupt the database
- If corruption occurs: stop all services, run `sqlite3 <db> "PRAGMA integrity_check;"` to assess, restore from backup

## 9. HTTP Servers, Ports, and Nginx
- Always think through the full chain: Browser → Nginx → backend (port) → DB / API / static files
- Before changing ports or locations: confirm the backend port and current active Nginx config (check `sites-enabled`, not just `sites-available`)
- Do not apply `auth_basic` globally if static assets should remain public
- Ensure `location` blocks do not block static files (CSS, JS, images)
- Ensure filesystem paths in backend/Nginx match where build artifacts actually land
- Verify proxy port matches the actual running service port before saving config

## 10. External APIs
- Never guess API parameter names — confirm from docs or working calls
- Always confirm pagination semantics (`startRecord`, `numberOfRecords`, `sortOrder`, date ranges)
- Ensure pagination logic cannot loop infinitely or fetch the same page repeatedly
- Log enough to see which page/offset is being fetched
- Handle rate limits — design incremental sync and call budgeting
- When endpoints return 410, 404, or "not available": stop calling them in loops, check capability flags

## 11. Frontend and Dashboard JavaScript
- Declare shared state at the top of the module — avoid "Cannot access X before initialization"
- Never place large inline `<script>` blocks (>~200KB) — use external JS files
- Understand that parse errors in inline scripts kill `DOMContentLoaded` and all event listeners
- When touching DOM structure: preserve attributes used by code (`data-page`, IDs, classes)
- If switching to inline `onclick`, keep any `data-*` attributes that other functions rely on
- Ensure all referenced assets (CSS, JS, images) exist and are included in the build
- Never use `file://` URLs for local development — browsers (especially Safari) block them with sandbox errors; always serve via `localhost`
- Never rely on symlinks in served directories — `SimpleHTTPRequestHandler` and many static servers do not follow symlinks; copy files into the served directory instead

## 12. Python Specifics
- Use `datetime.now(timezone.utc)` not `datetime.utcnow()`
- Use `os.getenv("VAR", "default")` correctly — never pass extra args
- Construct paths explicitly with `os.path.join(base_dir, relative)`
- When adding imports (e.g. `python-dotenv`): state that the package must be installed and where
- Assume virtualenvs on server — mention if a new package is needed in `requirements.txt`
- After any refactor: verify no stale variable references remain

## 13. Pre-Output Validation Checklist
Before outputting any code, run this checklist. Only output if all pass:
- [ ] Are all imports correct and actually used?
- [ ] Are all paths correct in both source and after build?
- [ ] Will Docker include all referenced files?
- [ ] Are there any duplicated variable declarations?
- [ ] Are there any stale references to removed symbols?
- [ ] Are all new dependencies accounted for and stated?
- [ ] Will this compile cleanly?
- [ ] Will this run cleanly after build?
- [ ] Will this avoid DB locks?
- [ ] Will this avoid ENOENT errors?
- [ ] Will this avoid browser parse errors?
- [ ] Will this avoid API pagination bugs?
- [ ] Are all ports conflict-free per port-registry.md?

## 14. Quick Modes
If not specified, treat this file as the default operating mode. Special modes:
- **"Minimal, surgical fix"** — fix only the described bug, no refactors, smallest possible change
- **"Refactor safely"** — improve structure, keep behavior identical, list verification checks
- **"Production-ready"** — favor correctness, robustness, clear logging, and full deployment notes

---

*Place this file at the root of every project repo. Claude Code reads `CLAUDE.md` automatically at session start.*
