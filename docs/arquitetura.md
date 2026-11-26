# Arquitetura

## Decisões de projeto

O SIMIO é Python 3.11+ com `asyncio` e SQLite, sem framework de orquestração de agentes. O motivo é simples: o caminho de cada página é fixo (descoberta, coleta, identidade, marca, conformidade, revisão, decisão) e precisa ser auditável. Os agentes não conversam entre si; o `Orquestrador` faz cada transição e passa modelos Pydantic estritos (`extra="forbid"`) de uma etapa para a outra.

Cada transição de estado, consulta de busca, evidência e chamada ao LLM é gravada na hora em `resultados/simio.db`. Por isso uma campanha interrompida pode ser retomada com `simio retomar` sem pagar de novo pelo que já foi analisado.

O Chromium nunca sai direto para a internet. Cada página ganha um proxy local (`ProxySaida`) que valida a URL, resolve o DNS, recusa IP privado e conecta no IP já validado. Detalhes em [seguranca.md](seguranca.md).

## Módulos

```
src/simio/
  cli.py                   comandos (Typer): investigar, retomar, enriquecer, relatorio, executivo, status, diagnostico, testar
  configuracoes.py         configurações tipadas (pydantic-settings, prefixo SIMIO_, lê o .env)
  config_logs.py           log JSONL em resultados/registros/simio.jsonl, com segredos mascarados
  nucleo/
    modelos.py             modelos Pydantic compartilhados (Candidata, Evidencia, AvaliacaoMarca, DecisaoFinal...)
    estado.py              máquina de estados da candidata e transições permitidas
    armazenamento.py       SQLite: esquema, migrações, checkpoints, trava de campanha, uso do LLM
    traducao_legado.py     migração v3 do banco: tabelas, colunas e valores de inglês para português
    caminhos.py            nomes de pastas e arquivos de saída e id legível da campanha
    orquestrador.py        pipeline assíncrono, paralelismo, novas tentativas, orçamento e retomada
    decisao.py             ConfigPontuacao, salvaguardas determinísticas e composição da DecisaoFinal
    urls.py                normalização de URL e domínio registrável (lista de sufixos offline)
  agentes/
    descoberta.py          hipóteses de busca (H1 a H14), modos de campanha, planejamento, deduplicação e fila
    evidencia.py           catálogo de evidências (E-*) e sinais determinísticos da página
    identidade.py          CNPJ (numérico e alfanumérico), razão social e cruzamento com o cadastro de COBANs
    marca.py               AnalistaMarca: Gemini multimodal, heurística offline e verificação cruzada
    conformidade.py        mapeamento para a Res. CMN 4.935/2021 e requisitos contratuais, sem LLM
    revisor.py             Revisor cético (LLM + contestações determinísticas)
    visual.py              comparação de imagem sem IA para a logo antiga e recortes
    triagem_ameacas.py     nível de ameaça (Crítico a Sem ameaça) a partir de fatos já gravados
    inteligencia_dominio.py  RDAP e DNS sobre HTTPS dos achados; detecção de plataformas de hospedagem
  provedores/
    llm.py                 GatewayLLM: orçamento, concorrência, novas tentativas e reparo de JSON
    vertex.py              cliente google-genai (Vertex AI) e Google Search Grounding
    grounding.py           conector de descoberta via Grounding, com resolução segura dos redirecionamentos
    busca.py               Serper, Google Lens via Serper, Brave, urlscan.io, crt.sh e importação de URLs
    typosquat.py           domínios parecidos com os oficiais, testados só por DNS
    mock.py                provedor em memória para os testes
  coletores/
    navegador.py           coleta com Playwright/Chromium: capturas, DOM, formulários e manifesto SHA-256
  seguranca/
    politica_url.py        política anti-SSRF (sintaxe da URL e DNS contra redes privadas)
    proxy_saida.py         proxy HTTP/CONNECT por página, com DNS fixado
  relatorios/
    __init__.py            gerar_relatorios(): arquiva a geração anterior e grava tudo de novo
    dados.py               carrega os registros de uma campanha e ordena por criticidade
    rotulos.py             rótulos legíveis dos códigos gravados no banco e paleta de cores
    textos_antigos.py      traduz, só na exibição, textos gravados em inglês por campanhas antigas
    dados_logo.py          logos do relatório embutidas em base64
    excel.py               relatorio.xlsx, com neutralização de fórmulas
    html_campanha.py       relatorio.html (base do PDF)
    html_ameacas.py        ameacas.html
    pdf.py                 PDFs dos relatórios, impressos pelo Chromium do Playwright
    executivo.py           relatorio_executivo.html/.pdf: soma todas as campanhas, cada URL uma vez
    dossie.py              dossiês HTML por página
    paginas.py             ambiente Jinja2 (autoescape)
    templates/             base.html.j2 (layout comum), relatorio_campanha, relatorio_ameacas, dossie, relatorio_executivo
  prompts/
    analista_marca.md      prompt do analista (analista-marca/v10)
    revisor.md             prompt do revisor (revisor/v7)
    modo_ameacas.md        adendo da campanha de ameaças (modo-ameacas/v3)
  conhecimento/
    cmn_4935.toml          texto literal dos artigos usados da Res. CMN 4.935/2021
  marcas/
    bv.toml                perfil do Banco BV
    assets/bv_logo_antiga.png  referência da logo antiga
scripts/
  preparar_cadastro_coban.py   limpa a exportação de COBANs e grava data/coban_registry_bv.csv
  verificar_segredos.py        procura segredos e arquivos proibidos nos arquivos do git (--historico: todos os commits)
  migrar_resultados.py         move campanhas de output/ para o layout de resultados/
tests/
  unitarios/  integracao/  e2e/  reais/    reais/ só roda com `simio testar --real`
```

