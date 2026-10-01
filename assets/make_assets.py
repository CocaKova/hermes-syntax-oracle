#!/usr/bin/env python3
"""Regenerate the README header art (light + dark). Stdlib only.

The diagram follows __init__.py: build_verdict() -> find_error() -> source_for()
-> diagnose() (must agree with Python's message) -> repair() (compile() each
candidate edit) -> the verdict is appended to the tool result, or nothing is.
"""
import html
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))

THEMES = {
    "dark": dict(bg="#0d1117", panel="#161b22", line="#30363d", text="#e6edf3", dim="#8b949e",
                 ok="#3fb950", warn="#d29922", bad="#f85149", accent="#3fb950"),
    "light": dict(bg="#ffffff", panel="#f6f8fa", line="#d0d7de", text="#1f2328", dim="#656d76",
                  ok="#1a7f37", warn="#9a6700", bad="#cf222e", accent="#1a7f37"),
}
FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
ALT = ("hermes-syntax-oracle: a SyntaxError in a tool result, recover the source, tokenizer diagnosis that must "
       "agree with Python, compile-checked one-edit fix, then a verdict appended or silence")


def t(x, y, s, c, size=13, color="text", mono=False, weight=None, anchor=None):
    fam = MONO if mono else FONT
    extra = (f' font-weight="{weight}"' if weight else "") + (f' text-anchor="{anchor}"' if anchor else "")
    return (f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" fill="{c[color]}"{extra} '
            f'xml:space="preserve">{html.escape(s)}</text>')


def box(x, y, w, h, label, sub, c, stroke, mono=False):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{c["panel"]}" stroke="{stroke}" stroke-width="1.5"/>'
            + t(x + w / 2, y + 30, label, c, size=16 if mono else 17, mono=mono, weight=600 if not mono else 700, anchor="middle")
            + t(x + w / 2, y + 52, sub, c, size=12.5, color="dim", anchor="middle"))


def arrow(x1, y1, x2, y2, color):
    a = math.atan2(y2 - y1, x2 - x1)
    bx, by = x2 - 8 * math.cos(a), y2 - 8 * math.sin(a)
    px, py = 5 * math.sin(a), -5 * math.cos(a)
    return (f'<line x1="{x1}" y1="{y1}" x2="{bx:.1f}" y2="{by:.1f}" stroke="{color}" stroke-width="1.8"/>'
            f'<path d="M{bx + px:.1f},{by + py:.1f} L{x2},{y2} L{bx - px:.1f},{by - py:.1f} Z" fill="{color}"/>')


def hero(c):
    W, H = 1200, 330
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="{html.escape(ALT)}">',
         f'<rect width="{W}" height="{H}" rx="16" fill="{c["bg"]}" stroke="{c["line"]}"/>',
         t(60, 78, "hermes-syntax-oracle", c, size=38, mono=True, weight=700),
         t(60, 112, "Points at the bracket that broke a Python SyntaxError, and checks the fix with compile().", c,
           size=18, color="dim")]
    y, w, gap = 168, 170, 30
    steps = [("SyntaxError", "in a tool result", c["bad"], True),
             ("recover source", "tool args or the .py file", c["line"], False),
             ("tokenizer", "must agree with Python", c["line"], False),
             ("compile()", "up to 160 one-char edits", c["line"], True)]
    for i, (label, sub, stroke, mono) in enumerate(steps):
        x = 60 + i * (w + gap)
        s.append(box(x, y, w, 70, label, sub, c, stroke, mono))
        if i:
            s.append(arrow(x - gap, y + 35, x, y + 35, c["dim"]))
    end = 60 + 4 * w + 3 * gap  # 830
    # verdict branch
    s.append(arrow(end, y + 20, 888, y - 2, c["accent"]))
    s.append(f'<rect x="890" y="{y - 32}" width="250" height="60" rx="10" fill="none" stroke="{c["accent"]}" stroke-width="1.5"/>')
    s.append(t(912, y - 6, "✓ VERDICT APPENDED", c, size=16, color="accent", mono=True, weight=700))
    s.append(t(912, y + 14, "where, why, fix if one compiles", c, size=12.5, color="dim"))
    # silent branch
    s.append(arrow(end, y + 50, 888, y + 82, c["dim"]))
    s.append(f'<rect x="890" y="{y + 54}" width="250" height="60" rx="10" fill="none" stroke="{c["line"]}" stroke-width="1.5"/>')
    s.append(t(912, y + 80, "○ SILENT", c, size=16, color="dim", mono=True, weight=700))
    s.append(t(912, y + 100, "any doubt: result passes untouched", c, size=12.5, color="dim"))
    s.append(t(60, 276, "source: write_file content, execute_code code, a patched .py, or a .py named in the traceback "
               "(only if it really fails to compile)", c, size=12.5, color="dim"))
    s.append("</svg>")
    return "".join(s)


for theme, colors in THEMES.items():
    with open(os.path.join(HERE, f"hero-{theme}.svg"), "w", encoding="utf-8") as f:
        f.write(hero(colors))
print("ok")
