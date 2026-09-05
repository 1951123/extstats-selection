"""Find all PG benchmark column references that are Oracle-reserved words, so we
know the full column-rename mapping needed for stats_CEB on Oracle (the 34 badges
Date failures are the known first hit; we want any others too)."""
import re
import sys
from collections import Counter

sys.path.insert(0, "src")
from extstats2.bench import load_benchmark

# conservative common Oracle reserved words that PG tables might use as bare names
RES = {"date", "level", "commit", "group", "order", "number", "session", "user",
       "range", "rank", "value", "size", "type", "interval", "current", "rows",
       "link", "length", "time", "uid", "name", "comment", "default"}
Q = load_benchmark("stats_ceb_single")
used = Counter()       # (table, col) -> count
bare = Counter()       # bare reserved col in WHERE
for q in Q:
    sql = q.sql
    fm = re.search(r"\bfrom\s+(\w+)\s+(?:as\s+)?(\w*)?", sql, re.I)
    t = fm.group(1).lower() if fm else "?"
    # qualified col refs
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)", sql):
        used[(m.group(1).lower(), m.group(2).lower())] += 1
    # bare reserved between WHERE/AND and operator
    for m in re.finditer(r"\b(?:WHERE|AND)\s+(date|level|group|order|number|user|range|size|type)\b", sql, re.I):
        bare[(m.group(1).lower(), t)] += 1

print("distinct qualified (table,col) WHERE colname is reserved:")
for (t, c), n in sorted(used.items()):
    if c in RES:
        print(f"  {t}.{c}  x{n}")
print("bare reserved in WHERE:", dict(bare))
