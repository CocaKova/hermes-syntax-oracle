# syntax-oracle

**A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that turns a Python `SyntaxError` in a tool result into a deterministic, compile-verified diagnosis — so small local models stop blaming the tools for their own bracket typos.**

Stdlib only. Observational (blocks nothing). Silent unless it is sure.

```
🔎 syntax-oracle — deterministic diagnosis from Python's tokenizer, not a guess:
  line 38: `{` opened at col 26 is never closed — the `]` at col 89 closes the outer bracket while that brace is still open.
      where_note = json.dumps([{"parentType": {"eq": "Opportunity"}, {"parentId": {"eq": oid}}])
                               ^ opened here                                                  ^ `]` arrives here, `{` still open
    Fix: add the missing `}` before the `]` (or delete a surplus `{`).
  ✔ Verified one-edit fix (the whole file compiles after this): insert `}` at line 38 col 89
      where_note = json.dumps([{"parentType": {"eq": "Opportunity"}, {"parentId": {"eq": oid}}▁])
                                                                                                ^ insert `}` here
  This is a defect in the source text as written — not tool escaping, not shell quoting, not a linter false positive,
  not a broken Python. Fix the delimiter at the marked position; do not re-run unchanged, hexdump the file, or
  rewrite the line into a different shape without fixing it.
```

## Why

Small local models are unreliable at counting brackets — and they cannot tell that they are unreliable at it.

The line above is real. A 27B agent wrote it, Python said `closing parenthesis ']' does not match opening parenthesis '{'`, and the model — having "counted" the braces three separate times in its reasoning and gotten two `}` after `"Opportunity"` every time — resolved the contradiction by blaming everything it couldn't see: the tool's bracket escaping, the linter ("false positive"), shell quoting, the Python build, at one point the machine's custom kernel. It hexdumped the file to look for hidden characters. Eight tool calls later it rewrote the line into a different shape without ever finding the missing `}`. Then it did the same thing again with a missing `)` on the next script, and announced that "the terminal layer is corrupting `(` inside double-quoted strings."

Prompting harder ("trust the linter") fights the model's own perception. The durable fix is to **hand the model the fact it cannot compute** — and to make that fact impossible to argue with by proving it: *this one edit makes the whole file compile*.

## What it does

It hooks Hermes's `transform_tool_result` seam (the same one upstream's `security-guidance` plugin uses). For every tool result it does a substring check for `SyntaxError` / `IndentationError` / `TabError`; on a miss it costs nothing. On a hit:

