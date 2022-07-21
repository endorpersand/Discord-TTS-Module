import re
import itertools
import sqlite3
from abc import abstractmethod
from collections.abc import MutableSet, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Callable, TypeVar, Mapping, MutableMapping

def scrub(table_name):
    return ''.join( chr for chr in table_name if chr.isalnum() or chr in "_" )

def placeholder(n):
    return ",".join("?" * n)

class Database:
    DB_DIR = Path('data') / 'db'
    def __init__(self, db: str, *, require_exists=False, readonly=False, use_row_type=False):
        self.path = self.DB_DIR / db
        if require_exists and not self.path.exists(): raise ValueError(f"Database {db} does not exist")
        
        if readonly:
            path = self.readonly_path(self.path)
        else:
            path = self.path

        self.con = sqlite3.connect(path)
        if use_row_type:
            self.con.row_factory = sqlite3.Row
    
    @staticmethod
    def readonly_path(path: Path):
        # Convert all "?" characters into "%3f".
        # Convert all "#" characters into "%23".
        # On windows only, convert all "\" characters into "/".
        # Convert all sequences of two or more "/" characters into a single "/" character.
        # On windows only, if the filename begins with a drive letter, prepend a single "/" character.
        # Prepend the "file:" scheme.

        p = str(path).replace("?", "%3f").replace("#", "%23")
        p = re.sub(r"/{2,}", "/", p)
        return f"file:{p}?mode=ro"

    @classmethod
    def databases(cls):
        return cls.DB_DIR.glob("*.db")

    def tables(self):
        return [r[0] for r in self.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    def execute(self, cmd: str, params: "tuple | None" = None):
        cur = self.con.cursor()
        if params is None:
            cur.execute(cmd)
        else:
            cur.execute(cmd, params)
        self.con.commit()
        return cur

    def create_table(self, table: str, cols: "Iterable[str]"):
        sep = ",\n"
        return self.execute(f"""CREATE TABLE IF NOT EXISTS {table} (\n{sep.join(cols)}\n)""")
    
    def drop_table(self, table: str):
        return self.execute(f"""DROP TABLE IF EXISTS {table}""")
    
    def table_exists(self, table: str):
        return bool(self.execute("""SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?""", (table,)).fetchone()[0])

    def RowView(self, table: str, cols: "Iterable[str] | None" = None):
        """
        Use a SQL table as a mutable dict (:class:`MutableMapping[Any, Row]`). 
        The first column is used as the index to access other columns.
        
        The values of this dict are similar to the :class:`sqlite3.Row` type but mutable.

        :meth:`map_values` can be called on this object with `from_sql` and `to_sql`
        parameters to map values inserted and extracted from the SQL database.

        Both parameters can be effectively treated as :class:`Callable[tuple, tuple]`.

        # Parameters

        table: :class:`str`
            The name of the table in the database
        cols: :class:`Optional[Iterable[str]]`
            If the table does not exist, this parameter should be a list of 
            columns specifying the columns of the table. 
            
            Each column is of the format `"col_name col_type"` and 
            "PRIMARY KEY" is automatically applied to the first column.

            If the table exists, this parameter does nothing.
        """
        return RowView(self, table, cols)

    def GuildTable(self, table: str, rest: "Iterable[str] | None" = None):
        """
        Use a SQL table as a mutable dict (:class:`MutableMapping[int, Row]`). 
        The first column is always `guild_id`.
        
        The values of this dict are similar to the :class:`sqlite3.Row` type but mutable.

        :meth:`map_values` can be called on this object with `from_sql` and `to_sql`
        parameters to map values inserted and extracted from the SQL database.

        Both parameters can be effectively treated as :class:`Callable[tuple, tuple]`.
        
        # Parameters

        table: :class:`str`
            The name of the table in the database
        rest: :class:`Optional[Iterable[str]]`
            If the table does not exist, this parameter should be a list of 
            columns specifying the columns of the table after the first column (which is `guild_id`). 

            Each column is of the format `"col_name col_type"`. 
            
            When accessing this dict, a tuple-like object will be returned with the exact columns
            defined by this `rest` parameter.

            If the table exists, this parameter does nothing.
        """
        return GuildTable(self, table, rest)

    def UserTable(self, table: str, rest: "Iterable[str] | None" = None):
        """
        Use a SQL table as a mutable dict (:class:`MutableMapping[int, Row]`). 
        The first column is always `user_id`.
        
        The values of this dict are similar to the :class:`sqlite3.Row` type but mutable.

        :meth:`map_values` can be called on this object with `from_sql` and `to_sql`
        parameters to map values inserted and extracted from the SQL database.

        Both parameters can be effectively treated as :class:`Callable[tuple, tuple]`.
        
        # Parameters

        table: :class:`str`
            The name of the table in the database
        rest: :class:`Optional[Iterable[str]]`
            If the table does not exist, this parameter should be a list of 
            columns specifying the columns of the table after the first column (which is `user_id`). 

            Each column is of the format `"col_name col_type"`. 
            
            When accessing this dict, a tuple-like object will be returned with the exact columns
            defined by this `rest` parameter.

            If the table exists, this parameter does nothing.
        """
        return UserTable(self, table, rest)

    def SetView(self, table: str):
        """
        Use a SQL table as a mapping of indexes to a set (:class:`Mapping[Any, MutableSet]`).
                
        # Parameters

        table: :class:`str`
            The name of the table in the database
        """
        return SetView(self, table)

    def TwoKeyView(self, table: str, cols: "Iterable[str] | None" = None):
        """
        Use a SQL table as a mapping of indexes to a dict (:class:`Mapping[Any, MutableMapping[Any, Row]]`). 
        The two columns are used as indexes to access other columns.
        
        The values of the end dict are similar to the :class:`sqlite3.Row` type but mutable.

        This is useful for when specific data needs to be bound to two keys together (e.g. `guild_id` and `user_id`).

        # Parameters

        table: :class:`str`
            The name of the table in the database
        cols: :class:`Optional[Iterable[str]]`
            If the table does not exist, this parameter should be a list of 
            columns specifying the columns of the table. 
            
            Each column is of the format `"col_name col_type"` and 
            "PRIMARY KEY" is automatically applied to the first column.

            If the table exists, this parameter does nothing.
        """
        return TwoKeyView(self, table, cols)

    def close(self):
        self.con.close()
    
    def is_closed(self):
        try:
            self.execute("")
        except sqlite3.ProgrammingError:
            return True
        else:
            return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

K = TypeVar("K")
T = TypeVar("T")
V = TypeVar("V")
RSelf = TypeVar("RSelf", bound="RowView")

class _RowMixin(MutableMapping[K, V]):
    sql2v: Callable[[Sequence], V]
    v2sql: Callable[[V], Sequence]
    
    @abstractmethod
    def get_row(self, key: K) -> "_MutRow": pass

    def __getitem__(self, key: K):
        row = self.get_row(key)
        if row.exists(): return self.sql2v(row)
        else: raise KeyError(key)

    def __setitem__(self, key: K, value: V):
        self.get_row(key).replace(self.v2sql(value))
    
    def __delitem__(self, key: K):
        self.get_row(key).clear()
        
    def __repr__(self):
        return "{" + ", ".join(f"{repr(k)}: {repr(v)}" for k, v in self.items()) + "}"

class _MutRow(Sequence):
    def __init__(self, db: Database, table, keys: "Iterable"):
        self.db = db
        self.table = table
        self.keys = tuple(keys)

        self._cols:       "tuple[str, ...]" = tuple(cn for cn, *_ in self.db.execute("SELECT name FROM pragma_table_info(?)", (self.table,)))
        
        self._key_cols:   "tuple[str, ...]" = self._cols[:len(self.keys)]
        self._value_cols: "tuple[str, ...]" = self._cols[len(self.keys):]
        
        self._where_clause = ' AND '.join(f"{c} = ?" for c in self._key_cols)

        self.has_defaults = any(e is not None for e, *_ in db.execute("SELECT dflt_value FROM pragma_table_info(?)", (table,)).fetchall()[len(self.keys):])

    def _resolve_index(self, index: "str | int | slice"):
        if isinstance(index, str):
            if index not in self._value_cols:
                raise KeyError(index)
            return index
        else:
            return self._value_cols[index]

    def __getitem__(self, index):
        if self.exists():
            col = self._resolve_index(index)
            is_one_col = isinstance(col, str)

            if is_one_col: 
                cols = (col,)
            else:
                if len(col) == 0: return ()
                cols = col

            result = self.db.execute(f"""SELECT {",".join(cols)} FROM {self.table} WHERE {self._where_clause}""", self.keys).fetchone()
            if is_one_col: return result[0]
            return result

        return None

    def __setitem__(self, index, value):
        col = self._resolve_index(index)
        is_one_col = isinstance(col, str)
        if is_one_col: 
            cols = (col,)
            values = (value,)
        else:
            cols = col
            values: tuple = tuple(value)

            n_cols = len(cols)
            n_values = len(values)
            if n_cols != n_values:
                raise ValueError(f"Expected {n_cols} value{'s' if n_cols != 1 else ''}, got {n_values}")
            if n_cols == 0:
                return


        if self.exists():
                return self.db.execute(f"""UPDATE {self.table} SET {','.join(c + " = ?" for c in cols)} WHERE {self._where_clause}""", values + self.keys)
        else:
            modified_cols = self._key_cols + cols
            col_values = self.keys + values
            return self.db.execute(f"""REPLACE INTO {self.table} ({",".join(modified_cols)}) VALUES ({placeholder(len(modified_cols))})""", col_values)

    def __len__(self):
        return len(self._value_cols)

    def __iter__(self):
        if self.exists():
            return itertools.islice(self._row_match().fetchone(), len(self.keys), None)
        
        return itertools.repeat(None, len(self._value_cols))

    def _row_match(self):
        return self.db.execute(f"SELECT * FROM {self.table} WHERE {self._where_clause}", self.keys)

    def exists(self) -> bool:
        exists = self._row_match().fetchone() is not None
        if not exists:
            if self.has_defaults:
                self.db.execute(f"""REPLACE INTO {self.table} ({",".join(self._key_cols)}) VALUES ({placeholder(len(self.keys))})""", self.keys)
            exists = self._row_match().fetchone() is not None
        
        return exists

    def replace(self, tpl: Iterable):
        entry = self.keys + tuple(tpl)
        return self.db.execute(f"""REPLACE INTO {self.table} VALUES ({placeholder(len(entry))})""", entry)

    def clear(self):
        return self.db.execute(f"""DELETE FROM {self.table} WHERE {self._where_clause}""", self.keys)
    
    def __repr__(self):
        return f"""<{self.__class__.__qualname__} [{','.join(repr(v) for v in self.keys)}|{','.join(repr(v) for v in self)}]>"""

class RowView(_RowMixin[K, V]):
    def __init__(self, db: Database, table: str, cols: "Iterable[str] | None" = None):
        self.db = db
        self.table = table = scrub(table)
        _pk_txt = "PRIMARY KEY"
        
        if not db.table_exists(table):
            if cols is not None:
                cols = [c.strip() for c in cols]
                if any(_pk_txt in c.upper() for c in cols[1:]):
                    raise TypeError("Primary key must be first column only")
                if _pk_txt not in cols[0].upper():
                    cols[0] += " " + _pk_txt

                db.create_table(table, cols)
            else:
                raise TypeError("Missing argument cols")
        
        # Grab primary key
        self.pk = db.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchone()[0]

        self.sql2v = self.v2sql = lambda t: t

    def get_row(self, key):
        return _MutRow(self.db, self.table, (key,))

    def __iter__(self) -> "Iterator[K]":
        return (pk for pk, *_ in self.db.execute(f"SELECT {self.pk} FROM {self.table}"))

    def __len__(self) -> int:
        return self.db.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
    
    def map_values(
        self: RSelf, *, 
        from_sql: "Callable[[Sequence], T] | None" = None, 
        to_sql:   "Callable[[T], Sequence] | None" = None
    ) -> RSelf:
        if callable(from_sql): self.sql2v = from_sql
        if callable(to_sql): self.v2sql = to_sql
        return self

class GuildTable(RowView[int, V]):
    def __init__(self, db: Database, table: str, rest: "Iterable[str] | None" = None):
        if rest is None: cols = None
        else: cols = ("guild_id int PRIMARY KEY",) + tuple(rest)
        super().__init__(db, table, cols)

class UserTable(RowView[int, V]):
    def __init__(self, db: Database, table: str, rest: "Iterable[str] | None" = None):
        if rest is None: cols = None
        else: cols = ("user_id int PRIMARY KEY",) + tuple(rest)
        super().__init__(db, table, cols)

class SetView(Mapping[Any, MutableSet]):
    PK = "key"
    SK = "set_entries"
    def __init__(self, db: Database, table: str):
        self.db = db
        self.table = table = scrub(table)

        cols = (
            f"{self.PK} NOT NULL",
            f"{self.SK} NOT NULL",
            f"UNIQUE({self.PK}, {self.SK})",
        )
        self.db.create_table(table, cols)

    def __getitem__(self, key) -> MutableSet:
        return _SetEntriesView(self.db, self.table, key)

    def __iter__(self):
        return (pk for pk, *_ in self.db.execute(f"SELECT DISTINCT {self.PK} FROM {self.table}"))

    def __len__(self):
        return self.db.execute(f"SELECT COUNT(DISTINCT {self.PK}) FROM {self.table}").fetchone()[0]
    
    def __repr__(self):
        return "{" + ", ".join(f"{repr(k)}: {repr(v)}" for k, v in self.items()) + "}"

class _SetEntriesView(MutableSet):
    def __init__(self, db: Database, table: str, dkey):
        self.db = db
        self.table = table
        self.dkey = dkey

    @property
    def PK(self): return SetView.PK
    @property
    def SK(self): return SetView.SK

    def __contains__(self, o):
        return bool(self.db.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {self.PK} = ? AND {self.SK} = ?", (self.dkey, o)).fetchone()[0])
    
    def __iter__(self):
        return (e for e, *_ in self.db.execute(f"SELECT {self.SK} FROM {self.table} WHERE {self.PK} = ?", (self.dkey,)))

    def __len__(self):
        return self.db.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {self.PK} = ?", (self.dkey,)).fetchone()[0]

    def add(self, o):
        self.db.execute(f"INSERT OR IGNORE INTO {self.table} VALUES (?, ?)", (self.dkey, o))

    def discard(self, o):
        self.db.execute(f"DELETE FROM {self.table} WHERE {self.PK} = ? AND {self.SK} = ?", (self.dkey, o))

    def __repr__(self):
        if len(self) == 0: return "{<empty set>}"
        return "{" + ", ".join(repr(e) for e, *_ in self) + "}"

class TwoKeyView(Mapping[Any, MutableMapping[Any, Sequence]]):
    def __init__(self, db: Database, table: str, cols: "Iterable[str] | None" = None):
        self.db = db
        self.table = table = scrub(table)

        _pk_txt = "PRIMARY KEY"
        _uq_txt = "UNIQUE"
        _nn_txt = "NOT NULL"

        if not db.table_exists(table):
            if cols is not None:
                cols = [c.strip() for c in cols]
                if any(_pk_txt in c.upper() or _uq_txt in c.upper() for c in cols[1:]):
                    raise TypeError("Primary key and unique constraints are automatically added to the first two columns and should not be specified in table declaration")
                for i in range(0, 2):
                    if _nn_txt not in cols[i].upper():
                        cols[i] += " " + _nn_txt
                cols.append("UNIQUE({})".format(",".join(c.strip().partition(" ")[0] for c in cols[0:2])))

                db.create_table(table, cols)
            else:
                raise TypeError("Missing argument cols")
        
        self.k1, self.k2 = [k for k, *_ in db.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchmany(2)]
        self.isql2v = self.iv2sql = lambda t: t
        self.itry_default = any(e is not None for e, *_ in db.execute("SELECT dflt_value FROM pragma_table_info(?)", (table,)).fetchall()[2:])

    def __getitem__(self, key) -> MutableMapping[Any, Sequence]:
        return _TKEntryView(self.db, self.table, self.k1, self.k2, key, self.iv2sql, self.isql2v, self.itry_default)

    def __iter__(self):
        return (k1 for k1, *_ in self.db.execute(f"SELECT DISTINCT {self.k1} FROM {self.table}"))

    def __len__(self):
        return self.db.execute(f"SELECT COUNT(DISTINCT {self.k1}) FROM {self.table}").fetchone()[0]
    
    def __repr__(self):
        return "{" + ", ".join(f"{repr(k)}: {repr(v)}" for k, v in self.items()) + "}"
    
    def map_values(self, *, from_sql=None, to_sql=None):
        if callable(from_sql): self.isql2v = from_sql
        if callable(to_sql): self.iv2sql = to_sql
        return self

class _TKEntryView(_RowMixin[Any, Sequence]):
    def __init__(self, db: Database, table: str, k1, k2, k1v, v2sql, sql2v, try_default):
        self.db = db
        self.table = table
        self.k1 = k1
        self.k2 = k2
        self.k1v = k1v

        self.v2sql = v2sql
        self.sql2v = sql2v

    def __contains__(self, o):
        return bool(self.db.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {self.k1} = ? AND {self.k2} = ?", (self.k1v, o)).fetchone()[0])
    
    def __iter__(self):
        return (k2 for k2, *_ in self.db.execute(f"SELECT {self.k2} FROM {self.table} WHERE {self.k1} = ?", (self.k1v,)))

    def __len__(self):
        return self.db.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {self.k1} = ?", (self.k1v,)).fetchone()[0]
    
    def get_row(self, key):
        return _MutRow(self.db, self.table, (self.k1v, key))

def setup(bot):
    bot.Database = Database