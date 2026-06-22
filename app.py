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
import logging
from datetime import datetime, date

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
    hoje = date.today()
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
          <form method="post" style="display:inline" action="{{ url_for('gestao_colab_del', cid=c.id) }}" onsubmit="return confirm('Remover {{ c.nome }}?')"><button class="g-del">remover</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="7" class="muted">Nenhum colaborador cadastrado ainda.</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h2 style="margin-top:16px" id="form-colab">{{ '✏️ Editar colaborador' if edit_colab else '➕ Novo colaborador' }}</h2>
  <form class="g-form" method="post" action="{{ url_for('gestao_colab_add') }}">
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
          <form method="post" style="display:inline" action="{{ url_for('gestao_loja_del', lid=l.id) }}" onsubmit="return confirm('Remover {{ l.nome }}?')"><button class="g-del">remover</button></form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="4" class="muted">Nenhuma loja cadastrada. Cadastre as lojas primeiro.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  <h2 style="margin-top:16px" id="form-loja">{{ '✏️ Editar loja' if edit_loja else '➕ Nova loja' }}</h2>
  <form class="g-form" method="post" action="{{ url_for('gestao_loja_add') }}">
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
  <table class="a">
    <thead><tr><th>Colaborador</th><th>Período</th><th>Tipo</th><th>Atingim.</th><th>Nota final</th><th>Conceito</th><th></th></tr></thead>
    <tbody>
    {% for a in avals %}
      <tr>
        <td><b>{{ a.colab_nome or '—' }}</b><br><span class="muted" style="font-size:12px">{{ a.loja_nome or '' }}</span></td>
        <td>{{ a.periodo or '—' }}</td>
        <td>{{ a.tipo or '—' }}</td>
        <td>{% if a.atingimento %}{{ '%.0f'|format(a.atingimento) }}%{% else %}—{% endif %}</td>
        <td class="nota">{{ a.nota_final if a.nota_final is not none else '—' }}</td>
        <td>{{ a.conceito }}</td>
        <td><a href="{{ url_for('avaliacao_ver', aid=a.id) }}">ver</a></td>
      </tr>
    {% else %}
      <tr><td colspan="7" class="muted">Nenhuma avaliação registrada ainda.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<div class="card">
  <h2 id="nova">➕ Nova avaliação</h2>
  {% if colabs %}
  <form method="post" action="{{ url_for('avaliacao_nova') }}">
    <div class="a-form">
      <div class="f"><label>Colaborador *</label>
        <select name="colaborador_id" required>
          <option value="">—</option>
          {% for c in colabs %}<option value="{{ c.id }}">{{ c.nome }}{% if c.loja_nome %} · {{ c.loja_nome }}{% endif %}</option>{% endfor %}
        </select></div>
      <div class="f"><label>Tipo</label>
        <select name="tipo">{% for t in tipos %}<option>{{ t }}</option>{% endfor %}</select></div>
      <div class="f"><label>Período</label><input name="periodo" placeholder="ex: jun/2026" required></div>
      <div class="f"><label>Meta R$ (opcional)</label><input name="kpi_meta" inputmode="decimal" placeholder="ex: 40000"></div>
      <div class="f"><label>Realizado R$ (opcional)</label><input name="kpi_real" inputmode="decimal" placeholder="ex: 38000"></div>
    </div>

    <p class="muted" style="margin:16px 0 0;font-weight:600">Competências (1 = muito abaixo · 5 = excelente)</p>
    <div class="comp-grid">
      {% for comp in competencias %}
      <div class="f"><label>{{ comp }}</label>
        <select name="comp__{{ loop.index0 }}">
          <option value="">—</option>
          {% for n in [1,2,3,4,5] %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
        </select></div>
      {% endfor %}
    </div>

    <div class="a-form" style="margin-top:14px">
      <div class="f full"><label>Pontos fortes</label><textarea name="pontos_fortes"></textarea></div>
      <div class="f full"><label>A desenvolver</label><textarea name="a_desenvolver"></textarea></div>
      <div class="f full"><label>Plano de desenvolvimento (PDI)</label><textarea name="plano"></textarea></div>
    </div>
    <div style="margin-top:14px"><button class="g-btn" type="submit">Salvar avaliação</button></div>
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
        # Gestão agora é página no servidor (Fase 3), não mais o HTML localStorage.
        if tool_id == "gestao":
            return redirect("/gestao")
        # Avaliação de desempenho migrada para o Postgres (Fase 3.2).
        if tool_id == "avaliacao":
            return redirect("/avaliacoes")
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

    # ------------------------------------------------- Gestão (Fase 3: dados no DB)
    @app.route("/gestao")
    @require_roles("admin", "rh", "supervisor")
    def gestao():
        lojas = query("SELECT * FROM lojas ORDER BY nome")
        colabs = query(
            "SELECT c.*, l.nome AS loja_nome FROM colaboradores c "
            "LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        hoje = date.today()
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
        execute("DELETE FROM lojas WHERE id = ?", (lid,))
        audit(u["id"], "gestao_loja_del", str(lid))
        return redirect("/gestao?ok=Loja removida#lojas")

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
        execute("DELETE FROM colaboradores WHERE id = ?", (cid,))
        audit(u["id"], "gestao_colab_del", str(cid))
        return redirect("/gestao?ok=Colaborador removido#colaboradores")

    @app.route("/gestao/colaboradores.csv")
    @require_roles("admin", "rh", "supervisor")
    def gestao_csv():
        colabs = query(
            "SELECT c.*, l.nome AS loja_nome FROM colaboradores c "
            "LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        hoje = date.today()
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Nome", "CPF", "Loja", "Cargo", "Admissão", "Desligamento", "Situação", "Contato"])
        for c in colabs:
            w.writerow([c.get("nome"), c.get("cpf"), c.get("loja_nome") or "", c.get("cargo"),
                        c.get("admissao"), c.get("desligamento"), _situacao(c, hoje), c.get("contato")])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=colaboradores_swn.csv"},
        )

    # ------------------------------------------------- Avaliações (Fase 3.2: DB)
    def _avals_join(where="", params=()):
        return query(
            "SELECT a.*, c.nome AS colab_nome, l.nome AS loja_nome "
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
        notas = [a["nota_final"] for a in avals if a.get("nota_final") is not None]
        media_geral = round(sum(notas) / len(notas), 2) if notas else None
        hoje = date.today()
        colabs = query(
            "SELECT c.id, c.nome, c.admissao, c.desligamento, l.nome AS loja_nome "
            "FROM colaboradores c LEFT JOIN lojas l ON l.id = c.loja_id ORDER BY c.nome"
        )
        colabs = [c for c in colabs if _situacao(c, hoje) != "Desligado"]
        return render_template_string(
            AVAL_HTML, user=current_user(), avals=avals, colabs=colabs,
            competencias=AVAL_COMPETENCIAS, tipos=AVAL_TIPOS, media_geral=media_geral,
            ok=request.args.get("ok"), erro=request.args.get("erro"),
        )

    @app.route("/avaliacoes/nova", methods=["POST"])
    @require_roles("admin", "rh", "gerente", "supervisor")
    def avaliacao_nova():
        u = current_user()
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
        execute(
            "INSERT INTO avaliacoes "
            "(colaborador_id, avaliador_id, tipo, periodo, kpi_meta, kpi_real, "
            " nota_resultado, nota_comp, nota_final, competencias, "
            " pontos_fortes, a_desenvolver, plano, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, u["id"], request.form.get("tipo") or "", request.form.get("periodo") or "",
             meta, real, calc["nota_resultado"], calc["nota_comp"], calc["nota_final"],
             json.dumps(competencias, ensure_ascii=False),
             request.form.get("pontos_fortes") or "", request.form.get("a_desenvolver") or "",
             request.form.get("plano") or "", _now(), u["id"]),
        )
        audit(u["id"], "avaliacao_nova", str(cid))
        return redirect("/avaliacoes?ok=Avaliação registrada")

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
            w.writerow([a.get("colab_nome"), a.get("loja_nome") or "", a.get("tipo"), a.get("periodo"),
                        a.get("kpi_meta"), a.get("kpi_real"), a.get("nota_comp"),
                        a.get("nota_resultado"), a.get("nota_final"), _conceito(a.get("nota_final"))])
        return Response(
            buf.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=avaliacoes_swn.csv"},
        )

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
