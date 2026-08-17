"""Mutation fuzzer for syntax-oracle.

Takes real, valid Python files, breaks each one with a single delimiter
mutation (delete an opener / delete a closer / insert a surplus closer /
swap a delimiter for the wrong kind), asks *this* interpreter for the real
SyntaxError, feeds message + source to ``diagnose()``, and grades:

  correct  — the finding points at the mutated position, OR one mechanical
             fix derived from the finding makes the file compile again
  silent   — no finding (safe; counted so we can see coverage)
  WRONG    — a finding whose derived fixes all fail to compile → printed

Also runs a "line-only" pass (what the plugin sees when it can only read the
echoed traceback line) — same grading, silence expected more often.

Usage: python fuzz_mutations.py [--files N] [--per-file K] [--seed S] [--corpus DIR ...] [-v]
Exit code 1 if any WRONG.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import pathlib
import random
import sys
import time
import tokenize

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("syntax_oracle", HERE.parent / "__init__.py")
so = importlib.util.module_from_spec(spec)
spec.loader.exec_module(so)

OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = {v: k for k, v in OPEN.items()}


def line_starts(src: str):
    starts = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def to_off(starts, line, col):
    return starts[line - 1] + col


def to_lc(starts, off):
    # binary-ish search
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= off:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, off - starts[lo]


def bracket_pairs(src: str):
    """[(opener_off, closer_off, char)] for a valid file, plus all token boundaries."""
    starts = line_starts(src)
    stack, pairs, boundaries = [], [], []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.OP:
            off = to_off(starts, *tok.start)
            if tok.string in OPEN:
                stack.append((tok.string, off))
            elif tok.string in CLOSE and stack and OPEN[stack[-1][0]] == tok.string:
                o, ooff = stack.pop()
                pairs.append((ooff, off, o))
        if tok.type in (tokenize.OP, tokenize.NAME, tokenize.NUMBER, tokenize.STRING):
            boundaries.append(to_off(starts, *tok.end))
    return pairs, boundaries


def compiles(src: str) -> bool:
    try:
        compile(src, "<fuzz>", "exec")
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False


def syntax_error(src: str):
    try:
        compile(src, "<fuzz>", "exec")
    except SyntaxError as e:
        return e
    except (ValueError, RecursionError, MemoryError):
        return None
    return None


def insert(src, off, s):
    return src[:off] + s + src[off:]


def delete(src, off):
    return src[:off] + src[off + 1:]


def fixes_from_finding(f, src):
    """Yield candidate one-edit fixed sources derived mechanically from a finding."""
    starts = line_starts(src)
    lines = src.split("\n")
    k = f["kind"]
    if k == "mismatch":
        cl, cc = f["closer_pos"]
        ol, oc = f["opener_pos"]
        coff, ooff = to_off(starts, cl, cc), to_off(starts, ol, oc)
        yield insert(src, coff, OPEN[f["opener"]])      # add the missing closer
        yield delete(src, ooff)                          # or delete the surplus opener
        yield delete(src, coff)                          # or the closer was the surplus one
        # or the closer is the wrong kind
        yield src[:coff] + OPEN[f["opener"]] + src[coff + 1:]
    elif k == "never_closed":
        ol, oc = f["opener_pos"]
        c = OPEN[f["opener"]]
        # end of opener line, then each following line end, then EOF
        for ln in range(ol, min(len(lines), ol + 60) + 1):
            body = lines[ln - 1]
            # insert before a trailing comment if any (best effort), else at end
            cut = len(body.rstrip())
            off = starts[ln - 1] + cut
            yield insert(src, off, c)
        yield src + c
        # or delete the opener itself
        yield delete(src, to_off(starts, ol, oc))
    elif k == "extra":
        cl, cc = f["closer_pos"]
        coff = to_off(starts, cl, cc)
        yield delete(src, coff)
        # or the opener kind was wrong / an opener is missing at line start — try
        # inserting matching opener at start of the same statement line (cheap guess)
        yield src[:coff] + "" + src[coff + 1:]
    elif k == "eof_open":
        yield src + "".join(OPEN[o] for o, _l, _c in reversed(f["open"]))


def mutate(src, pairs, boundaries, rng):
    """Return (mutated_src, kind, truth_off) — truth_off is the position the
    oracle should point at (in mutated coordinates), or None if ambiguous."""
    kind = rng.choice(["del_closer", "del_opener", "ins_closer", "swap_closer", "swap_opener"])
    if kind in ("del_closer", "del_opener", "swap_closer", "swap_opener") and not pairs:
        kind = "ins_closer"
    if kind == "del_closer":
        ooff, coff, o = rng.choice(pairs)
        return delete(src, coff), kind, ooff            # expect: opener at ooff flagged
    if kind == "del_opener":
        ooff, coff, o = rng.choice(pairs)
        return delete(src, ooff), kind, coff - 1        # expect: closer (shifted) flagged
    if kind == "swap_closer":
        ooff, coff, o = rng.choice(pairs)
        wrong = rng.choice([c for c in CLOSE if c != OPEN[o]])
        return src[:coff] + wrong + src[coff + 1:], kind, coff
    if kind == "swap_opener":
        ooff, coff, o = rng.choice(pairs)
        wrong = rng.choice([x for x in OPEN if x != o])
        return src[:ooff] + wrong + src[ooff + 1:], kind, ooff
    # ins_closer
    off = rng.choice(boundaries) if boundaries else len(src)
    ch = rng.choice(list(CLOSE))
    return insert(src, off, ch), kind, off


def grade(mut, finding, truth_off, whole_file=True):
    """Return 'silent' | 'exact' | 'fix-exact' | 'fix-verified' | 'unverified-ok' | 'WRONG'.

    exact        — the finding itself points at the mutated character
    fix-exact    — the plugin's compile-verified fix edits the mutated position
    fix-verified — the plugin's verified fix compiles but at another position (plausible alt)
    unverified-ok— no verified fix, but one of the grader's own derived edits compiles
    WRONG        — finding, no verified fix, none of the derived edits compile
    """
    if finding is None:
        return "silent"
    starts = line_starts(mut)
    fix = so.repair(finding, mut) if whole_file else None
    if fix is not None:
        l, c = fix["pos"]
        if fix["action"] in ("insert", "delete", "replace") and to_off(starts, l, c) in (truth_off, truth_off + 1, truth_off - 1):
            return "fix-exact"
    pos_key = {"mismatch": ("closer_pos", "opener_pos"), "never_closed": ("opener_pos",),
               "extra": ("closer_pos",), "eof_open": ()}
    # exact-position credit
    for key in pos_key.get(finding["kind"], ()):
        l, c = finding[key]
        if to_off(starts, l, c) == truth_off:
            return "exact"
    if finding["kind"] == "eof_open":
        for o, l, c in finding["open"]:
            if to_off(starts, l, c) == truth_off:
                return "exact"
    if fix is not None:
        return "fix-verified"
    # semantic credit: any derived one-edit fix compiles
    for fixed in fixes_from_finding(finding, mut):
        if compiles(fixed):
            return "unverified-ok"
    return "WRONG"


def echoed_line_mode(mut, e):
    """What the plugin sees when only the traceback's echoed line is available."""
    line = mut.split("\n")[e.lineno - 1] if e.lineno else ""
    return line.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=60)
    ap.add_argument("--per-file", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--corpus", nargs="*", default=None)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--dump", default=None, help="dir to dump WRONG mutated sources")
    a = ap.parse_args()
    rng = random.Random(a.seed)

    import sysconfig
    corpus_dirs = [pathlib.Path(p) for p in a.corpus] if a.corpus else [
        pathlib.Path(sysconfig.get_paths()["stdlib"]),   # stdlib of *this* interpreter (venv-safe)
    ]
    files = []
    for d in corpus_dirs:
        files += [p for p in d.rglob("*.py") if 1500 < p.stat().st_size < 60000 and "site-packages" not in str(p)]
    rng.shuffle(files)
    files = files[: a.files]

    tally = {"whole": {}, "line": {}}
    timings = []
    wrong = []
    n = 0
    for path in files:
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if not compiles(src) or "\t" in src:
            continue
        try:
            pairs, boundaries = bracket_pairs(src)
        except Exception:
            continue
        for _ in range(a.per_file):
            mut, kind, truth = mutate(src, pairs, boundaries, rng)
            e = syntax_error(mut)
            if e is None or not e.lineno:
                continue
            n += 1
            col = (e.offset - 1) if e.offset else None
            t0 = time.perf_counter()
            f = so.diagnose(mut, e.msg, e.lineno, True, col)
            if f:
                so.repair(f, mut)
            dt = (time.perf_counter() - t0) * 1000
            timings.append((dt, len(mut), path.name))
            if f and f.get("requires_fix") and so.repair(f, mut) is None:
                f = None  # the plugin drops these; grade as silent
            g = grade(mut, f, truth)
            tally["whole"][g] = tally["whole"].get(g, 0) + 1
            if g == "WRONG":
                wrong.append((path.name, kind, e.msg, e.lineno, f, mut.split("\n")[e.lineno - 1][:160]))
                if a.dump:
                    pathlib.Path(a.dump).mkdir(parents=True, exist_ok=True)
                    (pathlib.Path(a.dump) / f"wrong_{len(wrong)}_{path.name}").write_text(mut)
            if g == "silent" and a.v:
                print("  silent:", path.name, kind, repr(e.msg), "L", e.lineno, "|", mut.split("\n")[e.lineno - 1].strip()[:100])
            # line-only pass
            line = echoed_line_mode(mut, e)
            lstrip0 = len(mut.split("\n")[e.lineno - 1]) - len(mut.split("\n")[e.lineno - 1].lstrip())
            col2 = (e.offset - 1 - lstrip0) if e.offset else None
            f2 = so.diagnose(line, e.msg, e.lineno, False, col2)
            if f2 and f2.get("requires_fix"):
                f2 = None
            if f2 is None:
                g2 = "silent"
            else:
                # Map line-only finding back into the file to grade it.
                lstrip = len(mut.split("\n")[e.lineno - 1]) - len(mut.split("\n")[e.lineno - 1].lstrip())
                f3 = dict(f2)
                for key in ("closer_pos", "opener_pos"):
                    if key in f3:
                        l, c = f3[key]
                        f3[key] = (e.lineno, c + lstrip)
                if "open" in f3:
                    f3["open"] = [(o, e.lineno, c + lstrip) for o, l, c in f3["open"]]
                if "also_open" in f3:
                    f3["also_open"] = []
                g2 = grade(mut, f3, truth, whole_file=True)
                if g2 == "WRONG":
                    wrong.append((path.name, kind + "[line-only]", e.msg, e.lineno, f2, line[:160]))
            tally["line"][g2] = tally["line"].get(g2, 0) + 1

    print(f"python {sys.version.split()[0]}  mutations={n}")
    if timings:
        timings.sort(reverse=True)
        mean = sum(t for t, _, _ in timings) / len(timings)
        print(f"  diagnose+repair ms: mean={mean:.1f}  p95={timings[len(timings)//20][0]:.0f}  max={timings[0][0]:.0f} ({timings[0][2]}, {timings[0][1]//1024} KiB)")
    for mode in ("whole", "line"):
        t = tally[mode]
        tot = sum(t.values()) or 1
        print(f"  {mode:5s}: " + "  ".join(f"{k}={v} ({100*v/tot:.0f}%)" for k, v in sorted(t.items())))
    if wrong:
        print(f"\nWRONG ({len(wrong)}):")
        for w in wrong[:25]:
            print("  ", w)
    if n == 0:
        print("no mutations ran — corpus empty?")
        return 2
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
