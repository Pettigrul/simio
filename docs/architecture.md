<p align="center">
  <img src="../assets/logos/logo-claro.svg" alt="SIMIO - Arquitetura" width="340">
</p>

<p align="center">
  <img src="../assets/badges/agentes.svg" alt="Agentes">
  <img src="../assets/badges/automação.svg" alt="Automação">
  <img src="../assets/badges/monitoramento.svg" alt="Monitoramento">
</p>

# Arquitetura do Sistema

<p align="center">
  <img src="../assets/banners/divisoria.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/regras.svg" width="22" height="22" valign="middle"> 1. Visão geral e decisões de projeto

O SIMIO foi construído em **Python 3.11+ nativo com `asyncio` e SQLite**, sem frameworks pesados de orquestração (como LangGraph ou CrewAI).

Por que essa escolha:
- **Fluxo determinístico e auditável**: o caminho de cada candidato é conhecido (`Descoberta → Coleta → Identidade → Marca → Conformidade → Revisão → Decisão Final`). Agentes não conversam livremente entre si; o `Orchestrator` controla cada transição e passa contratos Pydantic estritos (`extra="forbid"`) de uma etapa para a outra.
- **Persistência e retomada nativa**: cada transição de estado, consulta executada, evidência coletada e chamada de LLM é gravada imediatamente em SQLite (`output/simio.db`). Se o processo for interrompido ou atingir o teto de orçamento, `simio resume` retoma do ponto exato sem pagar novamente por análises de IA já concluídas.
- **Segurança por padrão**: o navegador Chromium nunca acessa a internet diretamente; cada página cria um `EgressProxy` local que valida a URL, resolve o DNS, bloqueia IPs privados/internos e fixa o IP validado na conexão.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/agentes.svg" width="22" height="22" valign="middle"> 2. Estrutura de módulos

```
src/simio/
  cli.py                   Interface de linha de comando (Typer): hunt, resume, report, status, doctor, test
  config.py                Configurações tipadas (pydantic-settings, prefixo SIMIO_, leitura de .env)
  logging_setup.py         Logs estruturados (JSONL em output/logs/simio.jsonl) com redação de segredos
  core/
    models.py              Modelos Pydantic compartilhados entre todos os módulos
    state.py               Máquina de estados do candidato e regras de transição permitidas
    storage.py             Persistência SQLite com migrações versionadas, checkpoints e lease de campanha
    orchestrator.py        Pipeline assíncrono, pool de workers, retry/backoff, orçamento e retomada
    decision.py            ScoringConfig, guardrails determinísticos e composição da FinalDecision
    urls.py                Normalização canônica de URLs, extração de domínio registrável (PSL offline)
  agents/
    discovery.py           Hipóteses de busca (H1–H7 marca/produto/canais, H8 domínios semelhantes, H9 COBANs, H10 fraudes, H11 NaPista, H12/H13 logo antiga), modos de campanha (FOCUS_MODES), planejamento intercalado, deduplicação e priorização
    evidence.py            Construção do catálogo de evidências (E-*) e extração determinística de sinais
    identity.py            Extração/validação de CNPJ (numérico e alfanumérico), razão social e cruzamento COBAN
    brand.py               BrandAnalyst (Gemini multimodal + heurística offline + cross_check)
    compliance.py          Mapeamento determinístico para a Resolução CMN nº 4.935/2021 e cláusulas contratuais
    reviewer.py            Reviewer cético (LLM + desafios determinísticos de validação)
    visual.py              Comparação de imagem sem IA: logo antiga (B vermelho + V azul) nos screenshots, indícios por nome de arquivo e recortes
  providers/
    llm.py                 LLMGateway: reserva de orçamento, semáforo de concorrência, retry e reparo de JSON
    vertex.py              Integração oficial com google-genai (Vertex AI) e Google Search Grounding
    grounding.py           Conector de descoberta via Grounding com resolução segura de redirecionamentos
    search.py              Conectores de busca (Serper paginado, Google Lens via Serper, Brave, urlscan.io, crt.sh) e importadores
    typosquat.py           Domínios semelhantes (typo/combosquatting/troca de TLD) resolvidos por DNS (fonte `typosquat`)
    mock.py                Provedor determinístico em memória para testes automatizados
  collectors/
    browser.py             Coleta via Playwright/Chromium headless, screenshots, extração DOM e manifesto SHA-256
  security/
    url_policy.py          Política anti-SSRF (validação sintática e resolução DNS contra redes privadas)
    egress_proxy.py        Proxy HTTP/CONNECT por página com DNS pinning
  reporting/
    data.py                Carregamento consistente de registros por campanha e ordenação por criticidade
    __init__.py            generate_reports(): dossiês, recortes da logo antiga, Excel, HTML e PDF da campanha
    excel.py               Planilha consolidada (.xlsx), até 10 abas (inclui "Logo antiga" com recortes) e proteção contra fórmula injection
    campaign_html.py       Relatório consolidado da campanha (HTML), base do PDF
    pdf.py                 Conversão do relatório consolidado em PDF (Chromium do Playwright)
    dossier.py             Geração dos dossiês HTML individuais (Jinja2 com autoescape e Content-Security-Policy)
  prompts/
    brand_analyst.md       Prompt versionado do analista de marca (brand-analyst/v6)
    reviewer.md            Prompt versionado do revisor cético (reviewer/v4)
  knowledge/
    cmn_4935.toml          Texto literal dos artigos da Resolução CMN nº 4.935/2021
  brands/
    bv.toml                Perfil da marca Banco BV (domínios, perfis sociais, CNPJs, regexes, vocabulário, typosquatting e logo antiga)
    assets/                Imagens de referência (ex.: bv_legacy_logo.png)
scripts/
  prepare_coban_registry.py  Limpa a exportação de COBANs ativos e gera data/coban_registry_bv.csv
  check_secrets.py           Varredura de segredos na árvore e no histórico do Git
```

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/classificacao.svg" width="22" height="22" valign="middle"> 3. Máquina de estados do candidato

