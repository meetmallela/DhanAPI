"""
SQLite-to-MySQL Transparent Adapter Bridge
Author: Antigravity (Google DeepMind Team)
Date: May 30, 2026

Description:
This module intercepts all calls to the standard 'sqlite3' module and transparently
redirects database operations for active trading databases ('trading.db' and 'dhan_dashboard.db')
to your local high-performance MySQL server, while leaving local caching databases
(like 'kite_candles.db' or 'backtest_cache.db') on standard SQLite.

It dynamically translates SQL query parameters (?, INSERT OR IGNORE, AUTOINCREMENT, etc.)
and provides a high-fidelity 'MySQLRow' drop-in replacement for 'sqlite3.Row'.
"""

import sqlite3
import mysql.connector
import re

# ---------------------------------------------------------------------------
# MySQL reserved words used as column names in the trading schema.
# 'signal'   → strategy_signals.signal, meta_decisions.signal
# 'strategy' → strategy_signals.strategy, meta_decisions.strategy
# These need backtick-quoting in every SQL statement that references them.
# ---------------------------------------------------------------------------
_RESERVED_COLS = ('signal', 'strategy')

def _quote_reserved_columns(query: str) -> str:
    """
    Backtick-quote MySQL reserved words that appear as column names.
    Splits on single-quoted string literals first so values like
    'NEUTRAL' or 'SIGNAL' inside WHERE clauses are left unchanged.
    """
    # Split into alternating: [non-string, 'string-literal', non-string, ...]
    parts = re.split(r"('(?:[^'\\]|\\.)*')", query)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:          # inside a single-quoted string — leave untouched
            result.append(part)
        else:
            for col in _RESERVED_COLS:
                # Replace bare word (not already backtick-quoted, not a function call)
                part = re.sub(
                    r'(?<!`)\b' + col + r'\b(?!\s*\()(?!`)',
                    f'`{col}`',
                    part,
                    flags=re.IGNORECASE,
                )
            result.append(part)
    return ''.join(result)


class MySQLRow:
    def __init__(self, data_tuple, description):
        self._tuple = data_tuple
        self._keys = [col[0] for col in description]
        self._dict = dict(zip(self._keys, data_tuple))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._tuple[key]
        return self._dict.get(key)

    def __len__(self):
        return len(self._tuple)

    def __iter__(self):
        return iter(self._tuple)

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        return self._dict.get(key, default)

