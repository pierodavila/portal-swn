"""
Autenticacao e autorizacao do Portal SWN.

- login_required: exige sessao; sem login -> redireciona para /login (302).
- require_roles('rh','admin'): exige login + um dos papeis. 'admin' sempre
  passa (acesso a tudo). Sem permissao -> 403.
- helpers de sessao: current_user, set_session, clear_session.

Os papeis NAO sao hierarquicos: cada rota declara o conjunto que aceita.
A unica excecao e 'admin', que tem acesso a tudo.
"""
from functools import wraps

from flask import session, redirect, url_for, abort


# ----------------------------------------------------------------------------
# Helpers de sessao
# ----------------------------------------------------------------------------
def set_session(user):
    """Grava os campos minimos do usuario na sessao."""
    session["uid"] = user["id"]
    session["nome"] = user["nome"]
    session["email"] = user["email"]
    session["papel"] = user["papel"]
    session["loja_id"] = user.get("loja_id")


def clear_session():
    session.clear()


def current_user():
    """Retorna um dict simples com o usuario logado, ou None."""
    if "uid" not in session:
        return None
    return {
        "id": session.get("uid"),
        "nome": session.get("nome"),
        "email": session.get("email"),
        "papel": session.get("papel"),
        "loja_id": session.get("loja_id"),
    }


def is_logged_in():
    return "uid" in session


def has_role(*papeis):
    """True se o usuario logado e 'admin' ou tem um dos papeis informados."""
    papel = session.get("papel")
    if papel is None:
        return False
    if papel == "admin":
        return True
    return papel in papeis


# ----------------------------------------------------------------------------
# Decorators
# ----------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def require_roles(*papeis):
    """Exige login + papel. 'admin' sempre passa. Sem login -> /login;
    logado sem papel -> 403."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not is_logged_in():
                return redirect(url_for("login"))
            if not has_role(*papeis):
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator
