# AGENTS.md — Attack/Defense CTF Platform

## Project

Diploma project: Attack/Defense CTF Platform built with Python, Django and Docker.

The developer is a beginner. Prioritize maintainable architecture, clear explanations, safe migrations and incremental delivery.

## Before making changes

1. Read `docs/PROJECT_STATUS.md` and `docs/ROADMAP.md`.
2. Inspect the relevant existing source files and migrations.
3. Identify dependencies and compatibility risks.
4. Preserve existing data and working functionality.
5. Do not assume planned functionality already exists.

## Development rules

- Implement complete, reasonably scoped features rather than isolated snippets.
- Follow Django conventions and keep business logic separate from HTTP views.
- Add or update tests for changed business logic.
- Do not rewrite existing migrations or delete the database.
- Do not change the user model or team relationships without an explicit migration strategy.
- Do not modify or delete Docker containers, images, volumes or networks without explicit approval.
- Do not run destructive or irreversible commands without explicit approval.
- Never read, print, commit or expose secrets from `.env`.
- Keep test CTF services bound to `127.0.0.1` during local development.
- Do not claim that localhost port binding provides full Attack/Defense isolation.
- Do not introduce unnecessary frameworks or dependencies.
- Do not silently change the architecture of existing components.

## Verification

Where applicable, run:

- `python manage.py check`
- `python manage.py test`
- `python manage.py makemigrations --check --dry-run`

If dependencies, Docker or the environment prevent verification, report the limitation. Do not invent test results.

## Task completion

At the end of each implementation task:

1. Summarize the changed files and key decisions.
2. Explain the important implementation details in beginner-friendly terms.
3. List tests performed and their results.
4. Identify any remaining limitations or risks.
5. Update `docs/PROJECT_STATUS.md` and `docs/ROADMAP.md` when an actual project milestone is completed.
6. Do not commit, push or merge changes unless explicitly requested.

## Current architectural direction

Use Django's standard authentication User model unless a justified change is approved.

The future architecture should distinguish:
- User
- Team
- TeamMembership
- Competition
- CompetitionParticipation
- TeamService belonging to a specific competition participation

Existing Team and TeamService records must be preserved during migration.

Treat the existing Docker management as a local prototype, not as a production-ready isolated Attack/Defense environment.