class MySQLCursorWrapper:
    def __init__(self, mysql_cursor, row_factory=False):
        self.cursor = mysql_cursor
        self.row_factory = row_factory

    def execute(self, query, params=None):
        cleaned_query = self._translate(query)
        if params:
            if isinstance(params, list):
                params = tuple(params)
            self.cursor.execute(cleaned_query, params)
        else:
            self.cursor.execute(cleaned_query)
        return self

    def executemany(self, query, params_list):
        cleaned_query = self._translate(query)
        self.cursor.executemany(cleaned_query, params_list)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        if self.row_factory:
            return MySQLRow(row, self.cursor.description)
        return row

    def fetchall(self):
        rows = self.cursor.fetchall()
        if self.row_factory:
            return [MySQLRow(row, self.cursor.description) for row in rows]
        return rows

    def fetchmany(self, size=1000):
        rows = self.cursor.fetchmany(size)
        if self.row_factory:
            return [MySQLRow(row, self.cursor.description) for row in rows]
        return rows

    # ------------------------------------------------------------------ #
    # Text-type translation helper for CREATE TABLE statements
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fix_create_table_text_types(query: str) -> str:
        """
        Smart TEXT→MySQL type mapping inside a CREATE TABLE statement.

          TEXT with datetime('now') default  → DATETIME
          TEXT in UNIQUE KEY / PRIMARY KEY   → VARCHAR(191)  (indexable, utf8mb4-safe)
          TEXT with any other DEFAULT value  → VARCHAR(255)  (MySQL 5.7: TEXT can't have defaults)
          TEXT without any constraints       → VARCHAR(191)  (safe default: indexable, no prefix needed)

        Why VARCHAR(191) not LONGTEXT for unconstrained TEXT?
          Columns needing >191 chars (TG raw messages, KB context, parsed JSON) are
          already LONGTEXT in MySQL from the migration script.  Runtime CREATE TABLE
          IF NOT EXISTS statements hit existing tables → silently ignored.  New tables
          created after migration use VARCHAR(191) which is indexable without a prefix.
        """
        # Collect column names referenced in index/unique/pk constraints
        indexed_cols: set[str] = set()
        for m in re.finditer(
            r'(?:UNIQUE(?:\s+KEY\s+\w+)?|PRIMARY\s+KEY|KEY\s+\w+)\s*\(([^)]+)\)',
            query, re.IGNORECASE
        ):
            for col in m.group(1).split(','):
                indexed_cols.add(re.sub(r'[`\s()\d]', '', col).lower())

        def _replace_col(m: re.Match) -> str:
            chunk    = m.group(0)          # full col definition up to the next , or )
            col_name = re.sub(r'`', '', m.group(1)).lower()

            # 1. datetime default → DATETIME type
            if re.search(r"datetime\s*\(\s*'now'\s*\)|CURRENT_TIMESTAMP", chunk, re.IGNORECASE):
                return re.sub(r'\bTEXT\b', 'DATETIME', chunk, count=1, flags=re.IGNORECASE)

            # 2. In a UNIQUE / PRIMARY KEY index → VARCHAR(191)
            if col_name in indexed_cols:
                return re.sub(r'\bTEXT\b', 'VARCHAR(191)', chunk, count=1, flags=re.IGNORECASE)

            # 3. Has any DEFAULT clause → VARCHAR(255)
            #    (MySQL 5.7 forbids DEFAULT on TEXT/LONGTEXT/BLOB)
            if re.search(r'\bDEFAULT\b', chunk, re.IGNORECASE):
                return re.sub(r'\bTEXT\b', 'VARCHAR(255)', chunk, count=1, flags=re.IGNORECASE)

            # 4. Plain TEXT → VARCHAR(191) (indexable by default; LONGTEXT set by migration for known long-content cols)
            return re.sub(r'\bTEXT\b', 'VARCHAR(191)', chunk, count=1, flags=re.IGNORECASE)

        # Match col_name TEXT [NOT NULL] [DEFAULT ...] up to end-of-line or next comma.
        # Uses [^,\n]* (not [^,)]*) so parenthesised defaults like DEFAULT (datetime('now'))
        # are included in the match and picked up by the datetime check above.
        return re.sub(r'(`?\w+`?)\s+TEXT\b[^,\n]*', _replace_col, query, flags=re.IGNORECASE)

    # ------------------------------------------------------------------ #

    def _translate(self, query):
        if "PRAGMA journal_mode" in query or "PRAGMA synchronous" in query:
            return "SELECT 1"

        pragma_match = re.match(r"PRAGMA\s+table_info\s*\(\s*(\w+)\s*\)", query, re.IGNORECASE)
        if pragma_match:
            table_name = pragma_match.group(1)
            return f"""
                SELECT
                    ORDINAL_POSITION - 1 AS cid,
                    COLUMN_NAME AS name,
                    DATA_TYPE AS type,
                    IF(IS_NULLABLE = 'NO', 1, 0) AS notnull,
                    COLUMN_DEFAULT AS dflt_value,
                    IF(COLUMN_KEY = 'PRI', 1, 0) AS pk
                FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = '{table_name}'
                ORDER BY ORDINAL_POSITION;
            """

        query = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT IGNORE", query, flags=re.IGNORECASE)
        query = re.sub(r"AUTOINCREMENT", "AUTO_INCREMENT", query, flags=re.IGNORECASE)

        # MySQL <8.0.3 has no CREATE INDEX IF NOT EXISTS — strip the clause.
        # Duplicate-key / bad-prefix errors are silently ignored in execute() below.
        query = re.sub(r'\bCREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b',
                       lambda m: f'CREATE {m.group(1) or ""}INDEX',
                       query, flags=re.IGNORECASE)

        # SQLite datetime defaults → MySQL equivalent
        query = re.sub(r"datetime\s*\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", query, flags=re.IGNORECASE)
        query = re.sub(r"DEFAULT\s+\(CURRENT_TIMESTAMP\)", "DEFAULT CURRENT_TIMESTAMP", query, flags=re.IGNORECASE)

        # TEXT type translation — only meaningful inside DDL (CREATE TABLE / ALTER TABLE).
        # For DML (SELECT/INSERT/UPDATE) TEXT never appears as a type reference.
        if re.search(r'\bCREATE\s+TABLE\b', query, re.IGNORECASE):
            query = self._fix_create_table_text_types(query)
        # ALTER TABLE ADD COLUMN: TEXT → LONGTEXT (no index context to check)
        elif re.search(r'\bALTER\s+TABLE\b', query, re.IGNORECASE):
            query = re.sub(r'\bTEXT\b', 'LONGTEXT', query, flags=re.IGNORECASE)

        # Quote column names that are MySQL reserved words used in our trading schema.
        # Strategy_signals + meta_decisions use 'signal' and 'strategy' as column names;
        # both are reserved in MySQL 8.0 and cause ProgrammingError 1064 when unquoted.
        # We split on single-quoted string literals so values like 'NEUTRAL' are untouched.
        query = _quote_reserved_columns(query)

        query = query.replace('?', '%s')
        return query

    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def close(self):
        self.cursor.close()

