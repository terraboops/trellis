# Trellis Agent Guidelines

Instructions for AI agents (Claude Code, etc.) working in this repository.

## Before Every Commit

Run the automated test suite:
```
.venv/bin/pytest tests/ -x -q
```

For UI-touching changes (templates, routes, frontend JS), also run the browser test:
```
/test-trellis
```
Navigate to the running Trellis instance (default: http://localhost:8000), work through the checklist in the skill, and confirm all pages load correctly before committing.

## After Changing Pipeline Logic

Re-run the E2E pipeline tests explicitly:
```
.venv/bin/pytest tests/test_e2e_pipeline.py tests/test_e2e_web_pipeline.py -v
```

## Release Checklist

Before bumping version and pushing a release:
1. All tests pass
2. `/test-trellis` browser test passes
3. `trellis serve --list-log-formats` works
4. `/healthz`, `/readyz`, `/metrics` endpoints respond correctly
5. Version bumped in `pyproject.toml`
6. Homebrew formula updated in `terraboops/homebrew-tap`
