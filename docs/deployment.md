# Deployment no computador corporativo

Guia genérico: não contém (nem deve receber) nomes reais de projetos, regiões internas, proxies,
contas ou modelos da organização.

## 1. Ambiente virtual e dependências

```bash
git clone <url-do-repositório-privado> simio && cd simio
python -m venv .venv
# Linux/macOS: source .venv/bin/activate     Windows (PowerShell): .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"            # ou: pip install -e .  (sem ferramentas de teste)
```

Sem privilégios de administrador: tudo fica no perfil do usuário. Se o índice de pacotes for um
espelho interno, configure `PIP_INDEX_URL` conforme a orientação da organização.

## 2. Playwright / Chromium

```bash
python -m playwright install chromium
```

O navegador é baixado para o perfil do usuário (`%LOCALAPPDATA%\ms-playwright` no Windows). Atrás de
proxy, defina `HTTPS_PROXY` antes do comando. Se o download externo for bloqueado, a organização
pode disponibilizar o pacote do navegador e definir `PLAYWRIGHT_DOWNLOAD_HOST`. `simio doctor`
testa a inicialização.

## 3. Vertex AI

1. Obtenha com a área responsável: projeto GCP, localização e modelos Gemini aprovados.
2. Autentique pelo fluxo oficial (sem colar tokens em arquivos):
   ```bash
   gcloud auth application-default login     # ou conta de serviço definida pela organização
   ```
3. Configure `.env` (copie de `.env.example`):
   ```
   SIMIO_GCP_PROJECT=<projeto>
   SIMIO_GCP_LOCATION=<localização, ex.: global>
   SIMIO_ANALYSIS_MODEL=<modelo multimodal aprovado>
   SIMIO_REVIEW_MODEL=<opcional>
   SIMIO_DISCOVERY_MODEL=<modelo com suporte a Google Search Grounding>
   ```
4. Valide: `simio doctor --live` (faz uma chamada `generate_content` mínima e uma consulta de
   grounding com tema público neutro).

O SDK `google-genai` ≥ 3 usa `enterprise=True` (nomenclatura atual da plataforma); versões anteriores
usam `vertexai=True`. O SIMIO detecta automaticamente.

## 4. Google Search Grounding

- Verifique se o modelo/região suportam a ferramenta `google_search` (consulte a documentação
  vigente da plataforma) e se o projeto tem o recurso habilitado e aprovado pela organização.
- Teste: `simio doctor --live` e depois `SIMIO_LIVE_TESTS=1 python -m pytest tests/live -q`.
- Termos de uso: as *search suggestions* (`search_entry_point`) são preservadas sem alteração no
  banco (`queries.metadata_json`). O SIMIO não exibe a resposta textual do modelo como resultado ao
  usuário; usa apenas as fontes. Avalie com o jurídico os requisitos aplicáveis ao seu uso.
- Grounding não é índice exaustivo: combine com URLs importadas e conectores opcionais.

## 5. Execução offline

Sem Vertex nem APIs de busca (ex.: validação inicial, contingência):

```bash
simio hunt --offline --urls-file urls.txt --limit 50
```

A coleta das páginas importadas ocorre normalmente; análise e revisão usam regras
determinísticas, e todas as decisões registram essa limitação.

## 6. Proxy e certificados

- APIs (google-genai/httpx): `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`; CA corporativa via
  `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`.
- Navegador: `SIMIO_BROWSER_PROXY=http://<proxy>:<porta>` (credenciais, se necessárias, no próprio
  URL do proxy via `.env` local). O proxy de egress do SIMIO encadeia para ele enviando o **IP já
  validado** (anti DNS rebinding). Se o proxy corporativo recusar CONNECT para IP (filtragem por
  hostname), o erro aparece como `upstream proxy refused CONNECT`; avalie com a segurança usar
  `SIMIO_BROWSER_PROXY_PIN_IP=false` (risco residual em docs/security.md).
- Sandbox do Chromium: ativo por padrão. Se não iniciar (ambientes Linux restritos sem user
  namespaces), `simio doctor` mostra a falha; desative (`SIMIO_BROWSER_SANDBOX=false`) somente com
  aprovação da segurança. No Windows o sandbox funciona sem privilégios de administrador.
- Inspeção TLS corporativa no navegador: prefira instalar a CA no repositório do sistema; só em
  último caso use `SIMIO_IGNORE_HTTPS_ERRORS=true` e registre a decisão.
- Erros comuns: `CERTIFICATE_VERIFY_FAILED` (CA ausente), `407` (autenticação do proxy),
  `ERR_TUNNEL_CONNECTION_FAILED` (proxy recusou CONNECT ou destino bloqueado pela política).

## 7. Diretórios locais de evidência

`SIMIO_OUTPUT_DIR` (padrão `output/`) contém banco, evidências, relatórios e logs. Mantenha-o em
local aprovado, com backup e controle de acesso definidos pela auditoria. Não sincronize com
serviços pessoais e não copie para máquinas pessoais.

Cadastro autorizado de correspondentes (opcional, habilita `CONFIRMED_COBAN`):
`SIMIO_COBAN_REGISTRY_PATH=/caminho/relacao_correspondentes.csv` (colunas `cnpj;nome;...`),
`SIMIO_COBAN_REGISTRY_AUTHORITATIVE=true` somente se a fonte for oficial/aprovada e
`SIMIO_COBAN_REGISTRY_REFERENCE_DATE=AAAA-MM-DD` (exibida nos relatórios).
Antes do uso, preencha e valide em `src/simio/brands/bv.toml` os itens marcados `VALIDATE`
(domínios oficiais completos e CNPJs da instituição).

## 8. Limites de custo e concorrência

| Variável | Padrão | Efeito |
|---|---|---|
| `SIMIO_MAX_LLM_REQUESTS` | 300 | Requisições LLM por campanha (inclui tentativas e grounding) |
| `SIMIO_MAX_LLM_TOKENS` | 2.000.000 | Tokens por campanha |
| `SIMIO_MAX_SEARCH_QUERIES` | 40 | Consultas de descoberta |
| `SIMIO_WORKERS` | 3 | Candidatos em paralelo (também limita chamadas LLM simultâneas) |
| `SIMIO_MAX_ATTEMPTS` | 3 | Tentativas por etapa antes de FAILED |
| `SIMIO_MAX_RECOLLECTS` | 1 | Novas coletas solicitadas pelo revisor |

Ao esgotar o orçamento a campanha pausa (`PAUSED_BUDGET`); rode `simio resume --max-llm-requests N`
(ou `--max-tokens N`): sem flags, a retomada mantém os parâmetros originais da campanha. Análises
já pagas para a mesma evidência são reaproveitadas.
Estimativa: cada candidato analisado usa ~1–3 requisições (analista, revisor, reparo eventual).

## 9. Pendências de validação corporativa

- [ ] `simio doctor --live` com projeto/modelos aprovados.
- [ ] `SIMIO_LIVE_TESTS=1 python -m pytest tests/live` (saída estruturada e grounding reais).
- [ ] Compatibilidade de `response_json_schema` com o modelo aprovado (há fallback automático).
- [ ] Resolução dos URIs de redirecionamento do grounding através do proxy corporativo.
- [ ] Preencher/validar `bv.toml` (domínios oficiais, **CNPJs da instituição**) e carregar cadastro de
  correspondentes autoritativo com data de referência.
- [ ] Verificar o sandbox do Chromium e o encadeamento com o proxy corporativo (CONNECT para IP).
- [ ] Revalidar vigência da Res. CMN 4.935/2021 e calibrar a pontuação com casos reais revisados.
