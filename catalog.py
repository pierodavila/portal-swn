"""
Catalogo de ferramentas do Portal SWN.

Espelha o array TOOLS do PORTAL_SWN.html. Cada item:
  id        -> slug usado na rota /tool/<id>
  nome      -> rotulo exibido
  icone     -> emoji
  arquivo   -> nome do HTML em tools/ (None quando ainda nao ha destino)
  descricao -> texto curto
  papeis    -> conjunto de papeis que enxergam/acessam a ferramenta

Regras de papel: nao sao hierarquicos; 'admin' sempre tem acesso a tudo
(garantido tambem em auth.has_role). A checagem por ferramenta usa o campo
'papeis' deste catalogo.

Itens com arquivo=None (xlsx, links externos, 'em breve') aparecem no
dashboard mas NAO sao servidos por /tool/<id> — retornam 404 se acessados.
Nesta Fase 1 os HTMLs servidos ainda usam localStorage (sem migracao).
"""

# ----------------------------------------------------------------------------
# PAPEIS (grupos de usuario). Editavel. 'admin' sempre tem acesso a tudo.
# O cadastro de colaboradores e feito pelo admin em /admin/usuarios, escolhendo
# um destes papeis (como no app de Conferencia de Caixa).
# ----------------------------------------------------------------------------
ROLES = {
    "admin":      "Dono / Admin",
    "supervisor": "Supervisor",
    "gerente":    "Gerente de loja",
    "subgerente": "Subgerente",
    "vendedor":   "Vendedor(a)",
    "estoquista": "Estoquista",
    "digital":    "Digital / E-commerce",
    "rh":         "RH / DP",
    "financeiro": "Financeiro",
}
ROLE_IDS = list(ROLES.keys())

# Atalhos de conjuntos de papeis (apenas para legibilidade do catalogo)
TODOS = list(ROLES.keys())
OPERACAO_LOJA = ["supervisor", "gerente", "subgerente"]
ATENDIMENTO = ["supervisor", "gerente", "subgerente", "vendedor", "digital"]

