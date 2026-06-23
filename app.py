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
import io
import csv
import json
import time
import secrets
import logging
from datetime import datetime, date, timedelta

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    render_template_string, abort, send_from_directory, Response,
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

# Rate-limit de login (em memória; 1 worker no Render). Bloqueia após N falhas
# por IP dentro da janela, para frear tentativa de força bruta.
_LOGIN_FAILS = {}
LOGIN_MAX_FALHAS = 5
LOGIN_JANELA_SEG = 600  # 10 minutos

# Fuso de Brasília — o Render roda em UTC; sem isto a "data de hoje" virava
# 3h cedo demais (das 21h às 24h BRT o servidor já achava que era amanhã).
try:
    from zoneinfo import ZoneInfo
    BR_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # fallback se a base de fusos não estiver instalada
    from datetime import timezone, timedelta
    BR_TZ = timezone(timedelta(hours=-3))


def _hoje():
    """Data de hoje no fuso de Brasília."""
    return datetime.now(BR_TZ).date()


def _agora_iso():
    """Timestamp atual (Brasília) em ISO, segundos."""
    return datetime.now(BR_TZ).isoformat(timespec="seconds")


def _csv_safe(v):
    """Neutraliza fórmulas em exports CSV (CSV injection no Excel)."""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


# ----------------------------------------------------------------------------
# Gestão de pessoas — helpers (cálculo de situação e indicadores a partir do DB)
# ----------------------------------------------------------------------------
def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _meses_entre(d1, d2):
    m = (d2.year - d1.year) * 12 + (d2.month - d1.month)
    if d2.day < d1.day:
        m -= 1
    return max(m, 0)


def _situacao(c, hoje):
    desl = _parse_date(c.get("desligamento"))
    if desl and desl <= hoje:
        return "Desligado"
    adm = _parse_date(c.get("admissao"))
    if adm and _meses_entre(adm, hoje) < 3:
        return "Experiência"
    return "Ativo"


def _indicadores(colabs):
    hoje = _hoje()
    ym = (hoje.year, hoje.month)
    ativos, exper = [], 0
    adm_mes = desl_mes = 0
    tempos = []
    for c in colabs:
        sit = _situacao(c, hoje)
        adm = _parse_date(c.get("admissao"))
        desl = _parse_date(c.get("desligamento"))
        if adm and (adm.year, adm.month) == ym:
            adm_mes += 1
        if desl and (desl.year, desl.month) == ym:
            desl_mes += 1
        if sit != "Desligado":
            ativos.append(c)
            if sit == "Experiência":
                exper += 1
            if adm:
                tempos.append(_meses_entre(adm, hoje))
    head = len(ativos)
    return {
        "headcount": head,
        "adm_mes": adm_mes,
        "desl_mes": desl_mes,
        "turnover": round(desl_mes / head * 100, 1) if head else 0,
        "tempo_medio": round(sum(tempos) / len(tempos), 1) if tempos else 0,
        "pct_exper": round(exper / head * 100, 1) if head else 0,
        "exper": exper,
    }


# Página da Gestão (server-rendered, dados no Postgres). Inline para subir só
# arquivos .py na raiz (upload confiável no GitHub).
GESTAO_HTML = r"""
{% extends "base.html" %}
{% block title %}Gestão & Cadastros{% endblock %}
{% block body %}
<style>
  .g-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .g-kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .g-kpi b{display:block;font-size:26px;color:var(--brand2);line-height:1.1}
  .g-kpi span{font-size:12px;color:var(--muted)}
  table.g{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
  table.g th,table.g td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
  table.g th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  .sit{font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px}
  .sit.Ativo{background:rgba(46,204,113,.15);color:#7ee2a8}
  .sit.Experiência{background:rgba(241,196,15,.15);color:#f3d35e}
  .sit.Desligado{background:rgba(231,76,60,.15);color:#f29a90}
  .g-form{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-top:12px}
  .g-form .f{display:flex;flex-direction:column;gap:4px}
  .g-form label{font-size:11px;color:var(--muted);font-weight:600}
  .g-form input,.g-form select{padding:8px 10px;border-radius:9px;border:1px solid var(--line);
    background:var(--panel2);color:var(--txt);font-size:13.5px}
  .g-btn{padding:9px 15px;border:0;border-radius:9px;background:var(--brand);color:#1a1300;font-weight:800;cursor:pointer;font-size:13px}
  .g-del{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:5px 9px;cursor:pointer;font-size:12px}
  .g-del:hover{border-color:#e74c3c;color:#f29a90}
  .g-ok{background:rgba(46,204,113,.12);border:1px solid rgba(46,204,113,.35);color:#7ee2a8;padding:9px 12px;border-radius:9px;margin-bottom:12px;font-size:13px}
  .g-er{background:rgba(231,76,60,.12);border:1px solid rgba(231,76,60,.35);color:#f29a90;padding:9px 12px;border-radius:9px;margin-bottom:12px;font-size:13px}
  .g-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
  .g-tabs a{padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);text-decoration:none;font-weight:700;font-size:13px}
  .g-tabs a.on{background:var(--brand);color:#1a1300;border-color:var(--brand)}
  .rollup{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .rollup span{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:12.5px}
</style>

<div class="card">
  <h1>Gestão & Cadastros</h1>
  <p class="muted">Cadastro de colaboradores e lojas <b>salvo no banco</b> — compartilhado entre dispositivos, com histórico. Indicadores de gente calculados ao vivo.</p>
</div>

{% if ok %}<div class="g-ok">✔️ {{ ok }}</div>{% endif %}
{% if erro %}<div class="g-er">⚠️ {{ erro }}</div>{% endif %}

<div class="g-tabs">
  <a href="#colaboradores" class="on">Colaboradores</a>
  <a href="#lojas">Lojas</a>
  <a href="#gente">Indicadores de gente</a>
</div>

<div class="card" id="gente">
  <h2>Indicadores de gente</h2>
  <div class="g-kpis">
    <div class="g-kpi"><b>{{ ind.headcount }}</b><span>Headcount (ativos)</span></div>
    <div class="g-kpi"><b>{{ ind.adm_mes }}</b><span>Admissões no mês</span></div>
    <div class="g-kpi"><b>{{ ind.desl_mes }}</b><span>Desligamentos no mês</span></div>
    <div class="g-kpi"><b>{{ ind.turnover }}%</b><span>Turnover do mês</span></div>
    <div class="g-kpi"><b>{{ ind.tempo_medio }}</b><span>Tempo médio de casa (meses)</span></div>
    <div class="g-kpi"><b>{{ ind.pct_exper }}%</b><span>Em experiência</span></div>
  </div>
  <p class="muted" style="font-size:12px">Turnover = desligamentos do mês ÷ headcount × 100. Situação derivada das datas: &lt;3 meses de casa = Experiência; com data de desligamento = Desligado.</p>
  <h2 style="margin-top:14px">Headcount por loja</h2>
  <div class="rollup">
    {% for loja, n in rollup.items() %}<span>{{ loja }}: <b>{{ n }}</b></span>{% else %}<span class="muted">Sem colaboradores ativos.</span>{% endfor %}
  </div>
</div>

<div class="card" id="colaboradores">
  <h2>Colaboradores <span class="muted" style="font-size:13px">({{ colabs|length }})</span>
    <a href="{{ url_for('gestao_csv') }}" class="navbtn" style="float:right">⬇️ Exportar CSV</a></h2>
  <table class="g">
    <thead><tr><th>Nome</th><th>CPF</th><th>Loja</th><th>Cargo</th><th>Admissão</th><th>Situação</th><th></th></tr></thead>
    <tbody>
    {% for c in colabs %}
      <tr>
        <td><b>{{ c.nome }}</b>{% if c.contato %}<br><span class="muted" style="font-size:11px">{{ c.contato }}</span>{% endif %}</td>
        <td>{{ c.cpf or '—' }}</td>
        <td>{{ c.loja_nome or '—' }}</td>
        <td>{{ c.cargo or '—' }}</td>
        <td>{{ c.admissao or '—' }}</td>
        <td><span class="sit {{ c.situacao }}">{{ c.situacao }}</span></td>
        <td style="white-space:nowrap">
          <a class="g-del" style="text-decoration:none" href="?edit_colab={{ c.id }}#form-colab">editar</a>
          <form method="post" style="display:inline" action="{{ url_for('gestao_colab_del', cid=c.id) }}" onsubmit="return confirm('Remover {{ c.nome }}?')"><input type="hidden" name="_csrf" value="{{ csrf_token }}"><button class="g-del">remover</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="7" class="muted">Nenhum colaborador cadastrado ainda.</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h2 style="margin-top:16px" id="form-colab">{{ '✏️ Editar colaborador' if edit_colab else '➕ Novo colaborador' }}</h2>
  <form class="g-form" method="post" action="{{ url_for('gestao_colab_add') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token }}">
    {% if edit_colab %}<input type="hidden" name="id" value="{{ edit_colab.id }}">{% endif %}
    <div class="f"><label>Nome *</label><input name="nome" required value="{{ edit_colab.nome if edit_colab else '' }}"></div>
    <div class="f"><label>CPF</label><input name="cpf" value="{{ edit_colab.cpf if edit_colab else '' }}"></div>
    <div class="f"><label>Loja</label><select name="loja_id"><option value="">—</option>{% for l in lojas %}<option value="{{ l.id }}"{{ ' selected' if edit_colab and edit_colab.loja_id==l.id else '' }}>{{ l.nome }}</option>{% endfor %}</select></div>
    <div class="f"><label>Cargo</label><input name="cargo" value="{{ edit_colab.cargo if edit_colab else '' }}" placeholder="ex: Vendedor(a)"></div>
    <div class="f"><label>Admissão</label><input type="date" name="admissao" value="{{ edit_colab.admissao if edit_colab else '' }}"></div>
    <div class="f"><label>Desligamento</label><input type="date" name="desligamento" value="{{ edit_colab.desligamento if edit_colab else '' }}"></div>
    <div class="f"><label>Contato</label><input name="contato" value="{{ edit_colab.contato if edit_colab else '' }}" placeholder="tel / e-mail"></div>
    <button class="g-btn" type="submit">{{ 'Salvar' if edit_colab else 'Adicionar' }}</button>
    {% if edit_colab %}<a class="g-del" style="text-decoration:none" href="/gestao#colaboradores">cancelar</a>{% endif %}
  </form>
</div>

<div class="card" id="lojas">
  <h2>Lojas <span class="muted" style="font-size:13px">({{ lojas|length }})</span></h2>
  <table class="g">
    <thead><tr><th>Nome</th><th>CNPJ</th><th>Cidade/UF</th><th></th></tr></thead>
    <tbody>
    {% for l in lojas %}
      <tr>
        <td><b>{{ l.nome }}</b></td><td>{{ l.cnpj or '—' }}</td><td>{{ l.cidade_uf or '—' }}</td>
        <td style="white-space:nowrap">
          <a class="g-del" style="text-decoration:none" href="?edit_loja={{ l.id }}#form-loja">editar</a>
          <form method="post" style="display:inline" action="{{ url_for('gestao_loja_del', lid=l.id) }}" onsubmit="return confirm('Remover {{ l.nome }}?')"><input type="hidden" name="_csrf" value="{{ csrf_token }}"><button class="g-del">remover</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="4" class="muted">Nenhuma loja cadastrada. Cadastre as lojas primeiro.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  <h2 style="margin-top:16px" id="form-loja">{{ '✏️ Editar loja' if edit_loja else '➕ Nova loja' }}</h2>
  <form class="g-form" method="post" action="{{ url_for('gestao_loja_add') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token }}">
    {% if edit_loja %}<input type="hidden" name="id" value="{{ edit_loja.id }}">{% endif %}
    <div class="f"><label>Nome *</label><input name="nome" required value="{{ edit_loja.nome if edit_loja else '' }}" placeholder="ex: Forum / Colcci ..."></div>
    <div class="f"><label>CNPJ</label><input name="cnpj" value="{{ edit_loja.cnpj if edit_loja else '' }}"></div>
    <div class="f"><label>Cidade / UF</label><input name="cidade_uf" value="{{ edit_loja.cidade_uf if edit_loja else '' }}"></div>
    <button class="g-btn" type="submit">{{ 'Salvar' if edit_loja else 'Adicionar' }}</button>
    {% if edit_loja %}<a class="g-del" style="text-decoration:none" href="/gestao#lojas">cancelar</a>{% endif %}
  </form>
</div>
{% endblock %}
"""


# ----------------------------------------------------------------------------
# Avaliações de desempenho — competências, helpers de nota e templates (Fase 3.2)
# ----------------------------------------------------------------------------
# Competências fixas pontuadas de 1 a 5. NÃO incluem "resultado/metas": isso entra
# pelo KPI (R$), evitando contar a mesma coisa duas vezes.
AVAL_COMPETENCIAS = [
    "Atendimento ao cliente",
    "Postura e conduta",
    "Trabalho em equipe",
    "Organização e VM",
    "Pontualidade e assiduidade",
    "Proatividade e iniciativa",
]
AVAL_TIPOS = ["Mensal", "Experiência (45 dias)", "Experiência (90 dias)", "Anual"]


