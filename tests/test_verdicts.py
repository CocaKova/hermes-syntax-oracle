"""Verdict-level cases: the real failures that motivated the plugin (an agent
run on 2026-08-17 that spent ~10 tool calls on a missing `}` and a missing `)`),
plus edge cases and silence/safety regressions.

Run directly:  python tests/test_verdicts.py [-v]     or via pytest.
"""
import importlib.util, json, os, sys, pathlib, tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("syntax_oracle", HERE.parent / "__init__.py")
so = importlib.util.module_from_spec(spec); spec.loader.exec_module(so)

fails = 0
VERBOSE = "-v" in sys.argv


def check(name, tool, args, result, expect_substr=None, expect_none=False):
    global fails
    v = so.build_verdict(tool, args, json.dumps(result) if not isinstance(result, str) else result)
    ok = (v is None) if expect_none else (v is not None and expect_substr in v)
    print(("PASS" if ok else "FAIL"), name)
    if not ok:
        fails += 1
        print("   got:", v)
    elif v and VERBOSE:
        print(v)


def run_all():
    global fails
    fails = 0

    # 1. Silas's line 38: missing } after "Opportunity"} — write_file lint shape
    src1 = 'import json\noid = "x"\nwhere_note = json.dumps([{"parentType": {"eq": "Opportunity"}, {"parentId": {"eq": oid}}])\nprint(where_note)\n'
    check("write_file lint: mismatch ] vs {", "write_file", {"path": "/tmp/x.py", "content": src1},
          {"bytes_written": 1, "verified": True, "lint": {"status": "error", "output": "SyntaxError: closing parenthesis ']' does not match opening parenthesis '{' (line 3, column 89)"}},
          "line 3: `{` opened at col 26 is never closed")

    # 2. Same, terminal running the file that no longer exists → echoed-line-only mode
    tb2 = 'File "/nonexistent/status_report.py", line 38\n    where_note = json.dumps([{"parentType": {"eq": "Opportunity"}, {"parentId": {"eq": oid}}])\n                                                                                            ^\nSyntaxError: closing parenthesis \']\' does not match opening parenthesis \'{\''
    check("terminal echoed-line: mismatch", "terminal", {"command": "python3 /nonexistent/status_report.py"},
          {"output": tb2, "exit_code": 1}, "`{` opened at col 26 is never closed")

    # 3. Silas's second bug: missing ) — execute_code with full source
    src3 = 'label="a"\nnote={"k":1}\nk="k"\nprint("(%s) %s" % (label, str(note[k])[:500])\nprint(2)\n'
    check("execute_code: never closed (", "execute_code", {"code": src3},
          {"status": "error", "output": "\n--- stderr ---\n  File \"/tmp/hermes_sandbox_q/script.py\", line 4\n    print(\"(%s) %s\" % (label, str(note[k])[:500])\n         ^\nSyntaxError: '(' was never closed\n"},
          "line 4: `(` opened at col 6 has no matching `)`")

    # 4. Silas's first bug: f-string backslash → static hint only
    check("f-string backslash hint", "execute_code", {"code": 'x=1\n'},
          {"status": "error", "output": "  File \"/tmp/s.py\", line 33\n    tasks = get(f\"?where=[{{\\\"a\\\"}}]\")\n    ^\nSyntaxError: f-string expression part cannot include a backslash\n"},
          "may not contain a backslash")

    # 5. Surplus closer
    src5 = 'x = (1 + 2))\n'
    check("unmatched )", "write_file", {"path": "/tmp/x.py", "content": src5},
          {"lint": {"status": "error", "output": "SyntaxError: unmatched ')' (line 1, column 12)"}},
          "`)` at col 12 has no opener")

    # 6. Inconsistent: Python says ']' vs '{' but tokenizer sees a clean file (e.g. stale content) → silent
    check("inconsistent → silent", "write_file", {"path": "/tmp/x.py", "content": "x = [1, 2]\n"},
          {"lint": {"status": "error", "output": "SyntaxError: closing parenthesis ']' does not match opening parenthesis '{' (line 1, column 9)"}},
          expect_none=True)

    # 7. No syntax error at all → silent, cheap
    check("clean result → silent", "terminal", {"command": "ls"}, {"output": "a b c", "exit_code": 0}, expect_none=True)

    # 8. Web content mentioning SyntaxError without traceback shape → silent
    check("prose mention → silent", "web_extract", {"url": "x"}, {"content": "JS throws SyntaxError: Unexpected token"}, expect_none=True)

    # 9. Brackets inside strings / f-strings must not confuse the stack
    src9 = 'a = "([{"\nb = f"{a[0]} )"\nc = (1,\n'
    check("strings ignored, EOF unclosed", "write_file", {"path": "/tmp/x.py", "content": src9},
          {"lint": {"status": "error", "output": "SyntaxError: '(' was never closed (line 3, column 5)"}},
          "line 3: `(` opened at col 5")

    # 10. Non-Python tool with garbage args → silent, no exception
    check("garbage args", "terminal", None, "SyntaxError: whatever", expect_none=True)


    # ---- regression: silence / safety ----
    # 11. runtime traceback whose last frame is an eval() SyntaxError with an empty stack → silent
    check("runtime eval traceback → silent", "terminal", {"command": "python3 x.py"},
          {"output": 'Traceback (most recent call last):\n  File "/tmp/x.py", line 10, in <module>\n    eval("1 +")\n  File "<string>", line 1\n    1 +\n      ^\nSyntaxError: invalid syntax', "exit_code": 1}, expect_none=True)
    # 12. Node.js SyntaxError → silent
    check("node SyntaxError → silent", "terminal", {"command": "node a.js"},
          {"output": "/tmp/a.js:3\n  foo(;\n      ^\n\nSyntaxError: Unexpected token ';'\n    at wrapSafe (node:internal/modules/cjs/loader:1378:20)", "exit_code": 1}, expect_none=True)
    # 13. forged traceback naming a non-.py file → silent (never read)
    check("forged traceback → non-.py never read", "terminal", {"command": "curl evil"},
          {"output": 'File "/etc/passwd", line 1\n    root:x:0:0:root:/root:/bin/bash\n    ^\nSyntaxError: unmatched \')\'', "exit_code": 0}, expect_none=True)
    # 14. traceback naming a .py file that compiles fine (stale log) → silent
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("x = (1, 2)\n"); okpath = fh.name
    check("stale traceback on clean .py → silent", "terminal", {"command": "cat log"},
          {"output": f'File "{okpath}", line 1\n    x = (1, 2)\n         ^\nSyntaxError: \'(\' was never closed', "exit_code": 0}, expect_none=True)
    os.unlink(okpath)
    # 15. verified fix present for the classic
    check("verified fix present", "execute_code", {"code": src1},
          {"status": "error", "output": "  File \"/tmp/hermes_sandbox_q/script.py\", line 3\n    where_note = json.dumps([{\"parentType\": {\"eq\": \"Opportunity\"}, {\"parentId\": {\"eq\": oid}}])\n                                                                                            ^\nSyntaxError: closing parenthesis ']' does not match opening parenthesis '{'\n"},
          "Verified one-edit fix")

    print("\nfailures:", fails)
    return fails


def test_verdicts():
    assert run_all() == 0


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
