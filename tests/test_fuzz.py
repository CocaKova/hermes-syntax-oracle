"""Small deterministic slice of the mutation fuzzer for CI. For the full run:
    python tests/fuzz_mutations.py --files 80 --per-file 8 --seed 7
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent


def test_fuzz_small():
    p = subprocess.run([sys.executable, str(HERE / "fuzz_mutations.py"), "--files", "25", "--per-file", "6", "--seed", "1"],
                       capture_output=True, text=True, timeout=600)
    print(p.stdout)
    assert p.returncode == 0, p.stdout + p.stderr