def _nota_resultado(meta, real):
    """Converte atingimento de meta (R$) numa nota 1–5. Retorna (nota, atingimento%) ou (None, None)."""
    try:
        meta = float(str(meta).replace(".", "").replace(",", ".")) if meta not in (None, "") else 0.0
        real = float(str(real).replace(".", "").replace(",", ".")) if real not in (None, "") else 0.0
    except (TypeError, ValueError):
        return None, None
    if meta <= 0:
        return None, None
    ating = real / meta * 100.0
    if ating >= 110:
        nota = 5.0
    elif ating >= 100:
        nota = 4.5
    elif ating >= 90:
        nota = 4.0
    elif ating >= 80:
        nota = 3.0
    elif ating >= 70:
        nota = 2.0
    else:
        nota = 1.0
    return nota, ating


def _calc_avaliacao(competencias, meta, real):
    """Retorna dict com nota_comp, nota_resultado, atingimento e nota_final ponderada."""
    notas = [float(v) for v in competencias.values() if v not in (None, "")]
    nota_comp = round(sum(notas) / len(notas), 2) if notas else None
    nota_res, ating = _nota_resultado(meta, real)
    if nota_comp is not None and nota_res is not None:
        nota_final = round(0.6 * nota_comp + 0.4 * nota_res, 2)  # competências 60% + resultado 40%
    elif nota_comp is not None:
        nota_final = nota_comp
    else:
        nota_final = nota_res
    return {"nota_comp": nota_comp, "nota_resultado": nota_res,
            "atingimento": ating, "nota_final": nota_final}


def _conceito(nota):
    if nota is None:
        return "—"
    if nota >= 4.5:
        return "Excelente"
    if nota >= 4.0:
        return "Acima do esperado"
    if nota >= 3.0:
        return "Dentro do esperado"
    if nota >= 2.0:
        return "Abaixo do esperado"
    return "Crítico"


AVAL_HTML = r"""
{% extends "base.html" %}
{% block title %}Avaliações de desempenho{% endblock %}
{% block body %}
<style>
  .a-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .a-kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .a-kpi b{display:block;font-size:26px;color:var(--brand2);line-height:1.1}
  .a-kpi span{font-size:12px;color:var(--muted)}
  table.a{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
  table.a th,table.a td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
  table.a th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
  .nota{font-weight:800;color:var(--brand2)}
  .a-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:12px}
  .a-form .f{display:flex;flex-direction:column;gap:4px}
  .a-form label{font-size:11px;color:var(--muted);font-weight:600}
  .a-form input,.a-form select,.a-form textarea{padding:8px 10px;border-radius:9px;border:1px solid var(--line);
    background:var(--panel2);color:var(--txt);font-size:14px;font-family:inherit}
  .a-form textarea{min-height:60px;resize:vertical}
  .comp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:8px}
  .g-btn{padding:9px 16px;border-radius:9px;border:0;background:var(--brand);color:#13270a;font-weight:800;cursor:pointer;font-size:14px}
  .g-del{color:#f29a90;background:none;border:0;cursor:pointer;font-size:12.5px;padding:0}
  .full{grid-column:1/-1}
  .msg{padding:10px 12px;border-radius:10px;margin-bottom:12px;font-size:13.5px}
  .msg.ok{background:rgba(46,204,113,.12);color:#7ee2a8;border:1px solid rgba(46,204,113,.3)}
  .msg.err{background:rgba(231,76,60,.12);color:#f29a90;border:1px solid rgba(231,76,60,.3)}
</style>

<div class="card">
  <h1>Avaliações de desempenho</h1>
  <p class="muted">Nota final = Competências (60%) + Resultado/meta (40%). Sem meta em R$, a nota é só das competências.
    <a href="{{ url_for('avaliacoes_csv') }}">⬇ Exportar CSV</a></p>
</div>

{% if ok %}<div class="msg ok">{{ ok }}</div>{% endif %}
{% if erro %}<div class="msg err">{{ erro }}</div>{% endif %}

<div class="a-kpis">
  <div class="a-kpi"><b>{{ avals|length }}</b><span>avaliações registradas</span></div>
  <div class="a-kpi"><b>{{ media_geral or '—' }}</b><span>nota média geral</span></div>
  <div class="a-kpi"><b>{{ colabs|length }}</b><span>colaboradores ativos</span></div>
</div>

<div class="card">
  <h2>Histórico</h2>
  <form method="get" action="{{ url_for('avaliacoes') }}" class="a-form" style="margin:0 0 8px">
    <div class="f"><label>Filtrar por loja</label>
      <select name="loja" onchange="this.form.submit()">
        <option value="">Todas as lojas</option>
        {% for l in lojas %}<option value="{{ l.id }}" {{ 'selected' if flt_loja==l.id|string else '' }}>{{ l.nome }}</option>{% endfor %}
      </select></div>
  </form>
  <table class="a">
    <thead><tr><th>Colaborador</th><th>Período</th><th>Tipo</th><th>Atingim.</th><th>Nota final</th><th>Conceito</th><th>Ações</th></tr></thead>
    <tbody>
    {% for a in avals %}
      <tr>
        <td><b>{{ a.colab_nome or '—' }}</b><br><span class="muted" style="font-size:12px">{{ a.loja_nome or '' }}</span></td>
        <td>{{ a.periodo or '—' }}</td>
        <td>{{ a.tipo or '—' }}</td>
        <td>{% if a.atingimento %}{{ '%.0f'|format(a.atingimento) }}%{% else %}—{% endif %}</td>
        <td class="nota">{{ a.nota_final if a.nota_final is not none else '—' }}</td>
        <td>{{ a.conceito }}</td>
        <td style="white-space:nowrap">
          <a href="{{ url_for('avaliacao_ver', aid=a.id) }}">ver</a> ·
          <a href="?edit={{ a.id }}#nova">editar</a> ·
          <form method="post" style="display:inline" action="{{ url_for('avaliacao_del', aid=a.id) }}" onsubmit="return confirm('Excluir esta avaliação?')"><input type="hidden" name="_csrf" value="{{ csrf_token }}"><button class="g-del">excluir</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="7" class="muted">Nenhuma avaliação registrada{% if flt_loja %} para esta loja{% endif %}.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% if truncado %}<p class="muted" style="font-size:12.5px;margin-top:8px">Mostrando as 100 mais recentes de {{ total_avals }}. <a href="?todos=1{% if flt_loja %}&loja={{ flt_loja }}{% endif %}">Ver todas</a></p>{% endif %}
</div>

<div class="card">
  <h2 id="nova">{{ '✏️ Editar avaliação' if edit_av else '➕ Nova avaliação' }}</h2>
  {% if colabs %}
  <form method="post" action="{{ url_for('avaliacao_nova') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token }}">
    {% if edit_av %}<input type="hidden" name="id" value="{{ edit_av.id }}">{% endif %}
    <div class="a-form">
      <div class="f"><label>Colaborador *</label>
        <select name="colaborador_id" required>
          <option value="">—</option>
          {% for c in colabs %}<option value="{{ c.id }}" {{ 'selected' if edit_av and edit_av.colaborador_id==c.id else '' }}>{{ c.nome }}{% if c.loja_nome %} · {{ c.loja_nome }}{% endif %}</option>{% endfor %}
        </select></div>
      <div class="f"><label>Tipo</label>
        <select name="tipo">{% for t in tipos %}<option {{ 'selected' if edit_av and edit_av.tipo==t else '' }}>{{ t }}</option>{% endfor %}</select></div>
      <div class="f"><label>Período</label><input name="periodo" value="{{ edit_av.periodo if edit_av else '' }}" placeholder="ex: jun/2026" required></div>
      <div class="f"><label>Meta R$ (opcional)</label><input name="kpi_meta" value="{{ edit_av.kpi_meta if edit_av else '' }}" inputmode="decimal" placeholder="ex: 40000"></div>
      <div class="f"><label>Realizado R$ (opcional)</label><input name="kpi_real" value="{{ edit_av.kpi_real if edit_av else '' }}" inputmode="decimal" placeholder="ex: 38000"></div>
    </div>

    <p class="muted" style="margin:16px 0 0;font-weight:600">Competências (1 = muito abaixo · 5 = excelente)</p>
    <div class="comp-grid">
      {% for comp in competencias %}
      <div class="f"><label>{{ comp }}</label>
        <select name="comp__{{ loop.index0 }}">
          <option value="">—</option>
          {% for n in [1,2,3,4,5] %}<option value="{{ n }}" {{ 'selected' if edit_comp.get(comp)==n else '' }}>{{ n }}</option>{% endfor %}
        </select></div>
      {% endfor %}
    </div>

    <div class="a-form" style="margin-top:14px">
      <div class="f full"><label>Pontos fortes</label><textarea name="pontos_fortes">{{ edit_av.pontos_fortes if edit_av else '' }}</textarea></div>
      <div class="f full"><label>A desenvolver</label><textarea name="a_desenvolver">{{ edit_av.a_desenvolver if edit_av else '' }}</textarea></div>
      <div class="f full"><label>Plano de desenvolvimento (PDI)</label><textarea name="plano">{{ edit_av.plano if edit_av else '' }}</textarea></div>
    </div>
    <div style="margin-top:14px"><button class="g-btn" type="submit">{{ 'Salvar alterações' if edit_av else 'Salvar avaliação' }}</button>
      {% if edit_av %}<a class="g-del" style="text-decoration:none;margin-left:10px" href="{{ url_for('avaliacoes') }}">cancelar</a>{% endif %}</div>
  </form>
  {% else %}
    <p class="muted">Cadastre colaboradores em <a href="/gestao#colaboradores">Gestão</a> antes de avaliar.</p>
  {% endif %}
</div>
{% endblock %}
"""


AVAL_VER_HTML = r"""
{% extends "base.html" %}
{% block title %}Avaliação — {{ a.colab_nome }}{% endblock %}
{% block body %}
<style>
  table.a{width:100%;border-collapse:collapse;font-size:14px;margin-top:6px}
  table.a th,table.a td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
  table.a th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
  .big{font-size:34px;font-weight:800;color:var(--brand2)}
  .blk{white-space:pre-wrap;color:var(--txt)}
</style>
<div class="card">
  <p><a href="{{ url_for('avaliacoes') }}">← Voltar</a></p>
  <h1>{{ a.colab_nome or '—' }}</h1>
  <p class="muted">{{ a.loja_nome or '' }} · {{ a.tipo or '' }} · {{ a.periodo or '' }}</p>
  <p class="big">{{ a.nota_final if a.nota_final is not none else '—' }} <span style="font-size:16px;color:var(--muted)">/ 5 · {{ a.conceito }}</span></p>
</div>

<div class="card">
  <h2>Composição</h2>
  <table class="a">
    <tr><th>Competências (60%)</th><td>{{ a.nota_comp if a.nota_comp is not none else '—' }}</td></tr>
    <tr><th>Resultado/meta (40%)</th><td>
      {% if a.nota_resultado is not none %}{{ a.nota_resultado }} (atingimento {{ '%.0f'|format(a.atingimento) }}% — meta R$ {{ a.kpi_meta }} / realizado R$ {{ a.kpi_real }}){% else %}— (sem meta){% endif %}
    </td></tr>
  </table>
  <h2 style="margin-top:16px">Competências</h2>
  <table class="a">
    {% for nome, nota in comp_itens %}<tr><th style="text-transform:none">{{ nome }}</th><td>{{ nota or '—' }}</td></tr>{% endfor %}
  </table>
</div>

<div class="card">
  <h2>Feedback</h2>
  <p class="muted">Pontos fortes</p><p class="blk">{{ a.pontos_fortes or '—' }}</p>
  <p class="muted" style="margin-top:12px">A desenvolver</p><p class="blk">{{ a.a_desenvolver or '—' }}</p>
  <p class="muted" style="margin-top:12px">Plano de desenvolvimento</p><p class="blk">{{ a.plano or '—' }}</p>
</div>
{% endblock %}
"""


# ----------------------------------------------------------------------------
# Advertências / Disciplina — helpers, termo juridicamente correto e templates (Fase 3.3)
# ----------------------------------------------------------------------------
ADV_TIPOS = [("verbal", "Advertência verbal (registro interno)"),
             ("escrita", "Advertência escrita"),
             ("suspensao", "Suspensão disciplinar")]
ADV_TIPO_LABEL = {k: v for k, v in ADV_TIPOS}


def _fmt_data_br(s):
    d = _parse_date(s)
    return d.strftime("%d/%m/%Y") if d else (s or "__/__/____")


