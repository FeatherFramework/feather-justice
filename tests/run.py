"""Justice test runner: real MariaDB, real transactions, no players.

Creates a throwaway schema, runs tests/harness.lua (Justice server code with Core,
Character, Contracts, Inventory and Organizations mocked at their export boundary)
and drops the schema again. Also syntax-checks every Lua file and runs the
repository-ownership test.

    python3 tests/run.py --server-cfg ../../../server.cfg
    JUSTICE_DB_URL=mysql://user:pass@localhost/db python3 tests/run.py

The scratch schema is named justice_test_<pid> and is always dropped.
"""
import argparse
import ctypes
import ctypes.util
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument('--server-cfg', type=Path, help='server.cfg holding mysql_connection_string')
parser.add_argument('--suite', default='', help='run only suites whose file name contains this text')
parser.add_argument('--keep', action='store_true', help='do not drop the scratch schema (debugging)')
args = parser.parse_args()

# ---------------------------------------------------------------- connection
url = os.environ.get('JUSTICE_DB_URL')
if not url and args.server_cfg:
    match = re.search(r'mysql_connection_string\s+"([^"]+)"', args.server_cfg.read_text())
    url = match and match.group(1)
if not url:
    raise SystemExit('Set JUSTICE_DB_URL or pass --server-cfg.')
match = re.match(r'mysql://([^:]+):(.*)@([^/:]+)(?::(\d+))?/([^?]+)', url)
if not match:
    raise SystemExit('Unsupported connection string.')
user, password, host, port, _ = match.groups()
user, password = unquote(user), unquote(password)
schema = 'justice_test_%d' % os.getpid()

# ---------------------------------------------------------------- MariaDB client
mysql = ctypes.CDLL(ctypes.util.find_library('mariadb') or 'libmariadb.so.3')


class Field(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char_p), ('org_name', ctypes.c_char_p), ('table', ctypes.c_char_p),
                ('org_table', ctypes.c_char_p), ('db', ctypes.c_char_p), ('catalog', ctypes.c_char_p),
                ('def', ctypes.c_char_p), ('length', ctypes.c_ulong), ('max_length', ctypes.c_ulong),
                ('name_length', ctypes.c_uint), ('org_name_length', ctypes.c_uint), ('table_length', ctypes.c_uint),
                ('org_table_length', ctypes.c_uint), ('db_length', ctypes.c_uint), ('catalog_length', ctypes.c_uint),
                ('def_length', ctypes.c_uint), ('flags', ctypes.c_uint), ('decimals', ctypes.c_uint),
                ('charsetnr', ctypes.c_uint), ('type', ctypes.c_int)]


mysql.mysql_init.restype = ctypes.c_void_p
mysql.mysql_init.argtypes = [ctypes.c_void_p]
mysql.mysql_real_connect.restype = ctypes.c_void_p
mysql.mysql_real_connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                     ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_ulong]
mysql.mysql_real_query.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong]
mysql.mysql_store_result.restype = ctypes.c_void_p
mysql.mysql_store_result.argtypes = [ctypes.c_void_p]
mysql.mysql_field_count.argtypes = [ctypes.c_void_p]
mysql.mysql_num_fields.argtypes = [ctypes.c_void_p]
mysql.mysql_fetch_field_direct.restype = ctypes.POINTER(Field)
mysql.mysql_fetch_field_direct.argtypes = [ctypes.c_void_p, ctypes.c_uint]
mysql.mysql_fetch_row.restype = ctypes.POINTER(ctypes.c_void_p)
mysql.mysql_fetch_row.argtypes = [ctypes.c_void_p]
mysql.mysql_fetch_lengths.restype = ctypes.POINTER(ctypes.c_ulong)
mysql.mysql_fetch_lengths.argtypes = [ctypes.c_void_p]
mysql.mysql_free_result.argtypes = [ctypes.c_void_p]
mysql.mysql_error.restype = ctypes.c_char_p
mysql.mysql_error.argtypes = [ctypes.c_void_p]
mysql.mysql_errno.argtypes = [ctypes.c_void_p]
mysql.mysql_affected_rows.restype = ctypes.c_ulonglong
mysql.mysql_affected_rows.argtypes = [ctypes.c_void_p]
mysql.mysql_set_character_set.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
mysql.mysql_real_escape_string.restype = ctypes.c_ulong
mysql.mysql_real_escape_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong]
mysql.mysql_close.argtypes = [ctypes.c_void_p]
NUMERIC = {0, 1, 2, 3, 4, 5, 8, 9, 246}