1. **Locate the error** — traceback shape (`File "…", line N` + caret + `SyntaxError: msg`) or Hermes's in-process lint shape (`SyntaxError: msg (line N, column M)`). Runtime frames (`, in <module>`) are skipped.
2. **Recover the source** — from the tool args (`write_file.content`, `execute_code.code`), from the file the traceback names (guarded, see *Safety*), or as a fallback from the single echoed line.
3. **Diagnose with the tokenizer** — replay the delimiter stack over the source: which `(`/`[`/`{` is still open, which closer arrived while something else was on top, which closer has no opener. Indentation errors get their own analysis (tab/space mix rendered visibly as `→`/`·`, dedent levels that don't exist, an "unexpected indent" that is really a continuation line whose bracket was closed too early).
4. **Check consistency** — the finding must agree with Python's own message and caret (same delimiter characters, same line, same column). If it doesn't, the plugin says nothing. A wrong diagnosis would be worse than none: it would become one more thing for the model to be confused by.
5. **Search for a repair** — try the handful of one-character edits the finding implies (insert the missing closer here / at the end of that line / where the indentation says the construct should have ended; delete the surplus one; the closer is the wrong kind; an opener fused two identifiers…), `compile()` each, and report the first that makes the **whole file** parse. Bounded (≤160 compiles, ≤1.5 s, files ≤200 KiB). Where the finding is only plausible rather than certain (generic `invalid syntax`, f-string brace errors), it is reported *only* if a verified fix exists.
6. **Append the verdict** to the tool result. The file is still written, the command still ran; the model just sees the answer in the next turn.

Static one-line hints cover a few messages that need no analysis but routinely derail small models (`f-string expression part cannot include a backslash`, unterminated strings, "Perhaps you forgot a comma?", `cannot assign to`, `invalid decimal literal`).

## Install

```bash
git clone https://github.com/CocaKova/hermes-syntax-oracle ~/.hermes/plugins/syntax-oracle
```

Enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - syntax-oracle
```

Then restart Hermes — the gateway **and** the dashboard/TUI server if you run them as services (`systemctl --user restart hermes-gateway hermes-dashboard`). Plugins load at process start.

Check: `hermes plugins list` should show `syntax-oracle  enabled`.

Off switch without uninstalling: `SYNTAX_ORACLE_DISABLE=1` in the environment.

Requires Python 3.10+ (tested on 3.10 – 3.14). No dependencies.

## Battle-tested

Correctness matters more than coverage here — a confidently wrong bracket-stack readout would be a new conspiracy for the model — so the test suite is built around ground truth:

* `tests/fuzz_mutations.py` takes real, valid Python files (the running interpreter's own stdlib), applies one random delimiter mutation (delete an opener / delete a closer / insert a surplus closer / swap a delimiter for the wrong kind), asks the **real interpreter** for the message, runs the plugin, and grades: does the finding point at the mutated character, or does the plugin's verified fix edit that exact position, or at least compile? Any finding whose derived edits all fail to compile is a `WRONG` and fails the run.
* `tests/test_real_tracebacks.py` runs broken snippets as real subprocesses and pushes the genuine stderr through the full `terminal` / `execute_code` / `write_file` result paths.
* `tests/test_verdicts.py` replays the incident that motivated the plugin, plus silence/safety regressions.

Typical fuzz run (560 mutations per interpreter, whole-file mode):

| Python | exact | fix-exact | fix-verified | silent | WRONG |
|--------|------:|----------:|-------------:|-------:|------:|
| 3.10   | 25–29% | 65–69% | 5–6% | ≤1% | 0 |
| 3.11   | 25–31% | 63–68% | 6–8% | 0% | 0 |
| 3.12   | 30–32% | 60–61% | 7–9% | ≤1% | 0 |
| 3.13   | 29–31% | 62–64% | 7–8% | ≤1% | 0 |
| 3.14   | 25–28% | 64–66% | 8–9% | ≤1% | 0 |

*exact* = the diagnosis itself points at the mutated character; *fix-exact* = the compile-verified fix edits the mutated position; *fix-verified* = a different one-edit fix that also compiles (e.g. deleting the partner instead of re-inserting); *silent* = no verdict (the safe failure). Across ~15,000 mutations during development the last remaining `WRONG` classes were fixed one by one; the fuzzer exits non-zero if any reappear.

Cost when it fires: mean ≈ 8 ms, p95 ≈ 30 ms, worst seen ≈ 300 ms on 50 KiB files; ≤150 ms on a 150 KiB file. When it doesn't fire (every normal tool call): one substring check.

Run everything: `python -m pytest tests` — or the scripts directly, e.g. `python tests/fuzz_mutations.py --files 80 --per-file 8 --seed 7 -v`.

## Safety

The plugin reads files named in tool output. Tool output can contain attacker-controlled text (a `curl`, a log with a web page in it), and a forged `File "…", line N … SyntaxError:` could name any readable file. Two limits keep this from becoming a way to echo arbitrary file lines into the model's context:

* only `.py` / `.pyw` / `.pyi` files under 512 KiB are ever read, and
* a file is only used if it genuinely fails to compile — a stale or forged traceback pointing at a clean file yields silence.

Verdicts quote at most a couple of lines of the offending source, which the model already had access to (it wrote or ran it).

## Limitations, honestly

* Python only. JSON/YAML/TOML lint errors from `write_file` are not diagnosed (yet — see roadmap).
* Line-only mode (traceback echo, source unavailable — e.g. `python -c` one-liners) is context-blind: it trusts Python's caret column and stays silent ~20% of the time where whole-file mode would speak.
* A compile-verified fix is *syntactically* right; the model still has to judge whether it matches intent. Candidate ordering favours the likely-intended edit (nearest position, indentation evidence, a known identifier split), but `foo(a, b))` can be fixed by deleting either `)`.
* Delimiters that vanish *inside* an identifier (`os.makedirs(self.x` → `os.makedirsself.x`) are found only when the resulting fused name has a prefix that exists elsewhere in the file, or the budget reaches the last-resort tier.
* If more than one thing is wrong, the verdict says so and diagnoses the first.

## Roadmap

* JSON / YAML / TOML: `write_file`'s in-process linters emit `JSONDecodeError: … (line N, column M)` — same seam, same idea (unclosed `{`, trailing comma, single quotes).
* Other languages' brace errors (JS/TS via `node --check`, shell `unexpected EOF while looking for matching`) where the tool result already carries a precise message.
* Upstream: this is a plain Hermes plugin; if it proves useful it may be proposed to `plugins/` in hermes-agent.

## How it relates to Hermes

* Uses `transform_tool_result` (observational, replace-by-returning-a-string). No source patches, no gateway changes, survives `hermes update`.
* That hook is first-string-wins across plugins: if another enabled plugin also rewrites the same result (e.g. `security-guidance` on a `write_file`), whichever loaded first wins on that one result. Rare overlap; harmless.
* Uses no Hermes internals — the whole plugin is `__init__.py` + `plugin.yaml`.

## License

MIT — see `LICENSE`.