## Máquina de estados da candidata

Cada URL única de uma campanha é uma candidata e segue os estados de `nucleo/estado.py`:

```
DESCOBERTA -> NA_FILA -> COLETANDO -> COLETADA -> ANALISANDO -> REVISANDO -> CONCLUIDA
                 ^           |                       |             |  \-> INCONCLUSIVA
                 +-----------+ falha transitória     |             +----> NA_FILA (nova coleta pedida pelo revisor)
                                                     +------------------> CONCLUIDA / INCONCLUSIVA
qualquer etapa -> FALHOU (depois de SIMIO_MAX_TENTATIVAS falhas)
```

O que isso garante:

- Toda transição é conferida contra `TRANSICOES`, gravada em `transicoes_estado` dentro de `BEGIN IMMEDIATE` e só acontece se a candidata ainda estiver no estado esperado.
- Uma campanha só roda em um processo por vez. Quem executa `investigar` ou `retomar` mantém uma trava em `travas_campanha` com pulso a cada 30 s; a trava vence depois de 120 s sem pulso.
- Um erro inesperado numa candidata a leva para `FALHOU` sem derrubar as outras nem o Chromium compartilhado.
- O contador de tentativas sobe antes de começar a coleta ou a análise. Uma página que derruba o processo várias vezes vai para `FALHOU` no `retomar` (`recuperar_em_andamento`) em vez de entrar em loop.
- Etapas interrompidas recomeçam de um estado estável (`RETOMAR_DE`): coleta volta para a fila, análise e revisão voltam para `COLETADA`.
- Cada avaliação (identidade, marca, conformidade, revisão, final) fica ligada ao id da evidência. Se a campanha parar na revisão, a análise de marca daquela evidência é reaproveitada sem nova chamada à Vertex AI.
- `FALHOU` só volta para a fila com `simio retomar --repetir-falhas`. O revisor pode pedir nova coleta até `SIMIO_MAX_RECOLETAS` vezes (padrão 1).

## Descoberta (`agentes/descoberta.py`)

```
montar_hipoteses(marca)              H1-H7, H10, H11   varredura ampla, não depende do cadastro de COBANs
hipoteses_coban(cadastro)            H9                opcional, sempre no fim da fila
ConectorTyposquat                    H8                só DNS, não abre páginas
BuscaLens                            H12               busca reversa da logo antiga
montar_hipoteses_logo_antiga(marca)  H13               só em --foco logo-antiga, no lugar da varredura ampla
montar_hipoteses_ameacas(marca)      H14 + H10 + H7    só em --foco ameacas, sem H9
            |
            v
plano: uma lista por fonte, intercaladas
  grounding: 1 prompt por hipótese     serper/brave: 1 consulta por dork
  typosquat: 1 consulta por semente    crtsh: padrões de domínio da marca
            |
            v
execução -> deduplicar() -> priorizar() -> fila (corta em --limite)
```

| Fonte | Unidade de consulta | Na varredura ampla do BV |
|---|---|---|
| `grounding` | prompt com a hipótese e seus dorks (o Gemini faz cerca de 18 buscas por prompt) | 38, mais até 10 de H9 |
| `serper` | um dork, executado como está escrito, em páginas de 10 resultados (`SIMIO_PAGINAS_SERPER`) | 128, mais até 40 de H9 |
| `typosquat` | uma semente (`typo:bancobv.com.br`) ou rótulo (`combo:bv`); cerca de 1.900 domínios no total | 7 |
| `crtsh` | padrão de domínio da marca | 3 |
| `lens` | URL pública de uma imagem da logo antiga (3 créditos cada) | 1 |

`SIMIO_MAX_CONSULTAS_BUSCA` corta a lista intercalada; 300 cobre o plano completo (cerca de 230 consultas). Como as fontes são intercaladas, um limite pequeno ainda passa por todas elas. No `retomar`, consultas já feitas na campanha são puladas.

O cadastro de COBANs entra em rodízio: `Armazenamento.cnpjs_coban_pesquisados()` sabe quais correspondentes já foram pesquisados com sucesso, e a campanha seguinte começa pelos que faltam.

Resultados `google.com/goto?url=...` do Serper que trazem o domínio exibido (`displayedLink`) viram candidata da página inicial daquele domínio (`recuperado_de_redirecionamento_google`, com uma pequena penalidade na fila). Redes sociais e resultados sem domínio ficam só no registro da consulta.