class Connection:
    def __init__(self, database):
        self.handle = mysql.mysql_init(None)
        if not mysql.mysql_real_connect(self.handle, host.encode(), user.encode(), password.encode(),
                                        database.encode() if database else None, int(port or 3306), None, 0):
            raise SystemExit('MariaDB connection failed: ' + mysql.mysql_error(self.handle).decode())
        mysql.mysql_set_character_set(self.handle, b'utf8mb4')

    def escape(self, value):
        raw = value.encode()
        out = ctypes.create_string_buffer(len(raw) * 2 + 1)
        size = mysql.mysql_real_escape_string(self.handle, out, raw, len(raw))
        return out.raw[:size].decode()

    def literal(self, value):
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, (int, float)):
            return repr(int(value)) if float(value).is_integer() else repr(value)
        return "'" + self.escape(str(value)) + "'"

    def bind(self, sql, params):
        parts, index = sql.split('?'), 0
        if len(parts) - 1 != len(params):
            raise ValueError('placeholder count %d != %d parameters' % (len(parts) - 1, len(params)))
        out = [parts[0]]
        for part in parts[1:]:
            out.append(self.literal(params[index]))
            out.append(part)
            index += 1
        return ''.join(out)

    def run(self, sql):
        raw = sql.encode()
        if mysql.mysql_real_query(self.handle, raw, len(raw)) != 0:
            return {'ok': False, 'error': mysql.mysql_error(self.handle).decode(), 'errno': mysql.mysql_errno(self.handle)}
        result = mysql.mysql_store_result(self.handle)
        if not result:
            if mysql.mysql_field_count(self.handle) != 0:
                return {'ok': False, 'error': mysql.mysql_error(self.handle).decode()}
            return {'ok': True, 'rows': [], 'affected': mysql.mysql_affected_rows(self.handle)}
        count = mysql.mysql_num_fields(result)
        fields = [mysql.mysql_fetch_field_direct(result, i).contents for i in range(count)]
        names = [f.name.decode() for f in fields]
        rows = []
        while True:
            row = mysql.mysql_fetch_row(result)
            if not row:
                break
            lengths = mysql.mysql_fetch_lengths(result)
            item = {}
            for i in range(count):
                if row[i] is None:
                    continue
                text = ctypes.string_at(row[i], lengths[i]).decode()
                if fields[i].type in NUMERIC:
                    number = float(text)
                    item[names[i]] = int(number) if number.is_integer() and 'e' not in text.lower() else number
                else:
                    item[names[i]] = text
            rows.append(item)
        mysql.mysql_free_result(result)
        return {'ok': True, 'rows': rows}


# A timeout or Ctrl-C still reaches the finally block that drops the schema.
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
admin = Connection(None)
created = admin.run('CREATE DATABASE `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci' % schema)
if not created['ok']:
    raise SystemExit(created['error'])
connections = {}


def connection(number):
    if number not in connections:
        connections[number] = Connection(schema)
    return connections[number]


statements = {'count': 0}


def handle(request):
    try:
        conn = connection(int(request.get('conn', 1)))
        sql = request['sql'] if 'params' not in request else conn.bind(request['sql'], request['params'])
        statements['count'] += 1
        return conn.run(sql)
    except Exception as error:  # never raise across the Lua C boundary
        return {'ok': False, 'error': 'bridge: %s' % error}


# ---------------------------------------------------------------- Lua
lua = ctypes.CDLL(ctypes.util.find_library('lua5.4'))
lua.luaL_newstate.restype = ctypes.c_void_p
lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
lua.luaL_loadfilex.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
lua.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_void_p]
lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
lua.lua_tolstring.restype = ctypes.c_void_p
lua.lua_pushlstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
lua.lua_pushcclosure.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
lua.lua_setglobal.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lua.lua_pushstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lua.lua_close.argtypes = [ctypes.c_void_p]
CFUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


def bridge(state):
    size = ctypes.c_size_t()
    pointer = lua.lua_tolstring(state, 1, ctypes.byref(size))
    try:
        response = handle(json.loads(ctypes.string_at(pointer, size.value)))
    except Exception as error:
        response = {'ok': False, 'error': 'bridge: %s' % error}
    data = json.dumps(response).encode()
    lua.lua_pushlstring(state, data, len(data))
    return 1


callback = CFUNC(bridge)
os.chdir(ROOT)
failed = False
try:
    # Syntax check of every Lua file in the resource.
    for path in sorted(ROOT.rglob('*.lua')):
        state = lua.luaL_newstate()
        if lua.luaL_loadfilex(state, os.fsencode(path), None):
            size = ctypes.c_size_t()
            raise SystemExit(ctypes.string_at(lua.lua_tolstring(state, -1, ctypes.byref(size))).decode())
        lua.lua_close(state)
    # Repository ownership (master plan: Testing standard).
    owner = subprocess.run([sys.executable, '-I', str(ROOT / 'tests' / 'ownership.py')])
    failed = failed or owner.returncode != 0
    state = lua.luaL_newstate()
    lua.luaL_openlibs(state)
    lua.lua_pushcclosure(state, ctypes.cast(callback, ctypes.c_void_p), 0)
    lua.lua_setglobal(state, b'__db')
    lua.lua_pushstring(state, args.suite.encode())
    lua.lua_setglobal(state, b'__suite')
    error = lua.luaL_loadfilex(state, b'tests/harness.lua', None) or lua.lua_pcallk(state, 0, 0, 0, 0, None)
    if error:
        size = ctypes.c_size_t()
        print('FAIL', ctypes.string_at(lua.lua_tolstring(state, -1, ctypes.byref(size)), size.value).decode())
        failed = True
    lua.lua_close(state)
    print('SQL statements executed: %d (schema %s)' % (statements['count'], schema))
finally:
    for conn in connections.values():
        mysql.mysql_close(conn.handle)
    if not args.keep:
        admin.run('DROP DATABASE IF EXISTS `%s`' % schema)
    mysql.mysql_close(admin.handle)
sys.exit(1 if failed else 0)
