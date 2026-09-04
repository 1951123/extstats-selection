"""Inventory the BayesCard DMV workload DSL (non-SQL) to plan a translator.

Reads benchmarks/DMV/queries/query.sql (one query per line: SQL || ground_truth).
Reports: #queries, predicate-forms per column, distinct value tokens, whether
values are quoted, list syntax variants, trailing-comma lists, == N flags.
Also reports whether any column/table needs camcelCase->snake mapping by listing
raw column identifiers.
"""
import re
from collections import Counter, defaultdict

SRC = "benchmarks/DMV/queries/query.sql"
lines = [l for l in open(SRC) if l.strip() and not l.startswith("--")]
print("query lines:", len(lines))

forms = Counter()          # (col, 'eq'|'in')
col_in = defaultdict(int)
eq_flags = Counter()
no_truth = 0
tokens = Counter()
quoted = Counter()
trailing_comma = 0
samples_in = []

for ln in lines:
    if "||" not in ln:
        no_truth += 1
        continue
    body = ln.rpartition("||")[0]
    # find WHERE clause
    m = re.search(r"\bWHERE\b(.*)$", body, re.I)
    if not m:
        continue
    w = m.group(1)
    # equality:  Col == value  (and "=")
    for mm in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*==\s*([A-Za-z0-9_-]+)", w):
        forms[(mm.group(1), "eq")] += 1
        eq_flags[mm.group(2)] += 1
    # IN lists:  Col IN [ ... ]
    for mm in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*IN\s*\[([^\]]*)\]", w):
        col = mm.group(1)
        forms[(col, "in")] += 1
        col_in[col] += 1
        vals = mm.group(2)
        if vals.strip().endswith(","):
            trailing_comma += 1
        for t in vals.split(","):
            t = t.strip()
            if not t:
                continue
            tokens[t] += 1
            quoted["'" if "'" in t else ("\"" if "\"" in t else "plain")] += 1
        if col not in {c for c, _ in samples_in}:
            samples_in.append((col, mm.group(2)[:40] + ("..." if len(mm.group(2)) > 40 else "")))

print("lines without || truth:", no_truth)
print("predicate forms by col (col / eq-count / in-count):", dict(forms))
print("\nequality-flag tokens (e.g. N/Y values):", eq_flags.most_common(6))
print("distinct value tokens total:", len(tokens))
print("value token quoting counts:", dict(quoted))
print("IN lists with trailing comma:", trailing_comma)
print("\nsample column->list (first occurrence):")
for c, s in samples_in[:12]:
    print(f"   {c}: IN [{s}]")
print("\nmost common value forms sample:", [f"{t!r}:{n}" for t, n in tokens.most_common(10)])
