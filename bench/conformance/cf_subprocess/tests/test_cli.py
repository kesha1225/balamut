import subprocess
import sys


def test_cli_many():
    out = subprocess.run([sys.executable, "-m", "cf_subprocess.cli", "a", "b"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "many"


def test_cli_one():
    out = subprocess.run([sys.executable, "-m", "cf_subprocess.cli", "a"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "one"
