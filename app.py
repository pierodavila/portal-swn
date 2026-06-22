"""
Portal SWN — serviço web Flask (Fase 1).

Login real + casca protegida + RBAC + admin de usuários.
SERVIÇO SEPARADO do app de Conferência de Caixa — sem relação de código nem
de dados. As ferramentas continuam sendo os HTMLs em tools/ (ainda usando
localStorage; migração de dados é fase futura).

Rodar local (sem Postgres): python app.py  -> usa SQLite (portal.db).
Produção (Render): gunicorn + Postgres via DATABASE_URL.
"""
import os
import logging
from datetime import datetime

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    abort, send_from_directory, Response,
)
from werkzeug.security import generate_password_hash, check_password_hash

import db
from db import query, execute, init_db, close_db
from auth import (
    login_required, require_roles, set_session, clear_session,
    current_user,
)
import catalog

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("portal_swn")

PAPEIS_VALIDOS = catalog.ROLE_IDS  # 9 grupos definidos em catalog.ROLES
TOOLS_DIR = os.path.join(os.path.dirname(__file__), "tools")


# ----------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------
def create_app():
    app = Flask(__name__)

    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-troque-em-producao")
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not debug,  # Secure em producao (fora do debug)
    )

    app.teardown_appcontext(close_db)

    # ------------------------------------------------------------------ init
    with app.app_context():
        init_db()
        _seed_admin()

    # --------------------------------------------------------------- helpers
    def _now():
        return datetime.utcnow().isoformat(timespec="seconds")

    def _client_ip():
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.remote_addr or ""

    def audit(usuario_id, acao, detalhe=""):
        try:
            execute(
                "INSERT INTO audit_log (usuario_id, acao, detalhe, ip, criado_em) "
                "VALUES (?, ?, ?, ?, ?)",
                (usuario_id, acao, detalhe, _client_ip(), _now()),
            )
        except Exception as e:  # auditoria nunca deve derrubar a request
            log.warning("Falha ao gravar audit_log: %s", e)

    # ----------------------------------------------------------------- rotas
    @app.route("/health")
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            senha = request.form.get("senha") or ""
            user = query(
                "SELECT * FROM usuarios WHERE email = ?", (email,), one=True
            )
            if user and user.get("ativo") and check_password_hash(user["senha_hash"], senha):
                set_session(user)
                execute(
                    "UPDATE usuarios SET ultimo_acesso = ? WHERE id = ?",
                    (_now(), user["id"]),
                )
                audit(user["id"], "login", "login com sucesso")
                return redirect(url_for("portal"))
            audit(None, "login_falhou", "email=%s" % email)
            return render_template(
                "login.html", erro="E-mail ou senha inválidos, ou usuário inativo."
            ), 401
        if current_user():
            return redirect(url_for("portal"))
        return render_template("login.html", erro=None)

    @app.route("/logout")
    def logout():
        u = current_user()
        if u:
            audit(u["id"], "logout", "")
        clear_session()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def portal():
        u = current_user()
        tools = catalog.tools_for_role(u["papel"])
        return render_template("portal.html", user=u, tools=tools)

    @app.route("/tool/<tool_id>")
    @login_required
    def tool(tool_id):
        u = current_user()
        meta = catalog.get_tool(tool_id)
        if meta is None or not meta.get("arquivo"):
            abort(404)
        if not catalog.role_can_access(u["papel"], meta):
            abort(403)
        # Confirma que o arquivo existe em disco
        safe_name = os.path.basename(meta["arquivo"])
        path = os.path.join(TOOLS_DIR, safe_name)
        if not os.path.isfile(path):
            abort(404)
        audit(u["id"], "abrir_ferramenta", tool_id)
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        return Response(html, mimetype="text/html")

    # ------------------------------------------------- admin de usuarios
    @app.route("/admin/usuarios", methods=["GET", "POST"])
    @require_roles("admin")
    def admin_usuarios():
        u = current_user()
        erro = None
        ok = None

        if request.method == "POST":
            nome = (request.form.get("nome") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            senha = request.form.get("senha") or ""
            papel = (request.form.get("papel") or "").strip()
            loja_id = request.form.get("loja_id") or None

            if not nome or not email or not senha or papel not in PAPEIS_VALIDOS:
                erro = "Preencha nome, e-mail, senha e um papel válido."
            elif query("SELECT id FROM usuarios WHERE email = ?", (email,), one=True):
                erro = "Já existe um usuário com esse e-mail."
            else:
                execute(
                    "INSERT INTO usuarios (nome, email, senha_hash, papel, loja_id, ativo, criado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (nome, email, generate_password_hash(senha), papel,
                     loja_id, 1, _now()),
                )
                audit(u["id"], "criar_usuario", email)
                ok = "Usuário %s criado." % email

        usuarios = query(
            "SELECT id, nome, email, papel, loja_id, ativo, criado_em, ultimo_acesso "
            "FROM usuarios ORDER BY criado_em DESC"
        )
        return render_template(
            "admin_usuarios.html", user=u, usuarios=usuarios,
            papeis=list(catalog.ROLES.items()), roles=catalog.ROLES,
            erro=erro, ok=ok,
        )

    @app.route("/admin/usuarios/<int:uid>/toggle", methods=["POST"])
    @require_roles("admin")
    def admin_toggle(uid):
        u = current_user()
        alvo = query("SELECT id, ativo, email FROM usuarios WHERE id = ?", (uid,), one=True)
        if alvo is None:
            abort(404)
        novo = 0 if alvo["ativo"] else 1
        execute("UPDATE usuarios SET ativo = ? WHERE id = ?", (novo, uid))
        audit(u["id"], "toggle_usuario", "%s -> ativo=%s" % (alvo["email"], novo))
        return redirect(url_for("admin_usuarios"))

    @app.route("/admin/usuarios/<int:uid>/reset", methods=["POST"])
    @require_roles("admin")
    def admin_reset(uid):
        u = current_user()
        alvo = query("SELECT id, email FROM usuarios WHERE id = ?", (uid,), one=True)
        if alvo is None:
            abort(404)
        nova = request.form.get("senha") or ""
        if not nova:
            abort(400)
        execute(
            "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
            (generate_password_hash(nova), uid),
        )
        audit(u["id"], "reset_senha", alvo["email"])
        return redirect(url_for("admin_usuarios"))

    # --------------------------------------------------- static (PWA assets)
    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.json")

    @app.route("/sw.js")
    def service_worker():
        return send_from_directory(app.static_folder, "sw.js")

    # ----------------------------------------------------- error handlers
    @app.errorhandler(403)
    def err_403(e):
        return render_template("base.html", titulo="Acesso negado",
                               conteudo="403 — Você não tem permissão para esta ferramenta.",
                               user=current_user()), 403

    @app.errorhandler(404)
    def err_404(e):
        return render_template("base.html", titulo="Não encontrado",
                               conteudo="404 — Página ou ferramenta não encontrada.",
                               user=current_user()), 404

    return app


# ----------------------------------------------------------------------------
# Seed do admin inicial
# ----------------------------------------------------------------------------
def _seed_admin():
    """Se nao houver nenhum usuario, cria um admin a partir das env vars."""
    existe = query("SELECT id FROM usuarios LIMIT 1", one=True)
    if existe:
        return
    email = os.environ.get("ADMIN_EMAIL", "admin@swn.local").strip().lower()
    senha = os.environ.get("ADMIN_PASSWORD", "troque-imediatamente")
    execute(
        "INSERT INTO usuarios (nome, email, senha_hash, papel, ativo, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Administrador", email, generate_password_hash(senha), "admin",
         1, datetime.utcnow().isoformat(timespec="seconds")),
    )
    log.warning(
        "SEED: admin inicial criado (%s). TROQUE A SENHA IMEDIATAMENTE via "
        "/admin/usuarios ou definindo ADMIN_PASSWORD.", email
    )


# Instancia para gunicorn (app:app) e para 'python app.py'
app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port,
            debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"))
