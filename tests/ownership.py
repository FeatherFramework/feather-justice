"""Repository-ownership test (master plan: Testing standard, Persistence).

1. No resource outside feather-justice references a justice_ table.
2. Inside feather-justice, SQL touching justice_ tables appears only in the
   repository (server/repository.lua) and the schema (server/schema.lua).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT.parents[1]
NAMES = sorted(set(re.findall(r'CREATE TABLE IF NOT EXISTS (justice_[a-z_]+)', (ROOT / 'server' / 'schema.lua').read_text())))
TABLE = re.compile(r'\b(' + '|'.join(NAMES) + r')\b')
SQL = re.compile(r'\b(SELECT|INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|CREATE|TRUNCATE|FROM|JOIN)\b[^\n]*\b(' + '|'.join(NAMES) + r')\b', re.I)
ALLOWED = {ROOT / 'server' / 'repository.lua', ROOT / 'server' / 'schema.lua'}
failures = []

for path in RESOURCES.rglob('*'):
    if path.suffix not in ('.lua', '.sql', '.js', '.ts', '.py') or not path.is_file():
        continue
    if ROOT in path.parents or '.git' in path.parts or 'node_modules' in path.parts:
        continue
    try:
        text = path.read_text(errors='ignore')
    except OSError:
        continue
    for name in sorted(set(TABLE.findall(text))):
        failures.append('%s references %s' % (path.relative_to(RESOURCES), name))

for path in (ROOT / 'server').rglob('*.lua'):
    if path in ALLOWED:
        continue
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if SQL.search(line):
            failures.append('%s:%d SQL outside the repository: %s' % (path.relative_to(ROOT), number, line.strip()[:90]))

for failure in failures:
    print('FAIL ownership', failure)
print('%s repository ownership: %d tables, %d violation(s)' % ('PASS' if not failures else 'FAIL', len(NAMES), len(failures)))
sys.exit(1 if failures else 0)