def _termo_advertencia(adv, colab_nome, loja_nome):
    """Reproduz o texto-modelo do corpo da advertência, com as correções jurídicas
    (gradação só cita justa causa/art.482 em suspensão ou reincidência; verbal = registro interno)."""
    loja = loja_nome or "____________"
    nome = colab_nome or "____________"
    cargo = adv.get("cargo") or "____________"
    dtf = _fmt_data_br(adv.get("data_fato"))
    hr = adv.get("hora_fato") or "__:__"
    local = adv.get("local") or "____________"
    desc = adv.get("descricao") or "(descrever o ocorrido)"
    tipo = adv.get("tipo")
    is_verbal = tipo == "verbal"
    reincid = bool((adv.get("antecedentes") or "").strip())
    if is_verbal:
        medida = "advertência verbal"
    elif tipo == "suspensao":
        dias = adv.get("sus_dias") or "__"
        medida = "suspensão disciplinar de %s dia(s) (art. 474 CLT)" % dias
    else:
        medida = "advertência escrita"
    if tipo == "suspensao" or reincid:
        escalon = ("Fica o(a) colaborador(a) ciente de que a reincidência ou a prática de nova falta "
                   "poderá ensejar medida disciplinar mais grave, inclusive a rescisão do contrato por "
                   "justa causa (art. 482 da CLT).")
    else:
        escalon = ("Esta medida tem caráter estritamente pedagógico. A repetição da conduta poderá "
                   "ensejar medida disciplinar mais grave.")
    if is_verbal:
        return ("REGISTRO INTERNO DE ADVERTÊNCIA VERBAL — uso interno de gestão "
                "(não requer assinatura do colaborador).\n\n"
                "Unidade %s. Colaborador(a) %s, função %s.\n"
                "No dia %s, por volta das %s, no local %s, ocorreu: %s\n\n"
                "Foi feita orientação verbal ao(à) colaborador(a) sobre a conduta. %s\n\n"
                "Registrado por: ____________________ (gestor/RH) em ___/___/_____."
                % (loja, nome, cargo, dtf, hr, local, desc, escalon))
    return ("A SWN · Premium Outlets, por meio da unidade %s, comunica ao(à) colaborador(a) %s, "
            "na função de %s, a aplicação de %s em razão do fato a seguir:\n\n"
            "No dia %s, por volta das %s, no local %s, ocorreu: %s\n\n"
            "A conduta contraria a(s) regra(s) interna(s) da empresa e/ou dispositivo da CLT. %s\n\n"
            "Solicitamos a ciência abaixo. A assinatura atesta apenas o recebimento, não significando "
            "concordância com o teor."
            % (loja, nome, cargo, medida, dtf, hr, local, desc, escalon))


DISCIPLINA_HTML = r"""
{% extends "base.html" %}
{% block title %}Advertências & Disciplina{% endblock %}
{% block body %}
<style>
  .d-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .d-kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .d-kpi b{display:block;font-size:26px;color:var(--brand2);line-height:1.1}
  .d-kpi span{font-size:12px;color:var(--muted)}
  table.d{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
  table.d th,table.d td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
  table.d th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  .tag{font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px}
  .tag.verbal{background:rgba(241,196,15,.15);color:#f3d35e}
  .tag.escrita{background:rgba(230,126,34,.18);color:#f0a868}
  .tag.suspensao{background:rgba(231,76,60,.15);color:#f29a90}
  .d-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:12px}
  .d-form .f{display:flex;flex-direction:column;gap:4px}
  .d-form label{font-size:11px;color:var(--muted);font-weight:600}
  .d-form input,.d-form select,.d-form textarea{padding:8px 10px;border-radius:9px;border:1px solid var(--line);
    background:var(--panel2);color:var(--txt);font-size:14px;font-family:inherit}
  .d-form textarea{min-height:60px;resize:vertical}
  .g-btn{padding:9px 16px;border-radius:9px;border:0;background:var(--brand);color:#13270a;font-weight:800;cursor:pointer;font-size:14px}
  .g-del{color:#f29a90;background:none;border:0;cursor:pointer;font-size:12.5px;padding:0}
  .full{grid-column:1/-1}
  .msg{padding:10px 12px;border-radius:10px;margin-bottom:12px;font-size:13.5px}
  .msg.ok{background:rgba(46,204,113,.12);color:#7ee2a8;border:1px solid rgba(46,204,113,.3)}
  .aviso{background:linear-gradient(135deg,rgba(241,196,15,.08),rgba(231,76,60,.10));
    border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:var(--muted);font-size:13px;margin-bottom:18px}
</style>

<div class="card">
  <h1>Advertências & Disciplina</h1>
  <p class="muted">Registro de medidas disciplinares salvo no banco, com histórico por colaborador e termo pronto para impressão.
    <a href="{{ url_for('disciplina_csv') }}">⬇ Exportar CSV</a></p>
</div>

<div class="aviso">⚠️ <b>Modelo-base.</b> Advertência e suspensão devem respeitar a <b>CLT</b> e a <b>Convenção Coletiva (CCT)</b> da base de cada loja — confirme com a contabilidade/jurídico. A ciência do colaborador atesta apenas o recebimento, não concordância.</div>

{% if ok %}<div class="msg ok">{{ ok }}</div>{% endif %}

<div class="d-kpis">
  <div class="d-kpi"><b>{{ advs|length }}</b><span>registros</span></div>
  <div class="d-kpi"><b>{{ n_verbais }}</b><span>verbais</span></div>
  <div class="d-kpi"><b>{{ n_escritas }}</b><span>escritas</span></div>
  <div class="d-kpi"><b>{{ n_susp }}</b><span>suspensões</span></div>
</div>

<div class="card">
  <h2>Histórico</h2>
  <form method="get" action="{{ url_for('disciplina') }}" class="d-form" style="margin:0 0 8px">
    <div class="f"><label>Filtrar por loja</label>
      <select name="loja" onchange="this.form.submit()">
        <option value="">Todas as lojas</option>
        {% for l in lojas %}<option value="{{ l.id }}" {{ 'selected' if flt_loja==l.id|string else '' }}>{{ l.nome }}</option>{% endfor %}
      </select></div>
  </form>
  <table class="d">
    <thead><tr><th>Colaborador</th><th>Data do fato</th><th>Tipo</th><th>Motivo</th><th>Ações</th></tr></thead>
    <tbody>
    {% for a in advs %}
      <tr>
        <td><b>{{ a.colab_nome or '—' }}</b><br><span class="muted" style="font-size:12px">{{ a.loja_nome or '' }}</span></td>
        <td>{{ a.data_fato_br }}</td>
        <td><span class="tag {{ a.tipo }}">{{ a.tipo_label }}</span></td>
        <td>{{ (a.descricao or '')[:60] }}{% if a.descricao and a.descricao|length > 60 %}…{% endif %}</td>
        <td style="white-space:nowrap">
          <a href="{{ url_for('disciplina_ver', aid=a.id) }}">ver termo</a> ·
          <a href="?edit={{ a.id }}#nova">editar</a> ·
          <form method="post" style="display:inline" action="{{ url_for('disciplina_del', aid=a.id) }}" onsubmit="return confirm('Excluir este registro?')"><input type="hidden" name="_csrf" value="{{ csrf_token }}"><button class="g-del">excluir</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="5" class="muted">Nenhum registro{% if flt_loja %} para esta loja{% endif %}.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% if truncado %}<p class="muted" style="font-size:12.5px;margin-top:8px">Mostrando os 100 mais recentes de {{ total_advs }}. <a href="?todos=1{% if flt_loja %}&loja={{ flt_loja }}{% endif %}">Ver todos</a></p>{% endif %}
</div>

<div class="card">
  <h2 id="nova">{{ '✏️ Editar registro disciplinar' if edit_adv else '➕ Novo registro disciplinar' }}</h2>
  {% if colabs %}
  <form method="post" action="{{ url_for('disciplina_nova') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token }}">
    {% if edit_adv %}<input type="hidden" name="id" value="{{ edit_adv.id }}">{% endif %}
    <div class="d-form">
      <div class="f"><label>Colaborador *</label>
        <select name="colaborador_id" required>
          <option value="">—</option>
          {% for c in colabs %}<option value="{{ c.id }}" {{ 'selected' if edit_adv and edit_adv.colaborador_id==c.id else '' }}>{{ c.nome }}{% if c.loja_nome %} · {{ c.loja_nome }}{% endif %}</option>{% endfor %}
        </select></div>
      <div class="f"><label>Tipo *</label>
        <select name="tipo" required>{% for k, lbl in tipos %}<option value="{{ k }}" {{ 'selected' if edit_adv and edit_adv.tipo==k else '' }}>{{ lbl }}</option>{% endfor %}</select></div>
      <div class="f"><label>Dias de suspensão (só p/ suspensão)</label><input name="sus_dias" value="{{ edit_adv.sus_dias if edit_adv else '' }}" inputmode="numeric" placeholder="ex: 1"></div>
      <div class="f"><label>Data do fato *</label><input type="date" name="data_fato" value="{{ edit_adv.data_fato if edit_adv else '' }}" required></div>
      <div class="f"><label>Hora do fato</label><input name="hora_fato" value="{{ edit_adv.hora_fato if edit_adv else '' }}" placeholder="ex: 14:30"></div>
      <div class="f"><label>Local</label><input name="local" value="{{ edit_adv.local if edit_adv else '' }}" placeholder="ex: caixa da loja"></div>
    </div>
    <div class="d-form" style="margin-top:12px">
      <div class="f full"><label>Descrição do fato *</label><textarea name="descricao" required placeholder="O que ocorreu, com data/hora e a regra interna ou dispositivo da CLT violado.">{{ edit_adv.descricao if edit_adv else '' }}</textarea></div>
      <div class="f full"><label>Regra interna / dispositivo violado</label><input name="regra" value="{{ edit_adv.regra if edit_adv else '' }}" placeholder="ex.: item X do Manual de Caixa / art. 482 CLT"></div>
      <div class="f full"><label>Antecedentes (se reincidência: citar advertência anterior e data)</label><textarea name="antecedentes" placeholder="ex.: Advertência verbal em ../../.. pelo mesmo tipo de conduta.">{{ edit_adv.antecedentes if edit_adv else '' }}</textarea></div>
    </div>
    <div class="d-form" style="margin-top:12px">
      <div class="f"><label>Ciência (medida formal)</label>
        <select name="ciencia">
          <option value="">—</option>
          <option value="assinou" {{ 'selected' if edit_adv and edit_adv.ciencia=='assinou' else '' }}>Deu ciência e assinou</option>
          <option value="recusou" {{ 'selected' if edit_adv and edit_adv.ciencia=='recusou' else '' }}>Recusou-se a assinar (2 testemunhas)</option>
        </select></div>
      <div class="f"><label>Testemunha 1 (nome / CPF)</label><input name="testemunha1" value="{{ edit_adv.testemunha1 if edit_adv else '' }}"></div>
      <div class="f"><label>Testemunha 2 (nome / CPF)</label><input name="testemunha2" value="{{ edit_adv.testemunha2 if edit_adv else '' }}"></div>
    </div>
    <p class="muted" style="font-size:12.5px;margin-top:10px">Na <b>verbal</b>, não se colhe assinatura — é registro interno de gestão. Suspensão e reincidência citam a possibilidade de justa causa (art. 482).</p>
    <div style="margin-top:6px"><button class="g-btn" type="submit">{{ 'Salvar alterações' if edit_adv else 'Registrar' }}</button>
      {% if edit_adv %}<a class="g-del" style="text-decoration:none;margin-left:10px" href="{{ url_for('disciplina') }}">cancelar</a>{% endif %}</div>
  </form>
  {% else %}
    <p class="muted">Cadastre colaboradores em <a href="/gestao#colaboradores">Gestão</a> antes de registrar.</p>
  {% endif %}
</div>
{% endblock %}
"""


DISCIPLINA_VER_HTML = r"""
{% extends "base.html" %}
{% block title %}Termo — {{ a.colab_nome }}{% endblock %}
{% block body %}
<style>
  .termo{background:#fff;color:#111;border-radius:12px;padding:26px 30px;white-space:pre-wrap;
    font-size:14.5px;line-height:1.55;font-family:Georgia,'Times New Roman',serif}
  .sign{display:flex;gap:40px;margin-top:34px;flex-wrap:wrap;color:#111}
  .sign .line{border-top:1px solid #444;padding-top:6px;font-size:12.5px;min-width:240px}
  .no-print{}
  @media print{.no-print{display:none!important}.termo{box-shadow:none}}
  .g-btn{padding:9px 16px;border-radius:9px;border:0;background:var(--brand);color:#13270a;font-weight:800;cursor:pointer;font-size:14px}
</style>
<div class="card no-print">
  <p><a href="{{ url_for('disciplina') }}">← Voltar</a></p>
  <h1>{{ a.tipo_label }} — {{ a.colab_nome or '—' }}</h1>
  <p class="muted">{{ a.loja_nome or '' }} · fato em {{ a.data_fato_br }}</p>
  <button class="g-btn" onclick="window.print()">🖨️ Imprimir termo</button>
</div>

<div class="termo">{{ termo }}{% if a.tipo != 'verbal' %}

_______________________________________________
{{ a.colab_nome }} — ciente em ___/___/_____
(a ciência não implica concordância){% if a.regra %}

Regra/dispositivo: {{ a.regra }}{% endif %}{% if a.ciencia == 'recusou' %}

Recusa de assinatura registrada com 2 testemunhas:
1) {{ a.testemunha1 or '____________ — CPF ____________' }}
2) {{ a.testemunha2 or '____________ — CPF ____________' }}{% endif %}{% endif %}</div>
{% endblock %}
"""


