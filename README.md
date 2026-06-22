# Portal SWN — serviço web (Fase 1)

Casca protegida do **Portal SWN** (rede de outlets de moda). Faz **login real**,
controla acesso por **papel (RBAC)** e serve as ferramentas (HTMLs) só para quem
tem permissão. Inclui um **admin de usuários** simples.

> **Serviço SEPARADO.** Este repositório **não tem nenhuma relação** com o app de
> **Conferência de Caixa**. São repositórios, serviços e bancos distintos. Nada
> aqui toca o `app.py` do caixa.
>
> **Dados ainda em localStorage.** Nesta Fase 1 as ferramentas continuam salvando
> os formulários no `localStorage` do navegador. O portal apenas **protege o
> acesso** (login + papel). A migração de dados para o banco é **fase futura**.

## O que entra na Fase 1

- Login real (`werkzeug.security`, hash de senha) + sessão Flask.
- RBAC com 9 papéis/grupos (não hierárquicos): `admin`, `supervisor`, `gerente`,
  `subgerente`, `vendedor`, `estoquista`, `digital`, `rh`, `financeiro`
  (definidos em `catalog.ROLES`). O `admin` acessa tudo. Cada ferramenta declara o
  conjunto de papéis em `catalog.py`. Os colaboradores são cadastrados pelo admin
  em `/admin/usuarios`, escolhendo um destes papéis (como no app de Conferência de Caixa).
- Dashboard montado **no servidor** a partir do catálogo, filtrado pelo papel.
- `/tool/<id>`: sem login → redireciona p/ `/login`; logado sem papel → **403**;
  ferramenta inexistente → **404**.
- Admin de usuários: criar, ativar/desativar, redefinir senha.
- Auditoria básica (`audit_log`): login, logout, abertura de ferramenta etc.

## Papéis e ferramentas

O mapa papel → ferramenta está em [`catalog.py`](catalog.py) (espelha o array
`TOOLS` do `PORTAL_SWN.html`). Ferramentas servidas estão em `tools/`. Itens sem
arquivo (xlsx, links externos, "em breve") aparecem no painel mas não são
servidos por `/tool/<id>`.

## Stack

Flask 3 · gunicorn · psycopg2 (Postgres em produção) · sqlite3 (local) ·
`werkzeug.security` · sessão Flask.

A camada de banco ([`db.py`](db.py)) escolhe o backend pela env `DATABASE_URL`:
- ausente ou começando com `sqlite` → **SQLite** (arquivo `portal.db`);
- começando com `postgres` → **Postgres** (psycopg2).

Escreva SQL com placeholders `?`; a camada traduz para `%s` no Postgres.

## Rodar local (sem Postgres)

```bash
cd portal_swn_server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # ajuste ADMIN_EMAIL / ADMIN_PASSWORD se quiser
python app.py                 # http://localhost:5000
```

No primeiro boot, se o banco estiver vazio, é criado um **admin inicial** a
partir de `ADMIN_EMAIL` / `ADMIN_PASSWORD` (padrão `admin@swn.local` /
`troque-imediatamente`). Um aviso é logado. **Troque a senha após o 1º login.**

## Modelo de dados

- `usuarios(id, nome, email UNIQUE, senha_hash, papel, loja_id, ativo, criado_em, ultimo_acesso)`
- `lojas(id, nome, cnpj, cidade_uf, ativo)`
- `audit_log(id, usuario_id, acao, detalhe, ip, criado_em)`

As tabelas são criadas no boot (`init_db`) se não existirem.

## Rotas

| Método | Rota | Acesso |
|---|---|---|
| GET/POST | `/login` | público |
| GET | `/logout` | logado |
| GET | `/` | logado (painel por papel) |
| GET | `/tool/<id>` | logado + papel (senão 403 / 404) |
| GET/POST | `/admin/usuarios` | admin |
| POST | `/admin/usuarios/<id>/toggle` | admin |
| POST | `/admin/usuarios/<id>/reset` | admin |
| GET | `/health` | público (`ok`) |

## Deploy no Render

Há duas formas. A mais simples é o **Blueprint** ([`render.yaml`](render.yaml)),
que já provisiona Web Service + Postgres novo.

### Opção A — Blueprint (recomendado)
1. Suba este diretório como um repositório Git próprio (separado do caixa).
2. No Render: **Blueprints → New Blueprint Instance** → aponte para o repo.
3. O Render lê `render.yaml`, cria o **Web Service** (plano Free) e um **Postgres
   novo** (`portal-swn-db`), e liga `DATABASE_URL` automaticamente.
4. Defina no painel as env vars marcadas `sync: false`: `ADMIN_EMAIL` e
   `ADMIN_PASSWORD`. `SECRET_KEY` é gerada pelo Render.
5. Deploy. Acesse `/health` (deve responder `ok`) e depois `/login`.

### Opção B — manual
1. **New → Web Service**, conecte o repo.
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1`
   - Health check: `/health`
2. **New → Postgres** (um banco **NOVO**, dedicado a este portal).
3. No Web Service, em **Environment**, adicione:
   - `DATABASE_URL` = Internal Connection String do Postgres novo
   - `SECRET_KEY` = string forte (`python -c "import secrets; print(secrets.token_hex(32))"`)
   - `ADMIN_EMAIL` e `ADMIN_PASSWORD`
4. Deploy. Faça login com o admin inicial e **troque a senha**.

## Segurança

- `secret_key` vem de `SECRET_KEY` (env).
- Cookies de sessão `HttpOnly`, `SameSite=Lax`, e `Secure` quando fora do debug.
- Senha nunca é logada; armazenada só como hash (`generate_password_hash`).

## Testes

`test_smoke.py` usa `app.test_client()` com SQLite e prova: rota protegida sem
login redireciona; vendedor recebe 403 em ferramenta de admin; admin recebe 200;
e o `senha_hash` salvo não é igual à senha em texto puro.

```bash
pip install flask
python test_smoke.py
```
