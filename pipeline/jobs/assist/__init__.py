"""
Desktop-side apply-assist runner (docs/_research/2026-09-21_jobs-collector.md "Apply Assist").

Runs on the user's own machine via `python -m pipeline.jobs.assist`, using requirements-assist.txt
(T-001) — a separate, heavier dependency set (Playwright) than the server's requirements.txt. This
file stays importable wherever the base requirements.txt is installed and Playwright is not (e.g.
this repo's own CI test suite): it carries no Playwright import and no logic beyond this docstring.
Later tasks (T-026/T-028/T-029/T-030) add Playwright-dependent submodules to this package; only
those submodules, never this one, may import playwright.
"""