Cada URL única descoberta em uma campanha percorre os seguintes estados em `core/state.py`:

```
DISCOVERED ──► QUEUED ──► COLLECTING ──► COLLECTED ──► ANALYZING ──► REVIEWING ──► COMPLETED
                 ▲            │                            │             │   └───► INCONCLUSIVE
                 └────────────┘ (falha transitória)        │             └───────► QUEUED (RECOLLECT, máx. 1x)
                                                           └─────────────────────► COMPLETED / INCONCLUSIVE
qualquer etapa em voo ──► FAILED (após SIMIO_MAX_ATTEMPTS falhas internas)
```

### Garantias operacionais da máquina de estados:
1. **Transições atômicas (*Compare-and-Set*)**: toda transição é validada contra `TRANSITIONS`, gravada na tabela `state_transitions` dentro de uma transação SQLite (`BEGIN IMMEDIATE`) e verifica o estado esperado do candidato.
2. **Lease de campanha única (`campaign_leases`)**: enquanto um processo executa `hunt` ou `resume` em uma campanha, ele mantém um *lease* com *heartbeat* a cada 30 segundos (expiração em 120 segundos). Isso impede que dois terminais processem a mesma campanha ao mesmo tempo.
3. **Isolamento de falhas entre workers**: os candidatos são processados em paralelo até o limite de `--workers`. Se um candidato sofrer um erro inesperado, apenas ele é marcado como `FAILED`, sem derrubar os demais workers nem fechar a instância compartilhada do Chromium.
4. **Proteção contra "candidato veneno" (`recover_in_flight`)**: o contador de tentativas (`attempts`) é incrementado **antes** de iniciar a etapa de coleta ou análise. Se um candidato travar ou derrubar o processo repetidamente até atingir `max_attempts`, o `resume` o move automaticamente para `FAILED` em vez de entrar em loop infinito.
5. **Reaproveitamento de análises pagas (`evidence_id`)**: cada avaliação (`identity`, `brand`, `compliance`, `review`, `final`) é salva vinculada ao `id` da evidência coletada. Se uma campanha parar no meio da etapa de revisão por falta de orçamento, ao rodar `simio resume` a análise já feita pelo `BrandAnalyst` para aquela evidência é reaproveitada do banco sem nova chamada à Vertex AI.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/busca.svg" width="22" height="22" valign="middle"> 4. Descoberta: fontes, hipóteses e planejamento (`agents/discovery.py`)

```
build_hypotheses(marca)  ──►  H1–H7, H10, H11   (varredura aberta, sem depender de COBANs)
coban_hypotheses(base)   ──►  H9               (opcional, sempre no fim da fila)
TyposquatConnector       ──►  H8               (DNS, sem acessar páginas)
LensSearch               ──►  H12              (busca reversa da logo antiga)
build_legacy_logo_hypotheses() ──► H13         (só em --focus logo-antiga, substitui a varredura aberta)
build_threat_hypotheses()      ──► H14+H10+H7  (só em --focus ameacas, sem H9)
                                   │
                                   ▼
             plan(): uma lista por fonte, intercaladas (zip)
   grounding: 1 prompt por hipótese │ serper/brave: 1 consulta por dork
   typosquat: 1 consulta por semente │ crtsh: padrões de domínio
                                   │
                                   ▼
         run() ─► deduplicate() ─► prioritise() ─► fila (--limit)
```

