# Project Handoff: Java Code Agent

## Current status

The project is a working Streamlit prototype for Java-only code generation. The default request path is:

```text
input normalization and rules
  -> optional local model guardrails
  -> Claude generation
  -> Java extraction and policy checks
  -> javac validation
  -> display validated source
```

If the first generated response fails one of the retryable checks, the application performs one repair request. It does not execute generated Java code.

## Start locally

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Set ANTHROPIC_API_KEY in .env
streamlit run app.py
```

Install JDK 17 or newer and ensure `javac` is available on `PATH`. A portable compiler can be selected with `JAVAC_PATH`.

## Verify locally

Run the deterministic test suite:

```powershell
python -m pytest -q
```

For a production-like run, keep `REQUIRE_JAVAC=true`. The tests use `REQUIRE_JAVAC=false` so they do not require a JDK during unit testing.

## Important configuration notes

- `.env` is ignored and must never be committed.
- `ANTHROPIC_API_KEY` is required unless the application is only being tested through guardrail functions.
- `REQUIRE_JAVAC=true` is the safe default; `REQUIRE_JAVAC=false` is intended only for local experimentation.
- Local model guardrails are enabled with `ENABLE_LOCAL_MODEL_GUARDRAILS=true` and fail closed by default when required models are unavailable.
- The first local-model run may download model files and require additional disk space and startup time.
- `agent.log` contains operational diagnostics, while `latency_metrics.json` contains the latest timing report.

## Known limitations

- The application does not execute, sandbox, or security-scan generated Java programs.
- The default guardrails are policy checks, not a complete malware or vulnerability detector.
- The UI, orchestration, model integration, compiler validation, and most guardrail logic currently live in `app.py`.
- The generated source is limited to the configured output size and line count.
- Claude availability, model names, API quotas, and network access affect generation.
- Some valid Java requests may be rejected because the input classifier is intentionally narrow.

## Where to continue

1. Add integration tests that exercise the Streamlit request flow with mocked Anthropic responses.
2. Split `app.py` into `ui.py`, `pipeline.py`, `guardrails.py`, `compiler.py`, and `provider.py` as functionality grows.
3. Add rate limiting, authentication, and deployment secrets management before shared or public deployment.
4. Add a sandboxed execution service only if a future requirement needs runtime output.
5. Review the blocked API policy and expand it with an explicit allowlist or static analysis tool.

## Next planned change

- Add a tokenizer/token-usage label to the Streamlit interface so users can see request and response token usage.

## Documentation map

- [README](README.md) — user-facing setup and project overview.
- [Architecture proposal](architecture/proposal.md) — goals, scope, workflow, and future direction.
- [Architecture design](architecture/design.md) — implementation details and operational design.
- [Changelog](CHANGELOG.md) — documented project changes.