TOOLS = [
    # ---- Gestao / Dono ----
    {
        "id": "cockpit",
        "nome": "Cockpit do Dono",
        "icone": "📊",
        "arquivo": "COCKPIT_SWN.html",
        "descricao": "Visão por loja: gente, avaliações e disciplina num lugar só.",
        "papeis": ["admin", "supervisor"],
    },
    {
        "id": "gestao",
        "nome": "Gestão & Cadastros (console)",
        "icone": "🗂️",
        "arquivo": "GESTAO_SWN.html",
        "descricao": "Cadastro de colaboradores/lojas, cockpit multi-loja e indicadores de gente.",
        "papeis": ["admin", "rh", "supervisor"],
    },
    # ---- Financeiro / Operacao ----
    {
        "id": "conferencia_caixa",
        "nome": "App Conferência de Caixa",
        "icone": "💰",
        "arquivo": None,  # serviço separado (app de caixa); aqui é só atalho
        "descricao": "Conferência diária de caixa por loja.",
        "papeis": ["admin", "financeiro", "supervisor", "gerente"],
    },
    {
        "id": "conciliacao",
        "nome": "Conciliação 3 vias",
        "icone": "🔁",
        "arquivo": "conciliacao_3vias.html",
        "descricao": "Bate dinheiro, cartões e Pix.",
        "papeis": ["admin", "financeiro", "supervisor"],
    },
    {
        "id": "deposito",
        "nome": "Depósito",
        "icone": "🏦",
        "arquivo": "deposito.html",
        "descricao": "Controle de sangria e depósito.",
        "papeis": ["admin", "financeiro", "supervisor", "gerente", "subgerente"],
    },
    {
        "id": "plano_contas",
        "nome": "Plano de Contas Gerencial",
        "icone": "📊",
        "arquivo": None,  # xlsx
        "descricao": "Estrutura gerencial de contas SWN.",
        "papeis": ["admin", "financeiro"],
    },
    {
        "id": "nibo",
        "nome": "Nibo",
        "icone": "📒",
        "arquivo": None,  # em breve / externo
        "descricao": "Sistema contábil/financeiro.",
        "papeis": ["admin", "financeiro"],
    },
    # ---- RH ----
    {
        "id": "folha_pagamento",
        "nome": "Modelo de Folha de Pagamento",
        "icone": "🧾",
        "arquivo": None,  # xlsx
        "descricao": "Planilha-modelo da folha por loja.",
        "papeis": ["admin", "rh"],
    },
    {
        "id": "avaliacao",
        "nome": "Avaliação do Colaborador",
        "icone": "⭐",
        "arquivo": "AVALIACAO_COLABORADOR_SWN.html",
        "descricao": "Avaliação com KPIs e nota — salva no banco, com histórico.",
        "papeis": ["admin", "rh", "supervisor", "gerente"],
    },
    {
        "id": "adiantamento",
        "nome": "Adiantamento (vale)",
        "icone": "💵",
        "arquivo": "RH_DISCIPLINA_JORNADA_SWN.html",
        "descricao": "Vale/adiantamento (art. 462 CLT) — salvo no banco, com recibo.",
        "papeis": ["admin", "rh", "financeiro"],
    },
    {
        "id": "escala",
        "nome": "Escala mensal",
        "icone": "🗓️",
        "arquivo": "RH_DISCIPLINA_JORNADA_SWN.html",
        "descricao": "Grade de turnos por loja e mês — salva no banco.",
        "papeis": ["admin", "rh", "supervisor", "gerente", "subgerente"],
    },
    {
        "id": "kit_admissao",
        "nome": "Kit Admissão",
        "icone": "📝",
        "arquivo": "KIT_ADMISSAO_SWN.html",
        "descricao": "Checklist de documentos + termos + contrato.",
        "papeis": ["admin", "rh"],
    },
    {
        "id": "advertencia",
        "nome": "Modelo de Advertência",
        "icone": "⚠️",
        "arquivo": "RH_DISCIPLINA_JORNADA_SWN.html",
        "descricao": "Advertência verbal/escrita/suspensão (CLT) — salva no banco, com histórico.",
        "papeis": ["admin", "rh", "supervisor", "gerente"],
    },
    {
        "id": "disciplina_jornada",
        "nome": "Disciplina & Jornada",
        "icone": "📅",
        "arquivo": "RH_DISCIPLINA_JORNADA_SWN.html",
        "descricao": "Escala mensal, ponto, advertências e adiantamento.",
        "papeis": ["admin", "rh", "supervisor", "gerente"],
    },
    # ---- Vendas / loja ----
    {
        "id": "vendas",
        "nome": "Vendas (metas, atendimento, trocas)",
        "icone": "🎯",
        "arquivo": "VENDAS_SWN.html",
        "descricao": "KPIs ao vivo, 7 etapas do atendimento e política de trocas (CDC).",
        "papeis": ATENDIMENTO,
    },
    {
        "id": "treinamentos",
        "nome": "Treinamentos (com quiz)",
        "icone": "🎓",
        "arquivo": "TREINAMENTOS_SWN.html",
        "descricao": "Módulos de atendimento + quiz e registro de conclusão.",
        "papeis": ["supervisor", "gerente", "subgerente", "vendedor", "estoquista", "digital", "rh"],
    },
    {
        "id": "nps",
        "nome": "NPS — Satisfação do cliente",
        "icone": "📣",
        "arquivo": "NPS_SWN.html",
        "descricao": "Pesquisa por QR, resultados e NPS por loja.",
        "papeis": ["supervisor", "gerente", "subgerente", "vendedor", "digital", "rh"],
    },
    {
        "id": "checklist",
        "nome": "Checklist de Loja",
        "icone": "🔑",
        "arquivo": "CHECKLIST_LOJA_SWN.html",
        "descricao": "Abertura e fechamento diário — salvo no banco, com status no cockpit.",
        "papeis": ["admin", "supervisor", "gerente", "subgerente", "estoquista"],
    },
    # ---- Acessos / Geral ----
    {
        "id": "cofre",
        "nome": "Cofre Central (senhas)",
        "icone": "🔐",
        "arquivo": "cofre_central.html",
        "descricao": "Senhas e acessos dos sistemas.",
        "papeis": ["admin", "rh", "financeiro", "supervisor", "gerente"],
    },
    {
        "id": "guia",
        "nome": "Guia de Processos (completo)",
        "icone": "📘",
        "arquivo": "GUIA_DE_TUDO_SWN.html",
        "descricao": "Manual de todo o ciclo da loja.",
        "papeis": TODOS,
    },
]

# Indice por id para lookup rapido
TOOLS_BY_ID = {t["id"]: t for t in TOOLS}


def get_tool(tool_id):
    return TOOLS_BY_ID.get(tool_id)


def tools_for_role(papel):
    """Ferramentas visiveis para um papel. 'admin' ve tudo."""
    if papel == "admin":
        return list(TOOLS)
    return [t for t in TOOLS if papel in t["papeis"]]


def role_can_access(papel, tool):
    """Regra de acesso por ferramenta (admin sempre pode)."""
    if papel == "admin":
        return True
    return papel in tool["papeis"]