_DML_PREFIXES = ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE')

class MySQLConnectionWrapper:
    def __init__(self, mysql_conn):
        self.conn = mysql_conn
        self.row_factory = False

    # ------------------------------------------------------------------ #
    # Context manager — mirrors SQLite: commit on success, rollback on error
    # ------------------------------------------------------------------ #
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            try:
                self.conn.rollback()
            except Exception:
                pass
        else:
            try:
                self.conn.commit()
            except Exception:
                pass
        return False   # never suppress exceptions

    # ------------------------------------------------------------------ #

    def cursor(self):
        # buffered=True: MySQL reads all rows into memory immediately.
        # This prevents "Unread result found" errors when a SELECT is followed
        # by another query or a commit on the same connection.
        cursor = self.conn.cursor(buffered=True)
        return MySQLCursorWrapper(cursor, row_factory=self.row_factory)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def execute(self, query, params=None):
        cur = self.cursor()
        try:
            cur.execute(query, params)
        except Exception as e:
            # Silently ignore "already exists" DDL errors — mirrors SQLite's
            # IF NOT EXISTS / IF EXISTS semantics for CREATE TABLE, CREATE INDEX,
            # and ALTER TABLE ADD COLUMN when the object is already there.
            errno = getattr(e, 'errno', None)
            if errno in (
                1050,   # Table already exists
                1060,   # Duplicate column name (ALTER TABLE ADD COLUMN)
                1061,   # Duplicate key name  (CREATE INDEX — index already exists)
                1068,   # Multiple primary key defined
                1089,   # Incorrect prefix key (prefix > column length — index exists from migration)
                1170,   # BLOB/TEXT column used in key without length — index exists from migration
            ):
                cur.close()
                return cur   # return closed cursor — caller's fetchall() returns []
            raise
        # Only commit DML/DDL; SELECT/SHOW/PRAGMA don't need (or want) a commit.
        stripped = (query or '').strip().upper()
        if any(stripped.startswith(p) for p in _DML_PREFIXES):
            self.conn.commit()
        # Do NOT close the cursor here.
        # SQLite's Connection.execute() returns a live cursor so the caller can
        # do conn.execute("SELECT ...").fetchall().  Closing before return breaks
        # that pattern.  Python's GC will close the cursor when it goes out of scope.
        return cur

    def executescript(self, script: str):
        """
        SQLite-compatible executescript(): splits the script on ';', translates
        each statement through _translate(), and executes them in sequence.
        Blank statements and comment-only lines are silently skipped.
        Errors are logged and skipped so one bad statement doesn't abort the rest.
        """
        import sys as _sys
        cur = self.cursor()
        for raw_stmt in script.split(';'):
            stmt = raw_stmt.strip()
            if not stmt or stmt.startswith('--'):
                continue
            try:
                cur.execute(stmt)
                stripped = stmt.upper()
                if any(stripped.startswith(p) for p in _DML_PREFIXES):
                    self.conn.commit()
            except Exception as e:
                print(f"[MYSQL BRIDGE] executescript warning: {e} | stmt={stmt[:120]}",
                      file=_sys.stderr)
        cur.close()

def mysql_connect(db_path, **kwargs):
    db_path_lower = str(db_path).lower()
    
    # Identify target database name
    if "trading.db" in db_path_lower:
        db_name = "trading_live"
    elif "dhan_dashboard.db" in db_path_lower:
        db_name = "dhan_dashboard_live"
    else:
        # Fallback to local SQLite for other local files (e.g. caches or static data)
        return sqlite3._original_connect(db_path, **kwargs)

    # Establish connection to WAMP/MySQL server
    mysql_conn = mysql.connector.connect(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="Krishna@123",
        database=db_name,
        autocommit=False
    )
    return MySQLConnectionWrapper(mysql_conn)

# Enforce transparent redirection inside the sqlite3 module globally
if not hasattr(sqlite3, "_original_connect"):
    sqlite3._original_connect = sqlite3.connect
sqlite3.connect = mysql_connect
sqlite3.Row = MySQLRow

print("[MYSQL BRIDGE] Transparent sqlite3-to-MySQL connection redirection active!")
