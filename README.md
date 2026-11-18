# SIMIO — Autonomous Brand Identity Intelligence

Harness multiagente, executado por terminal, para investigar o uso potencialmente indevido de uma
marca por terceiros na web. O primeiro caso de uso apoia a auditoria interna de correspondentes
bancários (COBANs) à luz da Resolução CMN nº 4.935/2021.

O SIMIO **não é um detector de menções à marca**. Ele examina a diferença entre identidade
declarada, identidade visual, identidade efetiva do estabelecimento, transparência da relação
comercial e percepção potencial do consumidor, e separa os cenários:

| Cenário | Descrição | Classificação padrão |
|---|---|---|
| A | Menção comercial legítima, com condição de correspondente clara | `NO_INDICATION` |
| B | Uso destacado da marca sem transparência suficiente | `REVIEW_REQUIRED` (MEDIUM) |
| C | Possível apresentação como canal oficial | `REVIEW_REQUIRED` (HIGH) |
| D | Forte indício de impersonação | `STRONG_INDICATION` (HIGH) |
| E | Evidência insuficiente | `INCONCLUSIVE` |

Classificação, prioridade, confiança da extração, confiança da atribuição de identidade e
suficiência da evidência são **eixos separados**. Nenhuma saída constitui conclusão regulatória:
a decisão é da auditoria.

## Arquitetura

```
simio hunt
  └─ Orchestrator (máquina de estados em SQLite, checkpoints, retry/backoff, orçamento, retomada)
       ├─ Discovery Agent   hipóteses → Google Search Grounding (Vertex) | Brave | Serper | urlscan | importação
       │                    → deduplicação (URL normalizada / domínio registrável) → priorização
       ├─ Evidence Agent    Playwright/Chromium atrás de proxy de egress (anti-SSRF) → screenshots,
       │                    texto, HTML (.html.txt), dados da página, manifesto SHA-256
       ├─ Identity Agent    CNPJ (numérico e alfanumérico), razão social, contatos, declarações →
       │                    CONFIRMED_COBAN só com cadastro autorizado
       ├─ Brand Analyst     Gemini multimodal (ou heurística offline) → 8 dimensões, indícios citando
       │                    referências do catálogo de evidências
       ├─ Compliance        mapeamento determinístico ao texto literal da Res. CMN 4.935/2021
       ├─ Reviewer          revisor cético (LLM + regras); pode confirmar, rebaixar, pedir nova coleta (1x),
       │                    declarar inconclusivo ou encaminhar a humano
       └─ Reporting         Excel consolidado + dossiês HTML locais
```

Fluxo explícito, sem comunicação livre entre agentes; paralelismo apenas entre candidatos
independentes. Detalhes em [docs/architecture.md](docs/architecture.md).

## Requisitos

- Python ≥ 3.11 (sem Docker, WSL, banco externo ou privilégios de administrador)
- Chromium do Playwright (`python -m playwright install chromium`, instalação no perfil do usuário)
- Para IA: acesso à Vertex AI (projeto, região, modelo Gemini) com credenciais ADC autorizadas
- Opcional: chaves Brave Search / Serper / urlscan.io

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env                 # preencha localmente; nunca versione
simio doctor
```

## Execução

```bash
simio doctor [--live]                        # diagnóstico (--live faz chamadas mínimas à Vertex/Grounding)
simio hunt --brand bv --limit 50             # investigação completa (descoberta automática)
simio hunt --urls-file lista.txt --limit 20  # + URLs importadas (txt/csv/texto livre)
simio hunt --offline --urls-file lista.txt   # sem Vertex/APIs: heurística determinística
simio status [--list]                        # estado da campanha
simio resume [--retry-failed]                # retoma após falha, Ctrl+C ou orçamento esgotado
simio report [--all-dossiers]                # regenera Excel e dossiês
simio test [--live]                          # suíte de testes
```

Opções do `hunt`: `--limit`, `--workers`, `--model`, `--review-model`, `--max-queries`,
`--max-llm-requests`, `--max-tokens`, `--sources grounding,brave,serper,urlscan`, `--output-dir`,
`--offline`, `--urls-file`, `--urls`, `--review-all`, `--no-report`.

Saídas (em `output/`, ignorado pelo Git):

```
output/simio.db                                   estado, decisões, consultas, uso de LLM
output/campaigns/<campanha>/evidence/<id>/attempt-N/  viewport.png, fullpage.png, text.txt,
                                                  page.html.txt, page.json, manifest.json
output/reports/<campanha>/simio-<campanha>-<ts>.xlsx
output/reports/<campanha>/dossiers/<id>.html
output/logs/simio.jsonl
```

Abas do Excel: Executive Summary, Candidates, Strong Indications, Identity Attribution, Entities,
Evidence Index, Investigation Log, Errors and Limitations.

## Testes

```bash
python -m pytest -q          # unit + integração (Chromium real) + e2e, com Vertex mockado
SIMIO_LIVE_TESTS=1 python -m pytest tests/live   # somente no ambiente autorizado
ruff check src tests scripts && mypy
python scripts/check_secrets.py --history       # auditoria antes de publicar
```

Fixtures são sites **sintéticos** (empresas fictícias, domínios reservados, CNPJs de exemplo
documentais). Nenhum dado real de clientes, correspondentes ou da organização é usado.

## Limitações

- O Grounding não é um índice exaustivo da web; a descoberta é uma amostra guiada por hipóteses.
- Somente a URL descoberta é inspecionada (sem cliques, formulários, login ou páginas internas).
- `CONFIRMED_COBAN` exige um cadastro autorizado fornecido pelo operador (CSV).
- O modo offline usa heurística textual: identidade visual não é avaliada por modelo.
- A integração real com Vertex AI / Grounding foi implementada e testada com mocks; a validação
  real é pendente no ambiente corporativo (ver [docs/deployment.md](docs/deployment.md)).

Mais em [docs/methodology.md](docs/methodology.md) e [docs/security.md](docs/security.md).

## Segurança (resumo)

Navegação somente leitura atrás de proxy de egress que valida e fixa cada conexão (bloqueia
loopback, redes privadas, link-local, metadados de nuvem, inclusive via redirecionamento e DNS
rebinding); conteúdo coletado tratado como não confiável (delimitado, nunca com ferramentas no
modelo, referências validadas); segredos só por ambiente; dados de investigação nunca versionados;
sem telemetria.
