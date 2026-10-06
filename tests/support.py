import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "bin" / "senpai"
FIXTURES = ROOT / "tests" / "fixtures"
FAKE_ANI_PY = FIXTURES / "fake-ani-py"


def run_senpai(*args, env=None, timeout=30):
    """Run bin/senpai as the shell would; returns (rc, parsed stdout, stderr)."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    full_env.setdefault("SENPAI_ANI_PY", str(FAKE_ANI_PY))
    done = subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True, text=True, timeout=timeout, env=full_env,
    )
    payload = json.loads(done.stdout) if done.stdout.strip() else None
    return done.returncode, payload, done.stderr


def load_helper():
    """Import bin/senpai as a module for its pure functions."""
    spec = importlib.util.spec_from_loader("senpai", importlib.machinery.SourceFileLoader("senpai", str(HELPER)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def argv_log(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