### Campanhas focadas

O foco (`OpcoesInvestigacao.foco`) fica gravado nos parâmetros da campanha e o `retomar` respeita.

- `AgenteDescoberta(foco=...)` troca as hipóteses da varredura ampla pelas do modo e pula fontes que não servem ao modo (`typosquat`, `crtsh` e `urlscan` na logo antiga; `lens` em ameaças). As fontes padrão estão em `FONTES_PADRAO_FOCO`.
- Na logo antiga, `_analisar()` roda primeiro a comparação de imagem e a identidade. Página sem logo confirmada e sem `pistas_logo_antiga()` (imagem com o nome da marca no arquivo ou no `alt`) é encerrada com uma avaliação `foco:logo-antiga` (Sem indício, ou Inconclusiva se a coleta falhou), sem chamar os modelos. A limitação fica registrada na decisão final.
- Em ameaças, `priorizar(..., foco="ameacas")` mantém e prioriza construtores de site gratuitos (que a varredura ampla descarta) e `FOCOS_SEM_COBANS` desliga a H9. Na análise, `classificar_campo_entrada()` transforma os campos de formulário em evidências `E-FORM-*`, e `aplicar_regra_dados_sensiveis()` põe o piso Requer revisão / Alta em página com a marca que pede credencial.

### Logo antiga na análise

Antes do `AnalistaMarca`, `visual.detectar_logo_antiga()` compara as capturas (já com hash) com as referências do perfil. Os achados entram no catálogo como `E-VIS-LEGACY-*` e `aplicar_regra_logo_antiga()` põe o piso Requer revisão / Alta, que `compor_final()` mantém mesmo depois do revisor.

### Inteligência de domínio

`agentes/inteligencia_dominio.py` (`enriquecer_campanha`) consulta RDAP do domínio e do IP e DNS sobre HTTPS para os hosts dos achados (Indício forte, Requer revisão ou página que pede credencial). `plataforma_hospedagem()` reconhece páginas hospedadas em plataformas (lista própria mais a parte privada da Public Suffix List). O resultado vai para `campanhas/<id>/dominios.json`, sem tabela no banco, e alimenta a aba Domínios, colunas da aba Ameaças e os dossiês. Roda no fim do `investigar` e do `retomar`, ou com `simio enriquecer`.

## Orçamento e falhas do LLM (`provedores/llm.py`)

- Antes de cada chamada, o `GatewayLLM` reserva no banco uma requisição e uma estimativa conservadora de tokens (`caracteres / 3 + 1800 por imagem + máximo de saída`). Assim vários trabalhadores em paralelo não passam de `--max-requisicoes-llm` nem de `--max-tokens`. A reserva é acertada com o uso real quando a resposta chega.
- Quando o orçamento acaba, o orquestrador para de chamar o modelo, devolve a candidata atual para `COLETADA`, encerra a campanha como `PAUSADA_ORCAMENTO` e gera relatórios parciais. `simio retomar` com limites maiores continua.
- Toda resposta é validada pelo modelo Pydantic correspondente (`SaidaAnaliseMarca` ou `SaidaRevisao`). Se vier fora do contrato, o gateway faz uma tentativa de reparo mandando os erros de validação de volta ao modelo.
- Se a Vertex AI estiver inacessível por permissão ou configuração (`LLMIndisponivel`), só o agente afetado cai para a heurística, e a limitação aparece no relatório.

## Banco e migrações (`nucleo/armazenamento.py`)

O esquema tem versão em `schema_migrations`. Um banco novo já nasce no esquema atual (v3). Um banco antigo é migrado ao ser aberto por qualquer comando:

- v1 e v2: esquema original, com tabelas e valores em inglês.
- v3: `traducao_legado.migrar_v3()` renomeia tabelas, colunas e índices e reescreve o conteúdo gravado (JSON das evidências e avaliações, códigos como `STRONG_INDICATION` para `INDICIO_FORTE`, famílias de hipótese como `H1-identity` para `H1-identidade`). Roda numa transação com as chaves estrangeiras desligadas e confere as referências no final.

A migração do banco não move arquivos. Para trazer pastas do layout antigo (`output/`) use `scripts/migrar_resultados.py`; veja [implantacao.md](implantacao.md#migrar-do-layout-antigo).

## Layout de saída (`nucleo/caminhos.py`)

Todos os nomes de pasta e arquivo saem de `caminhos.py`. O id da campanha é `AAAA-MM-DD_HHhMM_<marca>_<modo>` no horário de Brasília (`-2`, `-3` se repetir). Cada candidata é identificada nos arquivos por `<host>_<8 primeiros caracteres do id>`. As evidências ficam em `evidencias/<host>_<id>/tentativa-N/` e o banco guarda o caminho relativo à pasta da campanha, sempre com `/`. Antes de gravar relatórios novos, `arquivar_relatorios_anteriores()` move os atuais para `anteriores/<data>/` e corrige os links para as evidências. A árvore completa está no [README](../README.md#onde-ficam-os-resultados).
