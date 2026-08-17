# Changelog

## 0.2.0 — 2026-08-17

* Compile-verified repair search: every diagnosis now tries the one-character edits it implies and reports the first that makes the whole file parse (bounded: ≤160 compiles, ≤1.5 s, ≤200 KiB).
* Candidate sources beyond the obvious: indentation anomalies (where a big literal's indent pops back while a bracket is still open), lines whose own bracket count is unbalanced, statement-end lines, fused identifiers with a known prefix, blank lines that used to hold a lone opener.
* Indentation errors: tab/space mix rendered visibly, dedent-level analysis, `unexpected indent` re-explained as a bracket problem when the verified fix is a bracket edit.
* Generic `invalid syntax` with a delimiter still open at the caret, and f-string brace errors, are diagnosed only when a verified fix exists.
* Column-aware consistency check against Python's caret; line-only mode trusts the caret and stays silent when it can't disambiguate.
* Safety: only `.py`/`.pyw`/`.pyi` files ≤512 KiB are read from traceback paths, and only if they genuinely fail to compile.
* Rendering: tab-safe carets, long-line windowing, stacked labels when marks are close.
* Tests: mutation fuzzer with ground truth, real-subprocess traceback tests, verdict regressions; CI matrix 3.10–3.14.

## 0.1.0 — 2026-08-17

* First version: tokenizer-based mismatch / never-closed / surplus-closer diagnosis appended to tool results; static hints for f-string backslash, unterminated strings, missing comma.
