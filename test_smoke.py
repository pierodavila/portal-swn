"""
Smoke test do Portal SWN com SQLite + app.test_client().

Prova:
  (1) GET /tool/avaliacao sem login -> 302 (redireciona p/ /login) ou 401/403.
  (2) login como 'vendedor' + GET de ferramenta de admin (gestao) -> 403.
  (3) login como 'admin' -> 200 na mesma ferramenta.
  (4) senha_hash salvo != senha em texto puro.

Roda com banco SQLite isolado e em modo debug (cookies sem Secure p/ test_client).
"""
import os
import sys
import tempfile

# Ambiente de teste ANTES de importar o app
_tmp_db = os.path.join(tempfile.mkdtemp(), "portal_test.db")
os.environ["DATABASE_URL"] = "sqlite"          # força backend sqlite
os.environ["SQLITE_PATH"] = _tmp_db
os.environ["SECRET_KEY"] = "test-secret"
os.environ["FLASK_DEBUG"] = "1"                # SESSION_COOKIE_SECURE=False p/ http
os.environ["ADMIN_EMAIL"] = "admin@swn.local"
os.environ["ADMIN_PASSWORD"] = "admin123"

import app as app_module
from werkzeug.security import generate_password_hash

app = app_module.app
app.testing = True


def _login(client, email, senha):
    return client.post("/login", data={"email": email, "senha": senha},
                       follow_redirects=False)


def _seed_vendedor():
    """Cria um vendedor de teste direto no banco, dentro do contexto do app."""
    with app.app_context():
        from db import query, execute
        existe = query("SELECT id FROM usuarios WHERE email = ?",
                       ("vendedor@swn.local",), one=True)
        if not existe:
            execute(
                "INSERT INTO usuarios (nome, email, senha_hash, papel, ativo, criado_em) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("Vendedor Teste", "vendedor@swn.local",
                 generate_password_hash("vend123"), "vendedor", 1, "2026-01-01"),
            )


def run():
    results = []
    client = app.test_client()
    _seed_vendedor()

    # (1) rota protegida sem login
    r = client.get("/tool/avaliacao")
    cond1 = r.status_code in (302, 401, 403)
    loc = r.headers.get("Location", "")
    assert cond1, f"(1) esperava 302/401/403, veio {r.status_code}"
    results.append(f"(1) /tool/avaliacao sem login -> {r.status_code} "
                   f"(Location='{loc}')  OK")

    # (2) vendedor em ferramenta de admin
    _login(client, "vendedor@swn.local", "vend123")
    r = client.get("/tool/gestao")
    assert r.status_code == 403, f"(2) esperava 403, veio {r.status_code}"
    results.append(f"(2) vendedor -> /tool/gestao -> {r.status_code} (403 esperado)  OK")
    client.get("/logout")

    # (3) admin na mesma ferramenta
    _login(client, "admin@swn.local", "admin123")
    r = client.get("/tool/gestao")
    assert r.status_code == 200, f"(3) esperava 200, veio {r.status_code}"
    is_html = r.data[:200].lower().find(b"<") != -1
    assert is_html, "(3) resposta nao parece HTML"
    results.append(f"(3) admin -> /tool/gestao -> {r.status_code} (HTML servido)  OK")
    client.get("/logout")

    # (4) hash != texto puro
    with app.app_context():
        from db import query
        row = query("SELECT senha_hash FROM usuarios WHERE email = ?",
                    ("admin@swn.local",), one=True)
    h = row["senha_hash"]
    assert h != "admin123", "(4) senha_hash igual a senha em texto puro!"
    assert h.startswith(("pbkdf2:", "scrypt:", "argon2")), \
        f"(4) hash em formato inesperado: {h[:20]}"
    results.append(f"(4) senha_hash != texto puro  OK  (prefixo='{h.split(':')[0]}')")

    # extras de sanidade
    r = client.get("/health")
    assert r.status_code == 200 and r.data == b"ok", "/health falhou"
    results.append(f"(+) /health -> {r.status_code} '{r.data.decode()}'  OK")

    r = client.get("/tool/nao_existe")
    assert r.status_code in (302, 404), "ferramenta inexistente deveria dar 404/redirect"
    # sem login -> login_required redireciona; com login daria 404
    return results


if __name__ == "__main__":
    try:
        out = run()
    except AssertionError as e:
        print("FALHOU:", e)
        sys.exit(1)
    print("=" * 60)
    print("SMOKE TEST — Portal SWN")
    print("=" * 60)
    for line in out:
        print("  " + line)
    print("=" * 60)
    print("TODOS OS ASSERTS PASSARAM.")
