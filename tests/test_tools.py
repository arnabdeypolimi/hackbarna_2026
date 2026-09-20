"""The developer tools under tools/ fail loudly and early when unconfigured."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_langfuse_smoke_exits_nonzero_when_unconfigured(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE", "TRACING", "OTEL"))}
    env.update({"TRACING_ENABLED": "false", "NEBIUS_API_KEY": "n", "SLNG_API_KEY": "s", "ANAM_API_KEY": "a"})
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "langfuse_smoke.py")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)  # no .env in cwd
    assert proc.returncode == 2
    assert "TRACING_ENABLED=true" in proc.stderr
