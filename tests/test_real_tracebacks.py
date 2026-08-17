"""End-to-end: run real broken snippets through *this* interpreter as a
subprocess, wrap the genuine stderr the way Hermes tools do, and check the
plugin's full build_verdict() path (traceback regex, caret parsing, source
recovery, repair) on every supported Python.

Run:  python tests/test_real_tracebacks.py [-v]
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("syntax_oracle", HERE.parent / "__init__.py")
so = importlib.util.module_from_spec(spec)
spec.loader.exec_module(so)

V = "-v" in sys.argv
CASES = [
    # name, source, expected substring in verdict (None → must be silent), expect verified fix
    ("missing } (Silas line 38)",
     'import json\noid = "x"\nwhere_note = json.dumps([{"parentType": {"eq": "Opportunity"}, {"parentId": {"eq": oid}}])\nprint(where_note)\n',
     "`{` opened at col 26 is never closed", True),
    ("missing ) (Silas print)",
     'label="a"\nnote={"k":1}\nk="k"\nprint("(%s) %s" % (label, str(note[k])[:500])\nprint(2)\n',
     "`(` opened at col 6 has no matching `)`", True),
    ("surplus )", 'x = (1 + 2))\n', "`)` at col 12 has no opener", True),
    ("wrong closer kind", 'd = {"a": [1, 2}\n', "`[` opened at col 11 is never closed", True),
    ("multi-line missing ) in call",
     'def f(a, b):\n    return a + b\n\nx = f(\n    1,\n    2\ny = 3\n', "never closed", True),
    ("big dict missing inner }",
     "D = {\n" + "".join(f'    "k{i}": {{"a": {i}, "b": [{i}]}},\n' for i in range(40)) +
     '    "k40": {"a": 40, "b": [40],\n' + "".join(f'    "k{i}": {{"a": {i}, "b": [{i}]}},\n' for i in range(41, 80)) + "}\n",
     "Verified one-edit fix", True),
    ("unexpected indent (real)", 'x = 1\n    y = 2\n', "indented 4 chars but the previous code line", True),
    ("unexpected indent caused by early )",
     'x = foo(1,\n        2)\n        3)\n', "bracket problem on the line(s) above", True),
    ("tab/space mix", 'if True:\n    x = 1\n\ty = 2\n', "mix of TAB", True),
    ("unindent mismatch", 'if True:\n    x = 1\n  y = 2\n', "enclosing blocks only use levels", True),
    ("missing ) before :", 'x = {}\nif hasattr(x, "y":\n    pass\n', "still open", True),
    ("f-string single }", 'a = 1\nx = f"head} {a}"\n', "inside an f-string has no opening", None),  # 3.12+ only
    ("f-string backslash", 'a = ["x"]\nx = f"{\'\\n\'.join(a)}"\n', "may not contain a backslash", None),  # <3.12 only
    ("clean file", 'x = [1, 2]\nprint(x)\n', None, False),
]


def run_file(src: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=tempfile.gettempdir()) as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=30)
        return path, p.stderr
    finally:
        pass  # keep the file: the plugin reads it from the traceback path


def main() -> int:
    fails = 0
    ver = sys.version_info[:2]
    for name, src, expect, want_fix in CASES:
        path, stderr = run_file(src)
        # 1) terminal-shaped result, file readable → whole-file mode
        result = json.dumps({"output": stderr, "exit_code": 1, "error": None})
        v = so.build_verdict("terminal", {"command": f"python3 {path}"}, result)
        # 2) execute_code-shaped (source in args, sandbox path gone)
        stderr2 = stderr.replace(path, "/tmp/hermes_sandbox_gone/script.py")
        result2 = json.dumps({"status": "error", "output": "\n--- stderr ---\n" + stderr2, "exit_code": 1})
        v2 = so.build_verdict("execute_code", {"code": src}, result2)
        # 3) write_file lint shape
        try:
            compile(src, "<x>", "exec")
            lint = {"status": "ok", "output": ""}
        except SyntaxError as e:
            lint = {"status": "error", "output": f"{type(e).__name__}: {e.msg} (line {e.lineno}, column {e.offset})"}
        v3 = so.build_verdict("write_file", {"path": path, "content": src}, json.dumps({"bytes_written": 1, "lint": lint}))
        os.unlink(path)

        # version-conditional expectations
        exp = expect
        if name.startswith("f-string single") and ver < (3, 12):
            exp = None if not stderr.strip() else "no opening"  # 3.10/3.11 message differs → static hint may not fire; accept silent
        if name.startswith("f-string backslash") and ver >= (3, 12):
            exp = None  # valid on 3.12+

        for label, verdict, whole in (("terminal", v, True), ("execute_code", v2, True), ("write_file", v3, True)):
            ok = (verdict is None) if exp is None else (verdict is not None and exp in verdict)
            if ok and exp is not None and want_fix and "Verified one-edit fix" not in verdict:
                ok = False
            status = "PASS" if ok else "FAIL"
            if not ok:
                fails += 1
            if V or not ok:
                print(f"{status} [{sys.version.split()[0]}] {name} / {label}")
                if not ok or V:
                    print("   stderr:", stderr.strip().replace("\n", "\n           ")[:600])
                    print("   verdict:", (verdict or "None").replace("\n", "\n            ")[:1200])
        if not V:
            print(f"PASS [{sys.version.split()[0]}] {name}" if fails == 0 else "", end="\n" if fails == 0 else "")
    print(f"\npython {sys.version.split()[0]}: failures={fails}")
    return 1 if fails else 0


def test_real_tracebacks():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
