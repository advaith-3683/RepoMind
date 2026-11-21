"""Exercise real pytest failure output without loading any developer secrets."""
import os
import subprocess
import sys
from pathlib import Path


def test_failure_output_does_not_expose_settings_credentials(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_text(
        (backend / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    dotenv_secret = "fake-dotenv-secret-for-regression"
    env_secret = "fake-environment-secret-for-regression"
    explicit_secret = "fake-explicit-secret-for-regression"
    (tmp_path / ".env").write_text(
        f"GEMINI_API_KEY={dotenv_secret}\nLLM_FALLBACK_MODEL=unexpected-local-model\n",
        encoding="utf-8",
    )
    (tmp_path / "test_probe.py").write_text('''
import os
from app.config import Settings, get_settings

# These run during collection, before autouse fixtures.
assert Settings().gemini_api_key == ""
assert Settings().llm_api_key == ""
assert Settings().llm_fallback_model == ""
assert get_settings().gemini_api_key == ""

def test_deliberate_failure():
    assert Settings(gemini_api_key=os.environ["TEST_EXPLICIT_SECRET"]).gemini_api_key == ""

def test_deliberate_repr_failure():
    settings = Settings(gemini_api_key=os.environ["TEST_EXPLICIT_SECRET"])
    print(settings.gemini_api_key)
    import sys
    print(settings.gemini_api_key, file=sys.stderr)
    assert isinstance(settings, dict)
''', encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(backend),
        "GEMINI_API_KEY": env_secret,
        "LLM_API_KEY": env_secret,
        "TEST_EXPLICIT_SECRET": explicit_secret,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "--showlocals", "-p", "no:cacheprovider"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    # Never include captured output in this test's own assertion messages.
    assert result.returncode == 1, "Expected deliberate assertion failures"
    leaked = any(secret in output for secret in (dotenv_secret, env_secret, explicit_secret))
    assert not leaked, "Credential sentinel appeared in pytest failure output"
    assert "2 failed" in output, "Probe did not reach both deliberate failures"
