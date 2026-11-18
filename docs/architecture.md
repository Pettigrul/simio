# Arquitetura

## Decisão de framework

Foram avaliados LangGraph, Google ADK e implementação Python nativa. Escolhemos **Python nativo
com `asyncio` e uma máquina de estados explícita persistida em SQLite**:

| Critério | Nativo | LangGraph / ADK |
|---|---|---|
| Fluxo | Linear e conhecido (descoberta → coleta → análise → revisão) | Grafos/agentes livres são desnecessários |
| Persistência | Estado por candidato em SQLite, transições validadas | Checkpointers próprios, acoplamento ao framework |
| Testabilidade | Agentes são classes/funções com entradas/saídas Pydantic | Requer harness do framework |
| Custo operacional | Sem dependências pesadas; roda em qualquer Python ≥ 3.11 | Mais dependências e superfície de atualização |
| Controle | Orçamento, retry, limites de ciclo explícitos | Indireto |

Não há comunicação livre entre agentes: o orquestrador passa saídas tipadas adiante.

## Módulos

```
src/simio/
  cli.py                 Typer: doctor, hunt, resume, report, status, test
  config.py              Settings (SIMIO_*), sem valores corporativos fixos
  core/
    models.py            Contratos Pydantic (extra="forbid") de todos os agentes
    state.py             Tabela de transições, estados em voo e pontos de retomada
    storage.py           SQLite + migrações versionadas (schema_migrations)
    orchestrator.py      Pipeline, concorrência, retry/backoff, orçamento, retomada
    decision.py          Guardrails determinísticos e composição da decisão final
    urls.py              Normalização, domínio registrável (PSL embutida), extração de URLs
  agents/
    discovery.py         Hipóteses, plano de consultas, fallback, dedup, priorização
    evidence.py          Catálogo de evidências endereçável + sinais determinísticos
    identity.py          Atribuição de identidade e cadastro de correspondentes
    brand.py             Brand Analyst (Gemini multimodal) + heurística offline
    compliance.py        Mapeamento normativo/contratual determinístico
    reviewer.py          Revisor cético (LLM + desafios determinísticos)
  providers/
    llm.py               Interface neutra, orçamento persistente, retry, reparo de JSON
    vertex.py            google-genai (Vertex AI), grounding, tradução de erros
    grounding.py         Conector de descoberta via Google Search Grounding
    search.py            Brave, Serper, urlscan, importação de arquivo/texto
    mock.py              Provedor determinístico para testes
  collectors/browser.py  Playwright: coleta, screenshots, manifesto SHA-256
  security/
    url_policy.py        Política de destinos (SSRF)
    egress_proxy.py      Proxy de egress por página (aplicação na camada de rede)
  reporting/             Excel (openpyxl) e dossiês HTML (Jinja2 com autoescape)
  prompts/               Prompts versionados (front-matter `version:`)
  knowledge/cmn_4935.toml  Texto literal dos dispositivos usados no mapeamento
  brands/bv.toml         Perfil de marca (termos, domínios oficiais, frases)
```

## Máquina de estados

```
DISCOVERED → QUEUED → COLLECTING → COLLECTED → ANALYZING → REVIEWING → COMPLETED
                ↑          │                        │           │   └→ INCONCLUSIVE
                └──────────┘ (falha transitória)    │           └→ QUEUED (nova coleta, máx. SIMIO_MAX_RECOLLECTS)
                                                    └→ COMPLETED / INCONCLUSIVE (sem revisão)
qualquer etapa → FAILED (após SIMIO_MAX_ATTEMPTS erros internos)
```

- Cada transição é validada (`core/state.py`) e registrada em `state_transitions`.
- `simio resume` devolve estados em voo ao último estado estável (`COLLECTING→QUEUED`,
  `ANALYZING/REVIEWING→COLLECTED`) e continua. Descoberta concluída é marcada por checkpoint.
- Candidatos além de `--limit` permanecem `DISCOVERED` (backlog rastreável).
- Deduplicação: chave única `(campanha, URL normalizada)`; domínios agrupados por domínio
  registrável; estabelecimentos agrupados por CNPJ (`entities`) — um domínio pode ter várias
  páginas, um estabelecimento vários domínios.

## Orçamento e falhas

- `Budget` persiste uso em `llm_usage`; requisições são reservadas antes da chamada (inclusive
  tentativas que falham). Ao esgotar: a campanha fica `PAUSED_BUDGET`, candidatos voltam ao estado
  estável e `simio resume` continua com orçamento maior.
- Erros transitórios (429/5xx/timeout) têm retry com backoff exponencial + jitter.
- Saída de modelo inválida: uma tentativa de reparo; depois o candidato conta tentativa e,
  esgotadas, vai a `FAILED`.
- Vertex indisponível (auth/permissão/modelo inexistente): degrada para analistas
  determinísticos e registra a limitação em cada decisão.
- Grounding indisponível: o conector é desativado na execução e os demais continuam (fallback).
  Sem nenhum mecanismo, `hunt` informa diagnóstico e aceita URLs importadas.

## Contratos e rastreabilidade

O **catálogo de evidências** (`E-TITLE`, `E-FOLD`, `E-FTR`, `E-TXT-001`, `E-SHOT-VIEWPORT`…) é
uma função pura da evidência armazenada; relatórios o reconstroem. Analistas devem citar refs do
catálogo; refs inexistentes são descartadas e indicadores sem refs válidas são removidos
(`core/decision.py`). O Excel e os dossiês exibem o texto bruto de cada ref.
