"""
Camada fina de acesso a banco — funciona com SQLite (local) e Postgres (Render).

Seleção automática pela variável de ambiente DATABASE_URL:
  - ausente ou começando com 'sqlite' -> sqlite3 (arquivo portal.db)
  - começando com 'postgres'          -> psycopg2

A API publica e a mesma nos dois backends:
  get_db()                      -> conexao (cacheada no contexto da request)
  query(sql, params, one=False) -> lista de dicts (ou um dict / None)
  execute(sql, params)          -> executa e da commit; retorna lastrowid (sqlite)
  init_db()                     -> cria tabelas se nao existem

IMPORTANTE: escreva o SQL com placeholders no estilo '?'. A camada traduz
para '%s' automaticamente quando o backend for Postgres.
"""
import os
import sqlite3

from flask import g

# ----------------------------------------------------------------------------
# Deteccao de backend
# ----------------------------------------------------------------------------
_RAW_URL = os.environ.get("DATABASE_URL", "").strip()

if _RAW_URL.startswith("postgres"):
    BACKEND = "postgres"
else:
    BACKEND = "sqlite"

# Render entrega URLs no formato 'postgres://'; psycopg2 aceita ambos, mas
# normalizamos para 'postgresql://' por seguranca.
if BACKEND == "postgres" and _RAW_URL.startswith("postgres://"):
    _RAW_URL = "postgresql://" + _RAW_URL[len("postgres://"):]

# Caminho do arquivo SQLite local (usado so quando BACKEND == 'sqlite')
SQLITE_PATH = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "portal.db"))


# ----------------------------------------------------------------------------
# Conexao
# ----------------------------------------------------------------------------
def _connect():
    if BACKEND == "postgres":
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(_RAW_URL)
        return conn
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """Retorna a conexao da request atual (criando se necessario)."""
    if "db_conn" not in g:
        g.db_conn = _connect()
    return g.db_conn


def close_db(exc=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


# ----------------------------------------------------------------------------
# Helpers de SQL
# ----------------------------------------------------------------------------
def _adapt(sql):
    """Traduz placeholders '?' -> '%s' quando o backend for Postgres."""
    if BACKEND == "postgres":
        return sql.replace("?", "%s")
    return sql


def _dict_cursor(conn):
    if BACKEND == "postgres":
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def query(sql, params=(), one=False):
    """SELECT -> lista de dicts (ou um dict / None quando one=True)."""
    conn = get_db()
    cur = _dict_cursor(conn)
    cur.execute(_adapt(sql), params)
    rows = cur.fetchall()
    cur.close()
    result = [dict(r) for r in rows]
    if one:
        return result[0] if result else None
    return result


def execute(sql, params=()):
    """INSERT/UPDATE/DELETE -> da commit. Retorna lastrowid quando aplicavel."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_adapt(sql), params)
    conn.commit()
    last = None
    try:
        last = cur.lastrowid  # sqlite
    except Exception:
        last = None
    cur.close()
    return last


# ----------------------------------------------------------------------------
# Schema / init
# ----------------------------------------------------------------------------
def _schema_statements():
    """Retorna o DDL apropriado para o backend ativo."""
    if BACKEND == "postgres":
        pk = "SERIAL PRIMARY KEY"
        boolt = "INTEGER"         # usamos 0/1 nos INSERT/UPDATE; INTEGER evita mismatch booleano
        booldef = "1"
        ts = "TIMESTAMP"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
        boolt = "INTEGER"
        booldef = "1"             # SQLite usa inteiro
        ts = "TEXT"

    return [
        f"""
        CREATE TABLE IF NOT EXISTS lojas (
            id        {pk},
            nome      TEXT NOT NULL,
            cnpj      TEXT,
            cidade_uf TEXT,
            ativo     {boolt} DEFAULT {booldef}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id            {pk},
            nome          TEXT NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            senha_hash    TEXT NOT NULL,
            papel         TEXT NOT NULL,
            loja_id       INTEGER,
            ativo         {boolt} DEFAULT {booldef},
            criado_em     {ts},
            ultimo_acesso {ts}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS audit_log (
            id         {pk},
            usuario_id INTEGER,
            acao       TEXT NOT NULL,
            detalhe    TEXT,
            ip         TEXT,
            criado_em  {ts}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS colaboradores (
            id           {pk},
            nome         TEXT NOT NULL,
            cpf          TEXT,
            loja_id      INTEGER,
            cargo        TEXT,
            admissao     TEXT,
            desligamento TEXT,
            contato      TEXT,
            criado_em    {ts},
            criado_por   INTEGER
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS avaliacoes (
            id             {pk},
            colaborador_id INTEGER,
            avaliador_id   INTEGER,
            tipo           TEXT,
            periodo        TEXT,
            kpi_meta       TEXT,
            kpi_real       TEXT,
            nota_resultado REAL,
            nota_comp      REAL,
            nota_final     REAL,
            competencias   TEXT,
            pontos_fortes  TEXT,
            a_desenvolver  TEXT,
            plano          TEXT,
            criado_em      {ts},
            criado_por     INTEGER
        )
        """,
    ]


def init_db():
    """Cria as tabelas se nao existirem."""
    conn = _connect()
    cur = conn.cursor()
    for ddl in _schema_statements():
        cur.execute(ddl)
    conn.commit()
    cur.close()
    conn.close()
