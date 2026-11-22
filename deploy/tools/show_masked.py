"""Print the lines of a source file that hold a scan hit, with every matched
span masked (never the matched text), plus the enclosing def/class.

    .venv/bin/python deploy/tools/show_masked.py FILE [--context N]
"""
import ast, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_public as sp

path = sys.argv[1]
ctx = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--context" else 0
text = open(path, encoding="utf-8").read()
lines = text.splitlines()
hits = [h for h in sp.find(text) if h[0] in sp.GATES]
masked = list(text)
for cat, s, e in hits:
    for i in range(s, e):
        if masked[i] != "\n":
            masked[i] = "#"
mtext = "".join(masked).splitlines()
# enclosing scopes
tree = ast.parse(text)
scopes = []
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        scopes.append((node.lineno, node.end_lineno, node.name))
def scope_of(ln):
    best = None
    for a, b, n in scopes:
        if a <= ln <= b and (best is None or a >= best[0]):
            best = (a, b, n)
    return best[2] if best else "<module>"
shown = set()
for cat, s, e in sorted(hits, key=lambda h: h[1]):
    ln = text.count("\n", 0, s) + 1
    if ln in shown:
        continue
    shown.add(ln)
    sc = scope_of(ln)
    for h in sp.find(sc):
        if h[0] in sp.GATES:
            sc = sc[:h[1]] + "#" * (h[2] - h[1]) + sc[h[2]:]
    print("---- line %d [%s] in %s" % (ln, cat, sc))
    for k in range(max(1, ln - ctx), min(len(lines), ln + ctx) + 1):
        print("%6d%s %s" % (k, ">" if k == ln else " ", mtext[k - 1][:200]))