| Fonte | Unidade de consulta | Consultas na rodada BV |
|---|---|---|
| `grounding` | Prompt com a hipótese e seus dorks (o Gemini faz cerca de 18 buscas por prompt) | 38 + até 10 de H9 |
| `serper` | Um dork executado literalmente, em páginas de 10 resultados (`SIMIO_SERPER_PAGES`) | 128 + até 40 de H9 |
| `typosquat` | Uma semente (`typo:bancobv.com.br`) ou rótulo (`combo:bv`), com cerca de 1.900 domínios no total | 7 |
| `crtsh` | Padrão de domínio da marca | 3 |
| `lens` | URL pública de uma imagem da logo antiga (3 créditos cada) | 1 |

- **Orçamento**: `SIMIO_MAX_SEARCH_QUERIES` corta a lista intercalada; 300 cobre o plano completo (cerca de 230 consultas).
- **Retomada**: consultas já executadas na campanha são puladas no `resume`.
- **Logo antiga na análise**: antes do `BrandAnalyst`, `visual.detect_legacy_logo()` compara os screenshots (já com hash) com as referências do perfil. Os achados entram no catálogo como `E-VIS-LEGACY-*` e a regra `apply_legacy_logo_rule()` fixa o piso `REVIEW_REQUIRED`/`HIGH`, que `compose_final()` mantém mesmo após o revisor.
- **Campanhas focadas** (`HuntOptions.focus`, gravado nos parâmetros da campanha e respeitado pelo `resume`):
  - `DiscoveryAgent(focus=...)` troca as hipóteses abertas pelas do modo e ignora fontes que só listam domínios (`typosquat`, `crtsh`, `urlscan`); as fontes padrão do modo ficam em `FOCUS_DEFAULT_SOURCES`.
  - Em `_analyze()`, depois da comparação de imagem e da identidade, uma página sem logo confirmada e sem `legacy_logo_hints()` (imagem com o nome da marca no arquivo ou no `alt`) é encerrada com um `BrandAssessment` `focus:logo-antiga` (`NO_INDICATION`, ou `INCONCLUSIVE` se a coleta falhou), sem chamar os modelos. A limitação fica registrada na decisão final.
- **Inteligência de domínio**: `agents/domain_intel.py` (`enrich_campaign`) consulta RDAP do domínio e do IP e DNS sobre HTTPS para os hosts dos achados; `hosting_platform()` reconhece inquilinos de plataformas (lista própria + parte privada da Public Suffix List). O resultado fica em `campaigns/<ID>/domain-intel.json` (sem migração de banco) e alimenta a aba "Domínios", três colunas da aba "Ameaças" e os dossiês. Comando `simio enrich`; o `hunt`/`resume` chamam antes do relatório.
- **Modo de ameaças**: `prioritise(..., focus="ameacas")` mantém e prioriza construtores gratuitos; `FOCUS_WITHOUT_COBANS` desliga H9. Na análise, `classify_input()` transforma os campos de formulário em `E-FORM-*` e `apply_sensitive_data_rule()` fixa o piso `REVIEW_REQUIRED`/`HIGH` para página com a marca que pede credenciais; `compose_final()` mantém o piso após o revisor.
- **Links opacos do Serper**: `SerperSearch.run()` transforma resultados `google.com/goto` com `displayedLink` em candidato da página inicial do domínio (`recovered_from_google_redirect`, -1 na fila); redes sociais e resultados sem domínio ficam só no registro da consulta.
- **Rotação de COBANs**: `Storage.searched_coban_cnpjs()` guarda quais correspondentes já foram pesquisados com sucesso; a próxima campanha começa pelos que faltam.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/validado.svg" width="22" height="22" valign="middle"> 5. Controle de orçamento e resiliência de LLM (`providers/llm.py`)

- **Reserva prévia de requisições e tokens (`Budget`)**: antes de disparar qualquer chamada à Vertex AI, o `LLMGateway` reserva no banco 1 requisição e uma estimativa conservadora de tokens (`caracteres / 3 + 1800 por imagem + max_output_tokens`). Isso garante que múltiplos workers paralelos não ultrapassem o teto de `--max-llm-requests` ou `--max-tokens`.
- **Pausa limpa por orçamento (`PAUSED_BUDGET`)**: quando o orçamento se esgota, o orquestrador interrompe novas chamadas, devolve o candidato atual ao estado estável `COLLECTED` e finaliza a execução com status `PAUSED_BUDGET`, gerando os relatórios parciais do que já foi concluído.
- **Autorreparo de JSON estruturado**: todas as saídas do modelo são validadas pelo schema Pydantic correspondente (`BrandAnalysisOutput` ou `ReviewOutput`). Se o modelo retornar um JSON fora do contrato, o `LLMGateway` faz automaticamente uma tentativa de reparo enviando os erros de validação do Pydantic de volta ao modelo.
- **Degradação graciosa**: se a Vertex AI estiver inacessível por erro de permissão/configuração (`LLMUnavailable`), o orquestrador degrada apenas o agente afetado para o analisador heurístico determinístico e registra a limitação no relatório final.

<p align="center">
  <img src="../assets/banners/footer.svg" alt="SIMIO Footer" width="100%">
</p>