# ----------------------------------------------------------------------------
# Adiantamento (vale) — recibo com art. 462 da CLT e templates (Fase 3.6)
# ----------------------------------------------------------------------------
ADI_TIPOS = [("padrao", "Adiantamento padrão"), ("extra", "Vale extraordinário")]
ADI_TIPO_LABEL = {k: v for k, v in ADI_TIPOS}


def _valor_float(s):
    """Converte valor digitado (formato BR: 1.234,56) para float; 0 se inválido."""
    if s in (None, ""):
        return 0.0
    try:
        return float(str(s).replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _recibo_adiantamento(adi, colab_nome):
    nome = colab_nome or "____________________"
    valor = adi.get("valor") or "______"
    comp = adi.get("competencia") or "____________"
    return ("Eu, %s, declaro ter recebido a título de adiantamento salarial o valor de "
            "R$ %s, e AUTORIZO o desconto integral desse valor na minha folha de pagamento "
            "referente ao mês de competência %s, nos termos do art. 462 da CLT." % (nome, valor, comp))


ADIANTAMENTO_HTML = r"""
{% extends "base.html" %}
{% block title %}Adiantamentos{% endblock %}
{% block body %}
<style>
  .v-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .v-kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .v-kpi b{display:block;font-size:24px;color:var(--brand2);line-height:1.1}
  .v-kpi span{font-size:12px;color:var(--muted)}
  table.v{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
  table.v th,table.v td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
  table.v th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  .v-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:12px}
  .v-form .f{display:flex;flex-direction:column;gap:4px}
  .v-form label{font-size:11px;color:var(--muted);font-weight:600}
  .v-form input,.v-form select,.v-form textarea{padding:8px 10px;border-radius:9px;border:1px solid var(--line);
    background:var(--panel2);color:var(--txt);font-size:14px;font-family:inherit}
  .full{grid-column:1/-1}
  .g-btn{padding:9px 16px;border-radius:9px;border:0;background:var(--brand);color:#13270a;font-weight:800;cursor:pointer;font-size:14px}
  .g-del{color:#f29a90;background:none;border:0;cursor:pointer;font-size:12.5px;padding:0}
  .msg{padding:10px 12px;border-radius:10px;margin-bottom:12px;font-size:13.5px}
  .msg.ok{background:rgba(46,204,113,.12);color:#7ee2a8;border:1px solid rgba(46,204,113,.3)}
  .aviso{background:linear-gradient(135deg,rgba(180,210,51,.08),rgba(27,60,134,.10));
    border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:var(--muted);font-size:13px;margin-bottom:18px}
</style>

<div class="card">
  <h1>Adiantamentos (vale)</h1>
  <p class="muted">Registro de adiantamento salarial salvo no banco, com recibo (art. 462 CLT) pronto para impressão.
    <a href="{{ url_for('adiantamentos_csv') }}">⬇ Exportar CSV</a></p>
</div>

<div class="aviso">⚠️ O "vale" é <b>antecipação de salário</b>, descontada integral no fechamento do mês (art. 462 CLT) — não é benefício novo. Percentual usual ~40% do bruto; <b>confira a CCT</b> da base de cada loja (quando há piso/data, prevalece). A autorização assinada é obrigatória.</div>

{% if ok %}<div class="msg ok">{{ ok }}</div>{% endif %}

<div class="v-kpis">
  <div class="v-kpi"><b>{{ total_reg }}</b><span>registros</span></div>
  <div class="v-kpi"><b>R$ {{ '%.2f'|format(soma_total) }}</b><span>total geral</span></div>
  <div class="v-kpi"><b>R$ {{ '%.2f'|format(soma_mes) }}</b><span>no mês atual (por data)</span></div>
</div>

<div class="card">
  <h2>Histórico</h2>
  <form method="get" action="{{ url_for('adiantamentos') }}" class="v-form" style="margin:0 0 8px">
    <div class="f"><label>Filtrar por loja</label>
      <select name="loja" onchange="this.form.submit()">
        <option value="">Todas as lojas</option>
        {% for l in lojas %}<option value="{{ l.id }}" {{ 'selected' if flt_loja==l.id|string else '' }}>{{ l.nome }}</option>{% endfor %}
      </select></div>
  </form>
  <table class="v">
    <thead><tr><th>Colaborador</th><th>Valor</th><th>Competência</th><th>Data</th><th>Tipo</th><th>Ações</th></tr></thead>
    <tbody>
    {% for a in adis %}
      <tr>
        <td><b>{{ a.colab_nome or '—' }}</b><br><span class="muted" style="font-size:12px">{{ a.loja_nome or '' }}</span></td>
        <td>R$ {{ a.valor or '—' }}</td>
        <td>{{ a.competencia or '—' }}</td>
        <td>{{ a.data_br }}</td>
        <td>{{ a.tipo_label }}</td>
        <td style="white-space:nowrap">
          <a href="{{ url_for('adiantamento_ver', aid=a.id) }}">recibo</a> ·
          <a href="?edit={{ a.id }}#nova">editar</a> ·
          <form method="post" style="display:inline" action="{{ url_for('adiantamento_del', aid=a.id) }}" onsubmit="return confirm('Excluir este adiantamento?')"><input type="hidden" name="_csrf" value="{{ csrf_token }}"><button class="g-del">excluir</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="6" class="muted">Nenhum adiantamento{% if flt_loja %} para esta loja{% endif %}.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% if truncado %}<p class="muted" style="font-size:12.5px;margin-top:8px">Mostrando os 100 mais recentes de {{ total_reg }}. <a href="?todos=1{% if flt_loja %}&loja={{ flt_loja }}{% endif %}">Ver todos</a></p>{% endif %}
</div>

<div class="card">
  <h2 id="nova">{{ '✏️ Editar adiantamento' if edit_adi else '➕ Novo adiantamento' }}</h2>
  {% if colabs %}
  <form method="post" action="{{ url_for('adiantamento_nova') }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token }}">
    {% if edit_adi %}<input type="hidden" name="id" value="{{ edit_adi.id }}">{% endif %}
    <div class="v-form">
      <div class="f"><label>Colaborador *</label>
        <select name="colaborador_id" required>
          <option value="">—</option>
          {% for c in colabs %}<option value="{{ c.id }}" {{ 'selected' if edit_adi and edit_adi.colaborador_id==c.id else '' }}>{{ c.nome }}{% if c.loja_nome %} · {{ c.loja_nome }}{% endif %}</option>{% endfor %}
        </select></div>
      <div class="f"><label>Valor R$ *</label><input name="valor" value="{{ edit_adi.valor if edit_adi else '' }}" inputmode="decimal" placeholder="ex: 1234,56" required></div>
      <div class="f"><label>Competência *</label><input name="competencia" value="{{ edit_adi.competencia if edit_adi else '' }}" placeholder="ex: jun/2026" required></div>
      <div class="f"><label>Data do pagamento</label><input type="date" name="data" value="{{ edit_adi.data if edit_adi else '' }}"></div>
      <div class="f"><label>Tipo</label>
        <select name="tipo">{% for k, lbl in tipos %}<option value="{{ k }}" {{ 'selected' if edit_adi and edit_adi.tipo==k else '' }}>{{ lbl }}</option>{% endfor %}</select></div>
    </div>
    <div class="v-form" style="margin-top:12px">
      <div class="f full"><label>Observação</label><input name="observacao" value="{{ edit_adi.observacao if edit_adi else '' }}" placeholder="ex: vale emergencial aprovado por ..."></div>
    </div>
    <div style="margin-top:12px"><button class="g-btn" type="submit">{{ 'Salvar alterações' if edit_adi else 'Registrar' }}</button>
      {% if edit_adi %}<a class="g-del" style="text-decoration:none;margin-left:10px" href="{{ url_for('adiantamentos') }}">cancelar</a>{% endif %}</div>
  </form>
  {% else %}
    <p class="muted">Cadastre colaboradores em <a href="/gestao#colaboradores">Gestão</a> antes de lançar adiantamento.</p>
  {% endif %}
</div>
{% endblock %}
"""


ADIANTAMENTO_VER_HTML = r"""
{% extends "base.html" %}
{% block title %}Recibo — {{ a.colab_nome }}{% endblock %}
{% block body %}
<style>
  .termo{background:#fff;color:#111;border-radius:12px;padding:26px 30px;white-space:pre-wrap;
    font-size:14.5px;line-height:1.6;font-family:Georgia,'Times New Roman',serif}
  .sign{display:flex;gap:40px;margin-top:40px;flex-wrap:wrap;color:#111}
  .sign .line{border-top:1px solid #444;padding-top:6px;font-size:12.5px;min-width:240px}
  @media print{.no-print{display:none!important}.termo{box-shadow:none}}
  .g-btn{padding:9px 16px;border-radius:9px;border:0;background:var(--brand);color:#13270a;font-weight:800;cursor:pointer;font-size:14px}
</style>
<div class="card no-print">
  <p><a href="{{ url_for('adiantamentos') }}">← Voltar</a></p>
  <h1>Recibo de adiantamento — {{ a.colab_nome or '—' }}</h1>
  <p class="muted">{{ a.loja_nome or '' }} · R$ {{ a.valor }} · competência {{ a.competencia }}</p>
  <button class="g-btn" onclick="window.print()">🖨️ Imprimir recibo</button>
</div>

<div class="termo">RECIBO DE ADIANTAMENTO SALARIAL

{{ termo }}{% if a.observacao %}

Obs.: {{ a.observacao }}{% endif %}

Local e data: ____________________, {{ a.data_br }}

<div class="sign">
  <div class="line">{{ a.colab_nome }} — autorizo o desconto</div>
  <div class="line">Responsável pelo pagamento (RH/DP)</div>
</div></div>
<p class="muted no-print" style="font-size:12.5px;margin-top:12px">Emitir em duas vias (empresa e colaborador) ou via eletrônica com aceite registrado. A rubrica "Adiantamento" deve constar no holerite.</p>
{% endblock %}
"""


# ----------------------------------------------------------------------------
# Cockpit do dono — agrega os dados que o portal já tem por loja (Fase 3.4)
# ----------------------------------------------------------------------------
def _is_mes_atual(s, hoje):
    d = _parse_date(s)
    return bool(d and d.year == hoje.year and d.month == hoje.month)


COCKPIT_HTML = r"""
{% extends "base.html" %}
{% block title %}Cockpit do Dono{% endblock %}
{% block body %}
<style>
  .c-top{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .c-top .k{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .c-top .k b{display:block;font-size:28px;color:var(--brand2);line-height:1.1}
  .c-top .k span{font-size:12px;color:var(--muted)}
  .loja-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
  .loja{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}
  .loja h3{margin:0 0 2px;font-size:17px}
  .loja .uf{font-size:12px;color:var(--muted)}
  .mini{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:12px}
  .mini div{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
  .mini b{display:block;font-size:19px;color:var(--txt)}
  .mini span{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
  .alert{margin-top:10px;font-size:12.5px;padding:6px 10px;border-radius:8px}
  .alert.warn{background:rgba(231,76,60,.13);color:#f29a90;border:1px solid rgba(231,76,60,.25)}
  .alert.ok{background:rgba(46,204,113,.1);color:#7ee2a8;border:1px solid rgba(46,204,113,.22)}
  .links{margin-top:12px;font-size:12.5px;display:flex;gap:12px;flex-wrap:wrap}
</style>

<div class="card">
  <h1>Cockpit do Dono</h1>
  <p class="muted">Visão por loja com os dados que o portal já controla: gente, avaliações, disciplina e checklist diário.
    O caixa não entra aqui (é app separado).</p>
</div>

<div class="c-top">
  <div class="k"><b>{{ rede.lojas }}</b><span>lojas</span></div>
  <div class="k"><b>{{ rede.headcount }}</b><span>colaboradores ativos</span></div>
  <div class="k"><b>{{ rede.exper }}</b><span>em experiência</span></div>
  <div class="k"><b>{{ rede.adv_mes }}</b><span>advertências no mês</span></div>
  <div class="k"><b>{{ rede.nota_media or '—' }}</b><span>nota média (avaliações)</span></div>
</div>

<div class="loja-grid">
  {% for L in lojas %}
  <div class="loja">
    <h3>{{ L.nome }}</h3>
    <div class="uf">{{ L.cidade_uf or '—' }}</div>
    <div class="mini">
      <div><b>{{ L.headcount }}</b><span>Ativos</span></div>
      <div><b>{{ L.pct_exper }}%</b><span>Experiência</span></div>
      <div><b>{{ L.turnover }}%</b><span>Turnover mês</span></div>
      <div><b>{{ L.adv_mes }}</b><span>Advert. mês</span></div>
      <div><b>{{ L.adv_total }}</b><span>Advert. total</span></div>
      <div><b>{{ L.nota_media or '—' }}</b><span>Nota média</span></div>
      <div><b>{{ L.chk_ab }}</b><span>Abertura hoje</span></div>
      <div><b>{{ L.chk_fe }}</b><span>Fechamento hoje</span></div>
    </div>
    {% for a in L.alertas %}<div class="alert warn">⚠️ {{ a }}</div>{% endfor %}
    {% if not L.alertas %}<div class="alert ok">✓ Sem alertas no momento</div>{% endif %}
    <div class="links">
      <a href="/gestao#colaboradores">Gente</a>
      <a href="/avaliacoes">Avaliações</a>
      <a href="/disciplina">Disciplina</a>
    </div>
  </div>
  {% else %}
  <div class="card"><p class="muted">Nenhuma loja cadastrada. Cadastre lojas e colaboradores em <a href="/gestao">Gestão</a> para o cockpit ganhar vida.</p></div>
  {% endfor %}
</div>
{% endblock %}
"""


# ----------------------------------------------------------------------------
# Checklist diário — itens fixos de abertura/fechamento e templates (Fase 3.5)
# ----------------------------------------------------------------------------
CHK_ITENS = {
    "abertura": [
        "Inspecionar fachada e porta antes de entrar (sinais de arrombamento)",
        "Desarmar o alarme e conferir câmeras funcionando",
        "Acender luzes e ligar ar-condicionado (registrar no controle de AC)",
        "Conferir limpeza geral: piso, provadores e banheiros",
        "Ligar música ambiente",
        "Ligar computadores, PDV/Almode e maquininhas de cartão",
        "Testar Pix e máquina de cartão com um teste rápido",
        "Conferir o fundo de troco na presença de outro responsável",
        "Abrir o caixa no sistema",
        "Definir o teto da gaveta e fazer sangria intraday ao ultrapassar",
        "Conferir vitrines e manequins",
        "Repor e organizar araras e mesas (pensar com abundância)",
        "Conferir etiquetas e preços visíveis",
        "Briefing de 5 min: metas do dia, resultado de ontem, campanhas",
        "Conferir uniforme e apresentação pessoal da equipe (dress code)",
    ],
    "fechamento": [
        "Conferir vendas do dia no sistema x formas de pagamento",
        "Comprovantes de Pix conferidos no app do banco (não aceitar print)",
        "Fazer a sangria final e a conferência de caixa no app",
        "Emitir relatório do dia e registrar o resultado",
        "Recolher peças dos provadores",
        "Reorganizar araras e mesas e repor para o dia seguinte",
        "Contar o dinheiro na presença do gerente e registrar no controle",
        "Organizar por denominação em envelope identificado (período + valor)",
        "Transporte só de 2ª a 6ª — caixa + gerente/subgerente juntos (nunca sozinho)",
        "Depositar no caixa eletrônico em horário de menor movimento",
        "Fotografar o comprovante, enviar no grupo e anexar ao controle",
        "Valor não reconhecido pelo caixa eletrônico: anotar, avisar e guardar no cofre",
        "Desligar ar-condicionado (registrar), luzes não essenciais e equipamentos",
        "Conferir que não há ninguém na loja",
        "Trancar portas, armar o alarme e conferir câmeras",
    ],
    "semanal": [
        "Montar e publicar a escala da semana seguinte (cobertura nos horários de pico)",
        "Reunião de desempenho: metas da semana e feedback da equipe",
        "Checagem de estoque: levantar reposições e rupturas",
        "Manutenção preventiva: corrigir pequenos problemas antes de virarem urgência",
        "Inventário cíclico dos itens de maior giro (curva A)",
        "Conferir etiquetas antifurto e divergências de caixa da semana",
    ],
    "mensal": [
        "Revisar metas e indicadores: faturamento, ticket médio, conversão, PA e rotação de estoque",
        "Treinamento e desenvolvimento da equipe (produto, tendências, técnicas de venda)",
        "Inventário do mês (quando aplicável) e conferência de divergências",
        "Avaliação individual de cada colaborador + plano de desenvolvimento",
        "Revisar escala, folgas e férias",
        "Fechamento financeiro com a administração",
    ],
}
CHK_TURNOS = [("abertura", "🔓 Abertura"), ("fechamento", "🌙 Fechamento"),
              ("semanal", "📅 Semanal"), ("mensal", "📈 Mensal")]
CHK_TURNO_LABEL = {k: v for k, v in CHK_TURNOS}
# Só abertura/fechamento entram no "status de hoje" e no cockpit (são diários).
CHK_TURNOS_DIARIOS = ("abertura", "fechamento")


CHECKLIST_HTML = r"""
{% extends "base.html" %}
{% block title %}Checklist diário{% endblock %}
{% block body %}
<style>
  .ck-form{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin:6px 0 4px}
  .ck-form .f{display:flex;flex-direction:column;gap:4px}
  .ck-form label{font-size:11px;color:var(--muted);font-weight:600}
  .ck-form input,.ck-form select{padding:8px 10px;border-radius:9px;border:1px solid var(--line);
    background:var(--panel2);color:var(--txt);font-size:14px}
  .g-btn{padding:9px 16px;border-radius:9px;border:0;background:var(--brand);color:#13270a;font-weight:800;cursor:pointer;font-size:14px}
  table.ck{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
  table.ck th,table.ck td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left}
  table.ck th{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  .st{font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px}
  .st.ok{background:rgba(46,204,113,.15);color:#7ee2a8}
  .st.parcial{background:rgba(241,196,15,.15);color:#f3d35e}
  .st.pendente{background:rgba(231,76,60,.15);color:#f29a90}
  .items li{list-style:none;display:flex;align-items:flex-start;gap:11px;padding:9px 10px;border-radius:9px;font-size:14px;border-bottom:1px solid var(--line)}
  .items{padding-left:0;margin:6px 0}
  .items input{margin-top:3px;width:18px;height:18px;accent-color:var(--brand)}
  .msg{padding:10px 12px;border-radius:10px;margin-bottom:12px;font-size:13.5px}
  .msg.ok{background:rgba(46,204,113,.12);color:#7ee2a8;border:1px solid rgba(46,204,113,.3)}
</style>

<div class="card">
  <h1>Checklist diário</h1>
  <p class="muted">Abertura e fechamento salvos no banco, por loja e data. O cockpit lê isto para mostrar o status de hoje.
    <a href="{{ url_for('checklist_csv') }}">⬇ Exportar CSV</a></p>
</div>

{% if ok %}<div class="msg ok">{{ ok }}</div>{% endif %}

<div class="card">
  <h2>Status de hoje ({{ hoje_br }})</h2>
  <table class="ck">
    <thead><tr><th>Loja</th><th>Abertura</th><th>Fechamento</th></tr></thead>
    <tbody>
    {% for L in status %}
      <tr>
        <td><b>{{ L.nome }}</b></td>
        <td>{% if L.ab_total %}<span class="st {{ L.ab_st }}">{{ L.ab_feitos }}/{{ L.ab_total }}</span>{% else %}<span class="st pendente">pendente</span>{% endif %}</td>
        <td>{% if L.fe_total %}<span class="st {{ L.fe_st }}">{{ L.fe_feitos }}/{{ L.fe_total }}</span>{% else %}<span class="st pendente">pendente</span>{% endif %}</td>
      </tr>
    {% else %}
      <tr><td colspan="3" class="muted">Cadastre lojas em <a href="/gestao#lojas">Gestão</a>.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

{% if lojas %}
<div class="card">
  <h2>Preencher checklist</h2>
  <form method="get" action="{{ url_for('checklist') }}" class="ck-form">
    <div class="f"><label>Loja</label>
      <select name="loja" required>
        <option value="">—</option>
        {% for l in lojas %}<option value="{{ l.id }}" {{ 'selected' if sel_loja==l.id|string else '' }}>{{ l.nome }}</option>{% endfor %}
      </select></div>
    <div class="f"><label>Turno</label>
      <select name="turno">{% for k, lbl in turnos %}<option value="{{ k }}" {{ 'selected' if sel_turno==k else '' }}>{{ lbl }}</option>{% endfor %}</select></div>
    <div class="f"><label>Data</label><input type="date" name="data" value="{{ sel_data }}"></div>
    <button class="g-btn" type="submit">Abrir checklist</button>
  </form>

  {% if itens %}
  <form method="post" action="{{ url_for('checklist_salvar') }}" style="margin-top:14px">
    <input type="hidden" name="_csrf" value="{{ csrf_token }}">
    <input type="hidden" name="loja_id" value="{{ sel_loja }}">
    <input type="hidden" name="turno" value="{{ sel_turno }}">
    <input type="hidden" name="data" value="{{ sel_data }}">
    <h3 style="margin:6px 0">{{ turno_label }} · {{ sel_loja_nome }} · {{ sel_data_br }}</h3>
    <ul class="items">
      {% for it in itens %}
      <li><input type="checkbox" name="it_{{ loop.index0 }}" id="it_{{ loop.index0 }}" {{ 'checked' if loop.index0 in feitos else '' }}>
          <label for="it_{{ loop.index0 }}">{{ it }}</label></li>
      {% endfor %}
    </ul>
    <div class="ck-form">
      <div class="f" style="flex:1"><label>Responsável</label><input name="responsavel" value="{{ responsavel }}" placeholder="quem preencheu"></div>
      <div class="f" style="flex:2"><label>Observações</label><input name="obs" value="{{ obs }}" placeholder="ocorrências do turno"></div>
    </div>
    <div style="margin-top:12px"><button class="g-btn" type="submit">Salvar checklist</button></div>
  </form>
  {% endif %}
</div>
{% else %}
<div class="card"><p class="muted">Cadastre lojas em <a href="/gestao#lojas">Gestão</a> para usar o checklist.</p></div>
{% endif %}
{% endblock %}
"""


ADMIN_USUARIOS_HTML = r"""
{% extends "base.html" %}
{% block title %}Usuários{% endblock %}
{% block body %}
  <style>
    .row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    @media(max-width:680px){.row{grid-template-columns:1fr}}
    label{display:block;font-size:13px;color:var(--muted);margin:10px 0 5px}
    input,select{width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:9px;
      background:var(--panel2);color:var(--txt);font-size:14px}
    .btn{margin-top:14px;padding:10px 16px;border:0;border-radius:9px;cursor:pointer;
      font-weight:800;background:var(--brand);color:#1a1300}
    .btn.small{margin:0;padding:6px 10px;font-size:12px}
    .btn.gray{background:var(--panel2);color:var(--txt);border:1px solid var(--line)}
    table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
    th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}
    th{color:var(--muted);font-weight:600}
    .tag{padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);background:var(--panel2)}
    .on{color:#7ee2a8}.no{color:#f29a90}
    .ok{background:rgba(46,204,113,.15);color:#7ee2a8;border:1px solid rgba(46,204,113,.3);
      padding:9px 12px;border-radius:9px;font-size:13px;margin-bottom:12px}
    .er{background:rgba(231,76,60,.15);color:#f29a90;border:1px solid rgba(231,76,60,.3);
      padding:9px 12px;border-radius:9px;font-size:13px;margin-bottom:12px}
    form.inline{display:inline}
    .tbl-wrap{overflow-x:auto}
  </style>

  <div class="card">
    <h1>Administração de usuários</h1>
    <p class="muted">Crie logins, defina o papel (que determina as ferramentas visíveis),
      ative/desative e redefina senhas. <a href="{{ url_for('admin_auditoria') }}">Ver auditoria →</a></p>
  </div>

  {% if ok %}<div class="ok">{{ ok }}</div>{% endif %}
  {% if erro %}<div class="er">{{ erro }}</div>{% endif %}

  <div class="card">
    <h2>Novo usuário</h2>
    <form method="post" action="{{ url_for('admin_usuarios') }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token }}">
      <div class="row">
        <div><label>Nome</label><input name="nome" required></div>
        <div><label>E-mail</label><input name="email" type="email" required></div>
      </div>
      <div class="row">
        <div><label>Senha inicial</label><input name="senha" type="text" required></div>
        <div>
          <label>Papel</label>
          <select name="papel" required>
            {% for pid, plabel in papeis %}<option value="{{ pid }}">{{ plabel }}</option>{% endfor %}
          </select>
        </div>
      </div>
      <div class="row">
        <div><label>Loja (opcional)</label><input name="loja_id" type="number"></div>
        <div></div>
      </div>
      <button class="btn" type="submit">Criar usuário</button>
    </form>
  </div>

  <div class="card">
    <h2>Usuários cadastrados</h2>
    <div class="tbl-wrap">
    <table>
      <thead>
        <tr><th>Nome</th><th>E-mail</th><th>Papel</th><th>Status</th><th>Último acesso</th><th>Ações</th></tr>
      </thead>
      <tbody>
        {% for u in usuarios %}
        <tr>
          <td>{{ u.nome }}</td>
          <td>{{ u.email }}</td>
          <td><span class="tag">{{ roles.get(u.papel, u.papel) }}</span></td>
          <td>{% if u.ativo %}<span class="on">ativo</span>{% else %}<span class="no">inativo</span>{% endif %}</td>
          <td class="muted">{{ u.ultimo_acesso or "—" }}</td>
          <td>
            <form class="inline" method="post" action="{{ url_for('admin_toggle', uid=u.id) }}">
              <input type="hidden" name="_csrf" value="{{ csrf_token }}">
              <button class="btn small gray" type="submit">{{ "Desativar" if u.ativo else "Ativar" }}</button>
            </form>
            <form class="inline" method="post" action="{{ url_for('admin_reset', uid=u.id) }}"
                  onsubmit="this.senha.value=prompt('Nova senha para {{ u.email }}:')||''; return !!this.senha.value;">
              <input type="hidden" name="_csrf" value="{{ csrf_token }}">
              <input type="hidden" name="senha">
              <button class="btn small gray" type="submit">Redefinir senha</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
  </div>
{% endblock %}
"""


ADMIN_AUDIT_HTML = r"""
{% extends "base.html" %}
{% block title %}Auditoria{% endblock %}
{% block body %}
<style>
  table.au{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
  table.au th,table.au td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line)}
  table.au th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  .acao{font-weight:700}
  @media(max-width:640px){table{display:block;overflow-x:auto;white-space:nowrap}}
</style>
<div class="card">
  <h1>Auditoria</h1>
  <p class="muted">Últimos {{ logs|length }} eventos registrados (logins, cadastros, edições, exclusões).
    <a href="{{ url_for('admin_usuarios') }}">← Usuários</a></p>
</div>
<div class="card">
  <table class="au">
    <thead><tr><th>Quando</th><th>Usuário</th><th>Ação</th><th>Detalhe</th><th>IP</th></tr></thead>
    <tbody>
    {% for l in logs %}
      <tr>
        <td class="muted">{{ l.criado_em or '—' }}</td>
        <td>{{ l.usuario_nome or ('#' ~ l.usuario_id if l.usuario_id else '—') }}</td>
        <td class="acao">{{ l.acao }}</td>
        <td class="muted">{{ l.detalhe or '' }}</td>
        <td class="muted">{{ l.ip or '' }}</td>
      </tr>
    {% else %}
      <tr><td colspan="5" class="muted">Sem eventos ainda.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
"""


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
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),  # expira após 12h de inatividade
    )

    app.teardown_appcontext(close_db)

    # ----------------------------------------------------------------- CSRF
    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": session.get("_csrf", "")}

    @app.before_request
    def _csrf_guard():
        # Garante um token por sessão logada.
        if "uid" in session and not session.get("_csrf"):
            session["_csrf"] = secrets.token_urlsafe(32)
        # Valida o token em qualquer POST (login é protegido por rate-limit).
        if request.method == "POST" and request.path != url_for("login"):
            if not session.get("_csrf") or request.form.get("_csrf") != session.get("_csrf"):
                abort(400)

    # ------------------------------------------------------------------ init
    with app.app_context():
        init_db()
        _seed_admin()

    # --------------------------------------------------------------- helpers
    def _now():
        return _agora_iso()

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
            ip = _client_ip()
            agora = time.time()
            fails = [t for t in _LOGIN_FAILS.get(ip, []) if agora - t < LOGIN_JANELA_SEG]
            if len(fails) >= LOGIN_MAX_FALHAS:
                _LOGIN_FAILS[ip] = fails
                audit(None, "login_bloqueado", "ip=%s" % ip)
                return render_template(
                    "login.html",
                    erro="Muitas tentativas. Aguarde alguns minutos e tente de novo."
                ), 429
            email = (request.form.get("email") or "").strip().lower()
            senha = request.form.get("senha") or ""
            user = query(
                "SELECT * FROM usuarios WHERE email = ?", (email,), one=True
            )
            if user and user.get("ativo") and check_password_hash(user["senha_hash"], senha):
                _LOGIN_FAILS.pop(ip, None)  # zera o contador ao acertar
                session.permanent = True  # aplica o PERMANENT_SESSION_LIFETIME (12h)
                set_session(user)
                session["_csrf"] = secrets.token_urlsafe(32)
                execute(
                    "UPDATE usuarios SET ultimo_acesso = ? WHERE id = ?",
                    (_now(), user["id"]),
                )
                audit(user["id"], "login", "login com sucesso")
                return redirect(url_for("portal"))
            fails.append(agora)
            _LOGIN_FAILS[ip] = fails
            audit(None, "login_falhou", "email=%s ip=%s" % (email, ip))
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
        # Gestão agora é página no servidor (Fase 3), não mais o HTML localStorage.
        if tool_id == "gestao":
            return redirect("/gestao")
        # Cockpit do dono — visão por loja (Fase 3.4).
        if tool_id == "cockpit":
            return redirect("/cockpit")
        # Checklist diário migrado para o Postgres (Fase 3.5).
        if tool_id == "checklist":
            return redirect("/checklist")
        # Avaliação de desempenho migrada para o Postgres (Fase 3.2).
        if tool_id == "avaliacao":
            return redirect("/avaliacoes")
        # Advertências migradas para o Postgres (Fase 3.3).
        if tool_id == "advertencia":
            return redirect("/disciplina")
        # Adiantamento migrado para o Postgres (Fase 3.6).
        if tool_id == "adiantamento":
            return redirect("/adiantamentos")
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
        return render_template_string(
            ADMIN_USUARIOS_HTML, user=u, usuarios=usuarios,
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

    @app.route("/admin/auditoria")
    @require_roles("admin")
    def admin_auditoria():
        logs = query(
            "SELECT a.criado_em, a.usuario_id, a.acao, a.detalhe, a.ip, u.nome AS usuario_nome "
            "FROM audit_log a LEFT JOIN usuarios u ON u.id = a.usuario_id "
            "ORDER BY a.id DESC LIMIT 200"
        )
        return render_template_string(ADMIN_AUDIT_HTML, user=current_user(), logs=logs)

    # ------------------------------------------------- Gestão (Fase 3: dados no DB)
    @app.route("/gestao")
    @require_roles("admin", "rh", "supervisor")
    def gestao():
        lojas = query("SELECT * FROM lojas ORDER BY nome")
        colabs = query(
            "SELECT c.*, l.nome AS loja_nome FROM colaboradores c "
            "LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        hoje = _hoje()
        for c in colabs:
            c["situacao"] = _situacao(c, hoje)
        rollup = {}
        for c in colabs:
            if c["situacao"] != "Desligado":
                k = c.get("loja_nome") or "(sem loja)"
                rollup[k] = rollup.get(k, 0) + 1
        ec = request.args.get("edit_colab")
        el = request.args.get("edit_loja")
        edit_colab = query("SELECT * FROM colaboradores WHERE id = ?", (ec,), one=True) if ec else None
        edit_loja = query("SELECT * FROM lojas WHERE id = ?", (el,), one=True) if el else None
        return render_template_string(
            GESTAO_HTML, user=current_user(), lojas=lojas, colabs=colabs,
            ind=_indicadores(colabs), rollup=rollup,
            edit_colab=edit_colab, edit_loja=edit_loja,
            ok=request.args.get("ok"), erro=request.args.get("erro"),
        )

    @app.route("/gestao/loja", methods=["POST"])
    @require_roles("admin", "rh")
    def gestao_loja_add():
        u = current_user()
        lid = request.form.get("id")
        nome = (request.form.get("nome") or "").strip()
        if not nome:
            return redirect("/gestao?erro=Informe o nome da loja#lojas")
        vals = (nome, request.form.get("cnpj") or "", request.form.get("cidade_uf") or "")
        if lid:
            execute("UPDATE lojas SET nome=?, cnpj=?, cidade_uf=? WHERE id=?", vals + (lid,))
            audit(u["id"], "gestao_loja_edit", nome)
            return redirect("/gestao?ok=Loja atualizada#lojas")
        execute("INSERT INTO lojas (nome, cnpj, cidade_uf, ativo) VALUES (?, ?, ?, 1)", vals)
        audit(u["id"], "gestao_loja_add", nome)
        return redirect("/gestao?ok=Loja adicionada#lojas")

    @app.route("/gestao/loja/<int:lid>/delete", methods=["POST"])
    @require_roles("admin", "rh")
    def gestao_loja_del(lid):
        u = current_user()
        # Não deixar colaboradores apontando para uma loja que deixou de existir.
        n = query("SELECT COUNT(*) AS n FROM colaboradores WHERE loja_id = ?", (lid,), one=True)
        execute("UPDATE colaboradores SET loja_id = NULL WHERE loja_id = ?", (lid,))
        execute("DELETE FROM lojas WHERE id = ?", (lid,))
        audit(u["id"], "gestao_loja_del", "loja=%s; colaboradores desvinculados=%s" % (lid, (n or {}).get("n", 0)))
        return redirect("/gestao?ok=Loja removida (colaboradores desvinculados)#lojas")

    @app.route("/gestao/colaborador", methods=["POST"])
    @require_roles("admin", "rh")
    def gestao_colab_add():
        u = current_user()
        cid = request.form.get("id")
        nome = (request.form.get("nome") or "").strip()
        if not nome:
            return redirect("/gestao?erro=Informe o nome do colaborador#colaboradores")
        vals = (nome, request.form.get("cpf") or "", request.form.get("loja_id") or None,
                request.form.get("cargo") or "", request.form.get("admissao") or "",
                request.form.get("desligamento") or "", request.form.get("contato") or "")
        if cid:
            execute(
                "UPDATE colaboradores SET nome=?, cpf=?, loja_id=?, cargo=?, "
                "admissao=?, desligamento=?, contato=? WHERE id=?",
                vals + (cid,),
            )
            audit(u["id"], "gestao_colab_edit", nome)
            return redirect("/gestao?ok=Colaborador atualizado#colaboradores")
        execute(
            "INSERT INTO colaboradores "
            "(nome, cpf, loja_id, cargo, admissao, desligamento, contato, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            vals + (_now(), u["id"]),
        )
        audit(u["id"], "gestao_colab_add", nome)
        return redirect("/gestao?ok=Colaborador adicionado#colaboradores")

    @app.route("/gestao/colaborador/<int:cid>/delete", methods=["POST"])
    @require_roles("admin", "rh")
    def gestao_colab_del(cid):
        u = current_user()
        # Remove também os registros dependentes para não deixar órfãos.
        execute("DELETE FROM avaliacoes WHERE colaborador_id = ?", (cid,))
        execute("DELETE FROM advertencias WHERE colaborador_id = ?", (cid,))
        execute("DELETE FROM colaboradores WHERE id = ?", (cid,))
        audit(u["id"], "gestao_colab_del", "colaborador=%s (avaliações e advertências removidas)" % cid)
        return redirect("/gestao?ok=Colaborador removido (avaliações e advertências também)#colaboradores")

    @app.route("/gestao/colaboradores.csv")
    @require_roles("admin", "rh", "supervisor")
    def gestao_csv():
        colabs = query(
            "SELECT c.*, l.nome AS loja_nome FROM colaboradores c "
            "LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        hoje = _hoje()
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Nome", "CPF", "Loja", "Cargo", "Admissão", "Desligamento", "Situação", "Contato"])
        for c in colabs:
            w.writerow([_csv_safe(x) for x in [
                c.get("nome"), c.get("cpf"), c.get("loja_nome") or "", c.get("cargo"),
                c.get("admissao"), c.get("desligamento"), _situacao(c, hoje), c.get("contato")]])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=colaboradores_swn.csv"},
        )

    # ------------------------------------------------- Avaliações (Fase 3.2: DB)
    def _avals_join(where="", params=()):
        return query(
            "SELECT a.*, c.nome AS colab_nome, c.loja_id AS loja_id, l.nome AS loja_nome "
            "FROM avaliacoes a "
            "LEFT JOIN colaboradores c ON c.id = a.colaborador_id "
            "LEFT JOIN lojas l ON l.id = c.loja_id "
            + where + " ORDER BY a.id DESC", params,
        )

    @app.route("/avaliacoes")
    @require_roles("admin", "rh", "gerente", "supervisor")
    def avaliacoes():
        avals = _avals_join()
        for a in avals:
            _, ating = _nota_resultado(a.get("kpi_meta"), a.get("kpi_real"))
            a["atingimento"] = ating
            a["conceito"] = _conceito(a.get("nota_final"))
        flt_loja = request.args.get("loja")
        if flt_loja:
            avals = [a for a in avals if str(a.get("loja_id")) == flt_loja]
        notas = [a["nota_final"] for a in avals if a.get("nota_final") is not None]
        media_geral = round(sum(notas) / len(notas), 2) if notas else None
        total_avals = len(avals)
        truncado = (not request.args.get("todos")) and total_avals > 100
        if truncado:
            avals = avals[:100]
        hoje = _hoje()
        colabs = query(
            "SELECT c.id, c.nome, c.admissao, c.desligamento, l.nome AS loja_nome "
            "FROM colaboradores c LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        colabs = [c for c in colabs if _situacao(c, hoje) != "Desligado"]
        lojas = query("SELECT id, nome FROM lojas ORDER BY nome")
        # edição: pré-carrega a avaliação e suas competências
        ev = request.args.get("edit")
        edit_av = None
        edit_comp = {}
        if ev:
            edit_av = query("SELECT * FROM avaliacoes WHERE id = ?", (ev,), one=True)
            if edit_av:
                try:
                    edit_comp = json.loads(edit_av.get("competencias") or "{}")
                except (ValueError, TypeError):
                    edit_comp = {}
        return render_template_string(
            AVAL_HTML, user=current_user(), avals=avals, colabs=colabs, lojas=lojas,
            competencias=AVAL_COMPETENCIAS, tipos=AVAL_TIPOS, media_geral=media_geral,
            flt_loja=flt_loja, edit_av=edit_av, edit_comp=edit_comp,
            truncado=truncado, total_avals=total_avals,
            ok=request.args.get("ok"), erro=request.args.get("erro"),
        )

    @app.route("/avaliacoes/nova", methods=["POST"])
    @require_roles("admin", "rh", "gerente", "supervisor")
    def avaliacao_nova():
        u = current_user()
        aid = request.form.get("id")
        cid = request.form.get("colaborador_id")
        if not cid:
            return redirect("/avaliacoes?erro=Selecione o colaborador#nova")
        competencias = {}
        for i, nome in enumerate(AVAL_COMPETENCIAS):
            v = request.form.get("comp__%d" % i)
            if v:
                competencias[nome] = int(v)
        meta = request.form.get("kpi_meta") or ""
        real = request.form.get("kpi_real") or ""
        calc = _calc_avaliacao(competencias, meta, real)
        comp_json = json.dumps(competencias, ensure_ascii=False)
        if aid:
            execute(
                "UPDATE avaliacoes SET colaborador_id=?, tipo=?, periodo=?, kpi_meta=?, kpi_real=?, "
                "nota_resultado=?, nota_comp=?, nota_final=?, competencias=?, "
                "pontos_fortes=?, a_desenvolver=?, plano=? WHERE id=?",
                (cid, request.form.get("tipo") or "", request.form.get("periodo") or "",
                 meta, real, calc["nota_resultado"], calc["nota_comp"], calc["nota_final"],
                 comp_json, request.form.get("pontos_fortes") or "",
                 request.form.get("a_desenvolver") or "", request.form.get("plano") or "", aid),
            )
            audit(u["id"], "avaliacao_edit", aid)
            return redirect("/avaliacoes?ok=Avaliação atualizada")
        execute(
            "INSERT INTO avaliacoes "
            "(colaborador_id, avaliador_id, tipo, periodo, kpi_meta, kpi_real, "
            " nota_resultado, nota_comp, nota_final, competencias, "
            " pontos_fortes, a_desenvolver, plano, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, u["id"], request.form.get("tipo") or "", request.form.get("periodo") or "",
             meta, real, calc["nota_resultado"], calc["nota_comp"], calc["nota_final"],
             comp_json,
             request.form.get("pontos_fortes") or "", request.form.get("a_desenvolver") or "",
             request.form.get("plano") or "", _now(), u["id"]),
        )
        audit(u["id"], "avaliacao_nova", str(cid))
        return redirect("/avaliacoes?ok=Avaliação registrada")

    @app.route("/avaliacoes/<int:aid>/delete", methods=["POST"])
    @require_roles("admin", "rh", "gerente", "supervisor")
    def avaliacao_del(aid):
        u = current_user()
        execute("DELETE FROM avaliacoes WHERE id = ?", (aid,))
        audit(u["id"], "avaliacao_del", str(aid))
        return redirect("/avaliacoes?ok=Avaliação excluída")

    @app.route("/avaliacoes/<int:aid>")
    @require_roles("admin", "rh", "gerente", "supervisor")
    def avaliacao_ver(aid):
        a = _avals_join("WHERE a.id = ?", (aid,))
        if not a:
            abort(404)
        a = a[0]
        _, ating = _nota_resultado(a.get("kpi_meta"), a.get("kpi_real"))
        a["atingimento"] = ating
        a["conceito"] = _conceito(a.get("nota_final"))
        try:
            comp = json.loads(a.get("competencias") or "{}")
        except (ValueError, TypeError):
            comp = {}
        comp_itens = [(nome, comp.get(nome)) for nome in AVAL_COMPETENCIAS]
        return render_template_string(
            AVAL_VER_HTML, user=current_user(), a=a, comp_itens=comp_itens,
        )

    @app.route("/avaliacoes.csv")
    @require_roles("admin", "rh", "gerente", "supervisor")
    def avaliacoes_csv():
        avals = _avals_join()
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Colaborador", "Loja", "Tipo", "Período", "Meta R$", "Realizado R$",
                    "Nota competências", "Nota resultado", "Nota final", "Conceito"])
        for a in avals:
            w.writerow([_csv_safe(x) for x in [
                a.get("colab_nome"), a.get("loja_nome") or "", a.get("tipo"), a.get("periodo"),
                a.get("kpi_meta"), a.get("kpi_real"), a.get("nota_comp"),
                a.get("nota_resultado"), a.get("nota_final"), _conceito(a.get("nota_final"))]])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=avaliacoes_swn.csv"},
        )

    # ------------------------------------------------- Disciplina (Fase 3.3: DB)
    def _advs_join(where="", params=()):
        rows = query(
            "SELECT a.*, c.nome AS colab_nome, c.cargo AS cargo, c.loja_id AS loja_id, l.nome AS loja_nome "
            "FROM advertencias a "
            "LEFT JOIN colaboradores c ON c.id = a.colaborador_id "
            "LEFT JOIN lojas l ON l.id = c.loja_id "
            + where + " ORDER BY a.id DESC", params,
        )
        for a in rows:
            a["data_fato_br"] = _fmt_data_br(a.get("data_fato"))
            a["tipo_label"] = ADV_TIPO_LABEL.get(a.get("tipo"), a.get("tipo") or "—")
        return rows

    @app.route("/disciplina")
    @require_roles("admin", "rh", "supervisor", "gerente")
    def disciplina():
        advs = _advs_join()
        n_verbais = sum(1 for a in advs if a.get("tipo") == "verbal")
        n_escritas = sum(1 for a in advs if a.get("tipo") == "escrita")
        n_susp = sum(1 for a in advs if a.get("tipo") == "suspensao")
        flt_loja = request.args.get("loja")
        if flt_loja:
            advs = [a for a in advs if str(a.get("loja_id")) == flt_loja]
        total_advs = len(advs)
        truncado = (not request.args.get("todos")) and total_advs > 100
        if truncado:
            advs = advs[:100]
        hoje = _hoje()
        colabs = query(
            "SELECT c.id, c.nome, c.admissao, c.desligamento, l.nome AS loja_nome "
            "FROM colaboradores c LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        colabs = [c for c in colabs if _situacao(c, hoje) != "Desligado"]
        lojas = query("SELECT id, nome FROM lojas ORDER BY nome")
        ev = request.args.get("edit")
        edit_adv = query("SELECT * FROM advertencias WHERE id = ?", (ev,), one=True) if ev else None
        return render_template_string(
            DISCIPLINA_HTML, user=current_user(), advs=advs, colabs=colabs, lojas=lojas,
            tipos=ADV_TIPOS, n_verbais=n_verbais, n_escritas=n_escritas, n_susp=n_susp,
            flt_loja=flt_loja, edit_adv=edit_adv,
            truncado=truncado, total_advs=total_advs,
            ok=request.args.get("ok"),
        )

    @app.route("/disciplina/nova", methods=["POST"])
    @require_roles("admin", "rh", "supervisor", "gerente")
    def disciplina_nova():
        u = current_user()
        aid = request.form.get("id")
        cid = request.form.get("colaborador_id")
        tipo = request.form.get("tipo") or "escrita"
        if not cid or not (request.form.get("descricao") or "").strip():
            return redirect("/disciplina?ok=Informe colaborador e descrição#nova")
        campos = (cid, tipo, request.form.get("data_fato") or "", request.form.get("hora_fato") or "",
                  request.form.get("local") or "", request.form.get("descricao") or "",
                  request.form.get("regra") or "", request.form.get("antecedentes") or "",
                  request.form.get("sus_dias") or "", request.form.get("ciencia") or "",
                  request.form.get("testemunha1") or "", request.form.get("testemunha2") or "")
        if aid:
            execute(
                "UPDATE advertencias SET colaborador_id=?, tipo=?, data_fato=?, hora_fato=?, local=?, "
                "descricao=?, regra=?, antecedentes=?, sus_dias=?, ciencia=?, testemunha1=?, testemunha2=? "
                "WHERE id=?",
                campos + (aid,),
            )
            audit(u["id"], "disciplina_edit", aid)
            return redirect("/disciplina?ok=Registro disciplinar atualizado")
        execute(
            "INSERT INTO advertencias "
            "(colaborador_id, tipo, data_fato, hora_fato, local, descricao, regra, "
            " antecedentes, sus_dias, ciencia, testemunha1, testemunha2, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            campos + (_now(), u["id"]),
        )
        audit(u["id"], "disciplina_nova", "%s/%s" % (cid, tipo))
        return redirect("/disciplina?ok=Registro disciplinar salvo")

    @app.route("/disciplina/<int:aid>/delete", methods=["POST"])
    @require_roles("admin", "rh", "supervisor", "gerente")
    def disciplina_del(aid):
        u = current_user()
        execute("DELETE FROM advertencias WHERE id = ?", (aid,))
        audit(u["id"], "disciplina_del", str(aid))
        return redirect("/disciplina?ok=Registro disciplinar excluído")

    @app.route("/disciplina/<int:aid>")
    @require_roles("admin", "rh", "supervisor", "gerente")
    def disciplina_ver(aid):
        rows = _advs_join("WHERE a.id = ?", (aid,))
        if not rows:
            abort(404)
        a = rows[0]
        termo = _termo_advertencia(a, a.get("colab_nome"), a.get("loja_nome"))
        return render_template_string(
            DISCIPLINA_VER_HTML, user=current_user(), a=a, termo=termo,
        )

    @app.route("/disciplina.csv")
    @require_roles("admin", "rh", "supervisor", "gerente")
    def disciplina_csv():
        advs = _advs_join()
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Colaborador", "Loja", "Tipo", "Data do fato", "Hora", "Local",
                    "Descrição", "Regra/dispositivo", "Antecedentes", "Ciência"])
        for a in advs:
            w.writerow([_csv_safe(x) for x in [
                a.get("colab_nome"), a.get("loja_nome") or "", a.get("tipo_label"),
                a.get("data_fato_br"), a.get("hora_fato"), a.get("local"),
                a.get("descricao"), a.get("regra"), a.get("antecedentes"), a.get("ciencia")]])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=disciplina_swn.csv"},
        )

    # ------------------------------------------------- Checklist diário (Fase 3.5)
    def _chk_st(feitos, total):
        if not total:
            return "pendente"
        if feitos >= total:
            return "ok"
        return "parcial"

    def _chk_hoje_por_loja(lojas, dia):
        """dict[loja_id] = {'abertura': (feitos,total), 'fechamento': (feitos,total)}."""
        rows = query("SELECT loja_id, turno, feitos, total FROM checklists WHERE data = ?", (dia,))
        m = {}
        for r in rows:
            m.setdefault(r["loja_id"], {})[r["turno"]] = (r.get("feitos") or 0, r.get("total") or 0)
        return m

    @app.route("/checklist")
    @require_roles("admin", "rh", "supervisor", "gerente", "subgerente", "estoquista")
    def checklist():
        hoje = _hoje()
        lojas = query("SELECT * FROM lojas ORDER BY nome")
        mapa = _chk_hoje_por_loja(lojas, hoje.isoformat())
        status = []
        for L in lojas:
            ab = mapa.get(L["id"], {}).get("abertura")
            fe = mapa.get(L["id"], {}).get("fechamento")
            status.append({
                "nome": L["nome"],
                "ab_feitos": ab[0] if ab else 0, "ab_total": ab[1] if ab else 0,
                "ab_st": _chk_st(*ab) if ab else "pendente",
                "fe_feitos": fe[0] if fe else 0, "fe_total": fe[1] if fe else 0,
                "fe_st": _chk_st(*fe) if fe else "pendente",
            })

        sel_loja = request.args.get("loja")
        sel_turno = request.args.get("turno") or "abertura"
        sel_data = request.args.get("data") or hoje.isoformat()
        itens = None
        feitos = set()
        responsavel = ""
        obs = ""
        sel_loja_nome = ""
        if sel_loja and sel_turno in CHK_ITENS:
            itens = CHK_ITENS[sel_turno]
            existing = query(
                "SELECT * FROM checklists WHERE loja_id = ? AND data = ? AND turno = ? ORDER BY id DESC",
                (sel_loja, sel_data, sel_turno), one=True,
            )
            if existing:
                try:
                    feitos = set(json.loads(existing.get("itens") or "[]"))
                except (ValueError, TypeError):
                    feitos = set()
                responsavel = existing.get("responsavel") or ""
                obs = existing.get("obs") or ""
            else:
                responsavel = (current_user() or {}).get("nome") or ""  # pré-preenche com quem está logado
            lj = query("SELECT nome FROM lojas WHERE id = ?", (sel_loja,), one=True)
            sel_loja_nome = lj["nome"] if lj else ""

        return render_template_string(
            CHECKLIST_HTML, user=current_user(), lojas=lojas, status=status,
            turnos=CHK_TURNOS, sel_loja=sel_loja, sel_turno=sel_turno, sel_data=sel_data,
            itens=itens, feitos=feitos, responsavel=responsavel, obs=obs,
            sel_loja_nome=sel_loja_nome, turno_label=CHK_TURNO_LABEL.get(sel_turno, sel_turno),
            hoje_br=hoje.strftime("%d/%m/%Y"), sel_data_br=_fmt_data_br(sel_data),
            ok=request.args.get("ok"),
        )

    @app.route("/checklist/salvar", methods=["POST"])
    @require_roles("admin", "rh", "supervisor", "gerente", "subgerente", "estoquista")
    def checklist_salvar():
        u = current_user()
        loja_id = request.form.get("loja_id")
        turno = request.form.get("turno")
        dia = request.form.get("data") or _hoje().isoformat()
        if not loja_id or turno not in CHK_ITENS:
            return redirect("/checklist?ok=Selecione loja e turno")
        itens = CHK_ITENS[turno]
        feitos = [i for i in range(len(itens)) if request.form.get("it_%d" % i)]
        payload = json.dumps(feitos)
        total = len(itens)
        existing = query(
            "SELECT id FROM checklists WHERE loja_id = ? AND data = ? AND turno = ? ORDER BY id DESC",
            (loja_id, dia, turno), one=True,
        )
        if existing:
            execute(
                "UPDATE checklists SET itens=?, feitos=?, total=?, responsavel=?, obs=?, criado_em=?, criado_por=? WHERE id=?",
                (payload, len(feitos), total, request.form.get("responsavel") or "",
                 request.form.get("obs") or "", _now(), u["id"], existing["id"]),
            )
        else:
            execute(
                "INSERT INTO checklists (loja_id, data, turno, itens, feitos, total, responsavel, obs, criado_em, criado_por) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (loja_id, dia, turno, payload, len(feitos), total,
                 request.form.get("responsavel") or "", request.form.get("obs") or "", _now(), u["id"]),
            )
        audit(u["id"], "checklist_salvar", "%s/%s/%s %d/%d" % (loja_id, turno, dia, len(feitos), total))
        return redirect("/checklist?ok=Checklist salvo (%d de %d itens)" % (len(feitos), total))

    @app.route("/checklist.csv")
    @require_roles("admin", "rh", "supervisor", "gerente", "subgerente", "estoquista")
    def checklist_csv():
        rows = query(
            "SELECT ck.*, l.nome AS loja_nome FROM checklists ck "
            "LEFT JOIN lojas l ON l.id = ck.loja_id ORDER BY ck.data DESC, l.nome"
        )
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Data", "Loja", "Turno", "Feitos", "Total", "%", "Responsável", "Observações"])
        for r in rows:
            tot = r.get("total") or 0
            pct = round((r.get("feitos") or 0) / tot * 100) if tot else 0
            w.writerow([_csv_safe(x) for x in [
                r.get("data"), r.get("loja_nome") or "", CHK_TURNO_LABEL.get(r.get("turno"), r.get("turno")),
                r.get("feitos"), r.get("total"), "%d%%" % pct, r.get("responsavel"), r.get("obs")]])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=checklists_swn.csv"},
        )

    # ------------------------------------------------- Adiantamento (Fase 3.6)
    def _adis_join(where="", params=()):
        rows = query(
            "SELECT a.*, c.nome AS colab_nome, c.loja_id AS loja_id, l.nome AS loja_nome "
            "FROM adiantamentos a "
            "LEFT JOIN colaboradores c ON c.id = a.colaborador_id "
            "LEFT JOIN lojas l ON l.id = c.loja_id "
            + where + " ORDER BY a.id DESC", params,
        )
        for a in rows:
            a["data_br"] = _fmt_data_br(a.get("data"))
            a["tipo_label"] = ADI_TIPO_LABEL.get(a.get("tipo"), a.get("tipo") or "—")
        return rows

    @app.route("/adiantamentos")
    @require_roles("admin", "rh", "financeiro")
    def adiantamentos():
        adis = _adis_join()
        hoje = _hoje()
        soma_total = sum(_valor_float(a.get("valor")) for a in adis)
        soma_mes = sum(_valor_float(a.get("valor")) for a in adis if _is_mes_atual(a.get("data"), hoje))
        flt_loja = request.args.get("loja")
        if flt_loja:
            adis = [a for a in adis if str(a.get("loja_id")) == flt_loja]
        total_reg = len(adis)
        truncado = (not request.args.get("todos")) and total_reg > 100
        if truncado:
            adis = adis[:100]
        colabs = query(
            "SELECT c.id, c.nome, c.admissao, c.desligamento, l.nome AS loja_nome "
            "FROM colaboradores c LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        colabs = [c for c in colabs if _situacao(c, hoje) != "Desligado"]
        lojas = query("SELECT id, nome FROM lojas ORDER BY nome")
        ev = request.args.get("edit")
        edit_adi = query("SELECT * FROM adiantamentos WHERE id = ?", (ev,), one=True) if ev else None
        return render_template_string(
            ADIANTAMENTO_HTML, user=current_user(), adis=adis, colabs=colabs, lojas=lojas,
            tipos=ADI_TIPOS, total_reg=total_reg, soma_total=soma_total, soma_mes=soma_mes,
            flt_loja=flt_loja, edit_adi=edit_adi, truncado=truncado,
            ok=request.args.get("ok"),
        )

    @app.route("/adiantamentos/nova", methods=["POST"])
    @require_roles("admin", "rh", "financeiro")
    def adiantamento_nova():
        u = current_user()
        aid = request.form.get("id")
        cid = request.form.get("colaborador_id")
        if not cid or not (request.form.get("valor") or "").strip():
            return redirect("/adiantamentos?ok=Informe colaborador e valor#nova")
        campos = (cid, request.form.get("valor") or "", request.form.get("competencia") or "",
                  request.form.get("data") or "", request.form.get("tipo") or "padrao",
                  request.form.get("observacao") or "")
        if aid:
            execute(
                "UPDATE adiantamentos SET colaborador_id=?, valor=?, competencia=?, data=?, tipo=?, observacao=? WHERE id=?",
                campos + (aid,),
            )
            audit(u["id"], "adiantamento_edit", aid)
            return redirect("/adiantamentos?ok=Adiantamento atualizado")
        execute(
            "INSERT INTO adiantamentos (colaborador_id, valor, competencia, data, tipo, observacao, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            campos + (_now(), u["id"]),
        )
        audit(u["id"], "adiantamento_nova", str(cid))
        return redirect("/adiantamentos?ok=Adiantamento registrado")

    @app.route("/adiantamentos/<int:aid>/delete", methods=["POST"])
    @require_roles("admin", "rh", "financeiro")
    def adiantamento_del(aid):
        u = current_user()
        execute("DELETE FROM adiantamentos WHERE id = ?", (aid,))
        audit(u["id"], "adiantamento_del", str(aid))
        return redirect("/adiantamentos?ok=Adiantamento excluído")

    @app.route("/adiantamentos/<int:aid>")
    @require_roles("admin", "rh", "financeiro")
    def adiantamento_ver(aid):
        rows = _adis_join("WHERE a.id = ?", (aid,))
        if not rows:
            abort(404)
        a = rows[0]
        termo = _recibo_adiantamento(a, a.get("colab_nome"))
        return render_template_string(ADIANTAMENTO_VER_HTML, user=current_user(), a=a, termo=termo)

    @app.route("/adiantamentos.csv")
    @require_roles("admin", "rh", "financeiro")
    def adiantamentos_csv():
        adis = _adis_join()
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Colaborador", "Loja", "Valor", "Competência", "Data", "Tipo", "Observação"])
        for a in adis:
            w.writerow([_csv_safe(x) for x in [
                a.get("colab_nome"), a.get("loja_nome") or "", a.get("valor"), a.get("competencia"),
                a.get("data_br"), a.get("tipo_label"), a.get("observacao")]])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=adiantamentos_swn.csv"},
        )

    # ------------------------------------------------- Cockpit do dono (Fase 3.4)
    @app.route("/cockpit")
    @require_roles("admin", "supervisor")
    def cockpit():
        hoje = _hoje()
        lojas_raw = query("SELECT * FROM lojas ORDER BY nome")
        colabs = query("SELECT * FROM colaboradores")
        avals = query(
            "SELECT a.nota_final, c.loja_id FROM avaliacoes a "
            "LEFT JOIN colaboradores c ON c.id = a.colaborador_id"
        )
        advs = query(
            "SELECT a.data_fato, c.loja_id FROM advertencias a "
            "LEFT JOIN colaboradores c ON c.id = a.colaborador_id"
        )
        chk_map = _chk_hoje_por_loja(lojas_raw, hoje.isoformat())
        for c in colabs:
            c["situacao"] = _situacao(c, hoje)

        lojas = []
        for L in lojas_raw:
            lid = L["id"]
            meus = [c for c in colabs if c.get("loja_id") == lid]
            ativos = [c for c in meus if c["situacao"] != "Desligado"]
            headcount = len(ativos)
            exper = sum(1 for c in ativos if c["situacao"] == "Experiência")
            desl_mes = sum(1 for c in meus if _is_mes_atual(c.get("desligamento"), hoje))
            adv_mes = sum(1 for a in advs if a.get("loja_id") == lid and _is_mes_atual(a.get("data_fato"), hoje))
            adv_total = sum(1 for a in advs if a.get("loja_id") == lid)
            notas = [a["nota_final"] for a in avals if a.get("loja_id") == lid and a.get("nota_final") is not None]
            nota_media = round(sum(notas) / len(notas), 2) if notas else None
            turnover = round(desl_mes / headcount * 100, 1) if headcount else 0
            pct_exper = round(exper / headcount * 100) if headcount else 0
            alertas = []
            if headcount == 0:
                alertas.append("Sem colaboradores ativos cadastrados")
            if turnover >= 20:
                alertas.append("Turnover do mês alto (%s%%)" % turnover)
            if adv_mes >= 3:
                alertas.append("%s advertências neste mês" % adv_mes)
            if nota_media is not None and nota_media < 3:
                alertas.append("Nota média de avaliação baixa (%s)" % nota_media)
            chk = chk_map.get(lid, {})
            ab = chk.get("abertura")
            fe = chk.get("fechamento")
            ab_txt = ("%d/%d" % ab) if ab else "—"
            fe_txt = ("%d/%d" % fe) if fe else "—"
            if headcount and not ab:
                alertas.append("Abertura de hoje não registrada")
            lojas.append({
                "nome": L["nome"], "cidade_uf": L.get("cidade_uf"),
                "headcount": headcount, "pct_exper": pct_exper, "turnover": turnover,
                "adv_mes": adv_mes, "adv_total": adv_total, "nota_media": nota_media,
                "chk_ab": ab_txt, "chk_fe": fe_txt,
                "alertas": alertas,
            })

        todas_notas = [a["nota_final"] for a in avals if a.get("nota_final") is not None]
        rede = {
            "lojas": len(lojas_raw),
            "headcount": sum(l["headcount"] for l in lojas),
            "exper": sum(1 for c in colabs if c["situacao"] == "Experiência"),
            "adv_mes": sum(1 for a in advs if _is_mes_atual(a.get("data_fato"), hoje)),
            "nota_media": round(sum(todas_notas) / len(todas_notas), 2) if todas_notas else None,
        }
        return render_template_string(COCKPIT_HTML, user=current_user(), lojas=lojas, rede=rede)

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

    @app.errorhandler(500)
    def err_500(e):
        try:
            u = current_user()
        except Exception:
            u = None
        return render_template(
            "base.html", titulo="Erro interno",
            conteudo="500 — Tivemos um problema ao processar. Tente novamente; "
                     "se continuar, avise o administrador.",
            user=u), 500

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
         1, _agora_iso()),
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
