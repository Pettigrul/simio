<p align="center">
  <img src="assets/Simioo_BannerReadme.png" alt="SIMIO - Sistema de Investigação de Marca e Identidade Online" width="100%">
</p>

<p align="center">
  <img src="assets/badges/osint.svg" alt="OSINT">
  <img src="assets/badges/search-grounding.svg" alt="Search Grounding">
  <img src="assets/badges/agentes.svg" alt="Agentes">
  <img src="assets/badges/monitoramento.svg" alt="Monitoramento">
  <img src="assets/badges/marca.svg" alt="Marca">
  <img src="assets/badges/alertas.svg" alt="Alertas">
</p>

<p align="center">
  <strong>Harness multiagente determinístico para descoberta ativa, coleta segura, auditoria regulatória (Resolução CMN nº 4.935/2021) e detecção de uso indevido ou usurpação de marca (<em>brand impersonation</em>).</strong>
</p>

<p align="center">
  <img src="assets/banners/divisoria.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/protecao.svg" width="24" height="24" valign="middle"> 1. Objetivo

O **SIMIO** foi projetado para responder a uma pergunta central de conformidade, auditoria e segurança de marca:

> *A forma como a página ou perfil se apresenta pode induzir o consumidor a acreditar que está interagindo diretamente com a instituição financeira (ex.: Banco BV), quando na verdade se trata de um correspondente bancário ou terceiro independente?*

O sistema correlaciona cinco frentes fundamentais:
1. **Identidade Visual**: Proeminência de logotipo, cores institucionais e banners acima da dobra (`viewport.png`).
2. **Identidade Declarada e Societária**: CNPJ e razão social da operadora do site versus CNPJs oficiais do próprio banco.
3. **Transparência Regulatória**: Posição e clareza da declaração obrigatória de correspondente bancário (art. 14, III da Resolução CMN nº 4.935/2021).
4. **Jornada de Atendimento e Captação**: Oferta de serviços críticos (emissão de 2ª via de boleto, quitação com desconto, renegociação) direcionados para números de WhatsApp de terceiros.
5. **Contexto Mercadológico**: Diferenciação precisa de menções legítimas (lojas multimarcas de veículos seminovos que operam com múltiplos bancos concorrentes como Santander, Itaú e Pan).

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/regras.svg" width="24" height="24" valign="middle"> 2. Tecnologias Utilizadas e Papel de Cada Uma

| Tecnologia | Finalidade no SIMIO |
|---|---|
| **Python 3.11+ (`asyncio`)** | Motor central assíncrono. Coordena tarefas paralelas de rede, workers e banco com isolamento de falhas. |
| **Google GenAI SDK (`google-genai`)** | Integração oficial com a Vertex AI para invocar modelos Gemini (multimodalidade e busca aterrada). |
| **Google Search Grounding** | Mecanismo de busca em tempo real do Gemini na web para execução orientada de hipóteses e dorks OSINT. |
| **Serper (Google SERP API)** | Executa os dorks **literalmente** (`site:`, `inurl:`, `intitle:`, `OR`, `-site:`), em paralelo ao Grounding. |
| **Resolvedor DNS assíncrono** | Testa milhares de domínios semelhantes aos oficiais (*typosquatting* e *combosquatting*) sem acessar as páginas. |
| **Playwright (Chromium Headless)** | Navegador real com sandbox ativo para renderizar páginas dinâmicas (SPA/JS), gerar prints e extrair o DOM. |
| **Proxy de Egress Dedicado (`EgressProxy`)** | Servidor proxy HTTP/CONNECT local criado por página para bloqueio anti-SSRF e mitigação de DNS Rebinding. |
| **SQLite com Migrações (`sqlite3`)** | Banco relacional local (`simio.db`). Garante transições de estado atômicas, controle de orçamento e retomada (`resume`). |
| **Pydantic v2 & Pydantic-Settings** | Contratos de dados estritos (`extra="forbid"`), validação de esquemas de saída do Gemini e leitura tipada de `.env`. |
| **HTTPX** | Cliente HTTP assíncrono para validação de conexões, checagem de URLs e resolução segura de redirecionamentos. |
| **TLDExtract** | Extração offline de domínio registrável (ex.: separa subdomínios de plataformas e aplica decaimento por repetição). |
| **OpenPyXL & Jinja2** | Geração da planilha Excel consolidada (8 abas com proteção anti-fórmula) e dos dossiês periciais HTML. |
| **Typer** | Interface de linha de comando (CLI) intuitiva e estruturada. |

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/busca.svg" width="24" height="24" valign="middle"> 3. Inteligência de Cibersegurança: OSINT Dorks & Google Search Grounding

A descoberta de ameaças do SIMIO não depende de buscas ingênuas ou manuais. Ele implementa uma camada de **Threat Intelligence e OSINT (Open Source Intelligence)** especializada em superfícies de ataque contra marcas e correspondentes bancários.

### <img src="assets/icones/dorks.svg" width="20" height="20" valign="middle"> 3.1. Cinco fontes de descoberta, uma única fila

| Fonte | O que faz | Custo |
|---|---|---|
| **`grounding`** | O Gemini recebe cada hipótese (com seus dorks) e pesquisa no Google; só entram URLs dos metadados oficiais de busca. | Cota grátis mensal de buscas, depois por consulta |
| **`serper`** | Executa cada dork **ao pé da letra** no Google e devolve até 10 resultados por página. | 1 crédito por página (2.500 grátis no cadastro) |
| **`typosquat`** | Gera cerca de 1.900 variações dos domínios oficiais e mantém só as que **resolvem em DNS para IP público**. | Gratuito |
| **`crtsh`** | Certificados SSL emitidos para nomes com o padrão da marca (Certificate Transparency). | Gratuito |
| **`brave` / `urlscan`** | Conectores opcionais, ativados quando há chave no `.env`. | Conforme o provedor |

Todas as fontes rodam **intercaladas**: um orçamento pequeno de consultas continua cobrindo todas as frentes.

### <img src="assets/icones/busca.svg" width="20" height="20" valign="middle"> 3.2. Vetores cobertos (independentes da lista de COBANs)

São **38 hipóteses e 128 dorks** de *brand protection*, todos com exclusão dos domínios oficiais e de ruído (Reclame Aqui, Jusbrasil, tribunais):

| Vetor | Exemplos de dorks |
|---|---|
| **Usurpação de identidade e logotipo** | `intitle:"Banco BV"`, `inurl:logo-bv`, `"BV FINANCEIRA S.A."` em site de terceiro |
| **Vínculo e linguagem oficial** | `"parceiro oficial" "BV Financeira"`, `"Somos correspondente" "Banco BV"`, `"correspondente" "Banco Votorantim"` |
| **Produtos** | Veículos, motos, energia solar, consignado, crédito com garantia, `"bancos parceiros"` |
| **Regional (27 UFs)** | `"BV Financeira" "bancos parceiros" veículos "Tocantins"`, agrupados por macrorregião |
| **Serviços sensíveis** | 2ª via de boleto, quitação, acordo, SAC/0800, simuladores e páginas de login |
| **Fraudes típicas (H10)** | Leilão e "retomados", venda de ágio e repasse, falso empréstimo com taxa antecipada, app/APK falso, central de atendimento falsa, "agência BV" em diretórios, vagas falsas, investimento e cartão falsos, "limpa nome" |
| **Plataformas** | Instagram, Facebook, TikTok, LinkedIn, YouTube, Kwai, Threads, Telegram, grupos de WhatsApp, OLX, Webmotors, Mercado Livre, iCarros, Mobiauto |
| **Marca NaPista (H11)** | Sites e perfis usando "NaPista" ou "Feirão NaPista" como canal próprio |

### <img src="assets/icones/imitacao.svg" width="20" height="20" valign="middle"> 3.3. Domínios semelhantes (*typosquatting*)
Inspirado no `dnstwist`: omissão, repetição, troca de posição, tecla vizinha, caractere parecido (`0`/`o`, `rn`/`m`), troca de vogal, hífen, troca de extensão (`.com`, `.app`, `.online`...) e **combosquatting** (`feiraobv.com.br`, `bvoficial.com.br`, `quitarbvfinanceira.com`), a forma mais abusada na prática. Antes de começar, o conector verifica se o DNS responde para nomes inexistentes; se responder, ele se desativa para não gerar falsos positivos.

### <img src="assets/icones/validado.svg" width="20" height="20" valign="middle"> 3.4. Busca complementar guiada por COBANs (H9)
Opcional e sempre **depois** da varredura aberta. Pesquisa o nome de correspondentes cadastrados junto com a marca para identificar **qual COBAN** está usando a marca de forma indevida. A seleção começa pelos cadastros mais recentes e muda a cada campanha, até percorrer toda a base. Nomes de MEI com CPF nunca são pesquisados (LGPD).

### <img src="assets/icones/grounding.svg" width="20" height="20" valign="middle"> 3.5. Garantias contra URLs inventadas
1. **Só URLs de metadados de busca**: no Grounding, links citados apenas no texto do modelo são **rejeitados**; valem só os de `grounding_metadata.grounding_chunks`.
2. **Redirecionamentos resolvidos com segurança**: links `vertexaisearch.cloud.google.com/grounding-api-redirect/...` são desfeitos sem seguir automaticamente (`follow_redirects=False`) e passam pela política anti-SSRF.
3. **Redes sociais**: o Grounding remove Instagram e Facebook dos resultados; por isso essas plataformas vêm do Serper (com uma busca de reserva no Yahoo). Links opacos `google.com/goto?...` não viram candidatos e ficam registrados para conferência manual.
4. **Importação manual**: listas de ferramentas de *threat intel* (Spiderfoot, Maltego, feeds de phishing) entram com `--urls-file`.

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/agentes.svg" width="24" height="24" valign="middle"> 4. O Fluxo de Funcionamento Passo a Passo

O ciclo de vida de uma investigação é determinístico e composto por 7 fases:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ 1.DESCOBERTA│ ──► │  2. COLETA  │ ──► │3.IDENTIDADE │ ──► │ 4. MARCA    │
│(Dorks/Ground)     │(Chromium/SSRF)    │(CNPJ/COBAN) │     │ (Gemini/Reg)│
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                   │
┌─────────────┐     ┌─────────────┐     ┌─────────────┐            │
│ 7.RELATÓRIOS│ ◄── │ 6. DECISÃO  │ ◄── │ 5. REVISÃO  │ ◄──────────┘
│(Excel/HTML) │     │(Guardrails) │     │(Cético/Downg│
└─────────────┘     └─────────────┘     └─────────────┘
```

### <img src="assets/icones/busca.svg" width="18" height="18" valign="middle"> Fase 1: Descoberta de Superfície (`DiscoveryAgent`)
- Planeja as consultas de todas as fontes a partir de 11 famílias de hipóteses:

| Hipótese | Foco |
|---|---|
| **H1** Identidade | Marca no título, na URL e em arquivos de logotipo |
| **H2** Produtos | Veículos, motos, solar, consignado, garantia, "bancos parceiros" |
| **H3** Serviço e vínculo | Correspondente, simulação e captação por WhatsApp |
| **H4** Linguagem oficial | "Canal oficial", "parceiro oficial", CNPJ e razão social do banco |
| **H5** Regional | Feirões e lojas nas 27 UFs |
| **H6** Plataformas | Redes sociais, vídeo, Telegram, WhatsApp e marketplaces |
| **H7** Threat intel | Serviços sensíveis, portais de login e simuladores, urlscan, crt.sh |
| **H8** Domínios semelhantes | Typosquatting, combosquatting e troca de extensão |
| **H9** COBANs | Correspondentes cadastrados (opcional, sempre por último) |
| **H10** Fraudes | Leilão, ágio, falso empréstimo, app falso, central falsa, vagas, investimento |
| **H11** NaPista | Uso indevido da marca do marketplace do grupo |

- **Higienização ativa**: exclui domínios e perfis oficiais do banco, joga notícias, reclamações e processos para o fim da fila e aplica decaimento a URLs repetidas do mesmo domínio.

### <img src="assets/icones/coleta.svg" width="18" height="18" valign="middle"> Fase 2: Coleta Passiva e Segura (`BrowserCollector` & `EgressProxy`)
- Inicia o Chromium Headless com **sandbox do sistema operacional ativado**.
- Para cada página, sobe um proxy local dedicado (`EgressProxy`) que:
  - Valida a URL contra a política de rede;
  - Resolve o DNS previamente e **bloqueia IPs de intranet/privados** (`10.0.0.0/8`, `192.168.0.0/16`, `127.0.0.1`, endpoints de nuvem `169.254.169.254`);
  - Realiza **DNS Pinning** (conecta diretamente no IP público validado, mitigando *DNS Rebinding*).
- Abre posts e Reels do Instagram e do Facebook pelos endpoints públicos de *embed*, que mostram imagem, perfil e legenda sem exigir login.
- Captura evidências: `page.html.txt`, screenshots (`viewport.png` e `fullpage.png`), textos do DOM e gera manifesto indexado com **hash SHA-256**.

### <img src="assets/icones/classificacao.svg" width="18" height="18" valign="middle"> Fase 3: Identificação de Titularidade (`IdentityAgent`)
- Extrai e valida o algoritmo de todos os CNPJs presentes na página.
- **Separação de Identidade**: Identifica se o CNPJ é do próprio banco ou da empresa responsável pela página.
- Extrai razões sociais e perfis sociais.
- Cruza com a base oficial de correspondentes cadastrados (`CobanRegistry`), classificando em:
  - `CONFIRMED_COBAN` (Correspondente homologado, CNPJ exato na base);
  - `POSSIBLE_COBAN` (Outro estabelecimento da mesma empresa na base, ou declara ser correspondente sem confirmação);
  - `OTHER_THIRD_PARTY` (Terceiro independente);
  - `UNKNOWN_IDENTITY` (Página anônima).

### <img src="assets/icones/marca.svg" width="18" height="18" valign="middle"> Fase 4: Análise de Marca e Conformidade (`BrandAnalyst` & `ComplianceAgent`)
- O **`BrandAnalyst`** aciona o Gemini Multimodal com o print da tela e textos dentro de tags seguras `<conteudo_nao_confiavel>` (sem ferramentas ativas, prevenindo *prompt injection*).
  - Avalia o destaque do logotipo e cores;
  - Analisa a presença de discursos de transparência;
  - Detecta se é loja multimarca legítima (menção simultânea a múltiplos bancos concorrentes como Santander e Itaú);
  - Possui *fallback* determinístico offline caso a IA esteja indisponível.
- O **`ComplianceAgent`** enquadra o caso contra a **Resolução CMN nº 4.935/2021**:
  - **Art. 14, II**: Uso indevido de logotipo ou elementos visuais exclusivos da contratante.
  - **Art. 14, III**: Falta de declaração clara da condição de correspondente na página inicial ou omissão dos canais de ouvidoria/contato oficiais do banco.

### <img src="assets/icones/validado.svg" width="18" height="18" valign="middle"> Fase 5: Revisão Cética (`ReviewerAgent`)
- Atua como um auditor cético em todos os casos suspeitos.
- Valida se os argumentos da IA apontam para evidências reais do catálogo (`E-TITLE`, `E-TXT-001`, etc.).
- Tem poder de **rebaixar a criticidade (`DOWNGRADE`)**, solicitar nova coleta (`RECOLLECT`, máx. 1x) ou declarar inconclusivo, impedindo falsos positivos.

### <img src="assets/icones/regras.svg" width="18" height="18" valign="middle"> Fase 6: Decisão Final e Guardrails Determinísticos (`decision.py`)
- O modelo não dá a palavra final: são aplicadas regras matemáticas duras.
- Para receber a classificação máxima (`STRONG_INDICATION`), a página deve ter no mínimo 2 indicadores comprovados com evidências válidas.
- Divergências entre o modelo e as regras determinísticas forçam o caso para `REVIEW_REQUIRED` (revisão humana obrigatória).

### <img src="assets/icones/relatorio.svg" width="18" height="18" valign="middle"> Fase 7: Relatórios Consolidados (`Reporting`)
- **Planilha Excel (`.xlsx`)**: 8 abas de alta gerência e auditoria. Sanitiza textos para impedir ataques de *Spreadsheet Formula Injection*.
- **Dossiês HTML**: Gera páginas estáticas completas com histórico pericial, capturas de tela e evidências, blindadas por política restritiva de *Content-Security-Policy*.

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/alerta.svg" width="24" height="24" valign="middle"> 5. Casos de Uso

| Caso de Uso | Descrição |
|---|---|
| **<img src="assets/icones/imitacao.svg" width="18" height="18" valign="middle"> Monitoramento de Uso Indevido** | Detectar sites que fingem ser o canal oficial ou central 0800 do banco. |
| **<img src="assets/icones/validado.svg" width="18" height="18" valign="middle"> Fiscalização Regulatória (COBAN)** | Auditar se correspondentes contratados exibem claramente razão social, CNPJ e a condição de prestador de serviços. |
| **<img src="assets/icones/alerta.svg" width="18" height="18" valign="middle"> Combate a Fraudes de Boletos** | Mapear páginas que usam a marca para atrair clientes para WhatsApp falso prometendo quitação com desconto. |
| **<img src="assets/icones/alerta.svg" width="18" height="18" valign="middle"> Golpes com a Marca** | Encontrar leilões falsos, venda de ágio, empréstimo com taxa antecipada, apps e centrais falsas. |
| **<img src="assets/icones/web.svg" width="18" height="18" valign="middle"> Domínios Semelhantes** | Detectar domínios registrados parecidos com os oficiais antes que virem phishing. |
| **<img src="assets/icones/evidencia.svg" width="18" height="18" valign="middle"> Instrução Pericial e Takedowns** | Produção rápida de dossiês com capturas autenticadas por hashes SHA-256 para subsidiar notificações e processos. |

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/web.svg" width="24" height="24" valign="middle"> 6. Como Instalar

### Pré-requisitos
- Python 3.11+
- Projeto no Google Cloud com a Vertex AI habilitada
- Acesso à internet ou proxy corporativo configurado

### Instalação no Linux / macOS

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env
```

### Instalação no Windows (PowerShell)

```powershell
# 1. Acessar a pasta do projeto
cd C:\Users\pedro.felicio\projetos\simio

# 2. Criar e ativar o ambiente virtual
python.exe -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Instalar o pacote SIMIO e dependências
pip.exe install --upgrade pip
pip.exe install -e ".[dev]"

# 4. Instalar o Chromium do Playwright
python.exe -m playwright install chromium

# 5. Criar arquivo de configuração local
cp .env.example .env
```

### Configuração Mínima (`.env`)
Edite o arquivo `.env`:
```ini
SIMIO_GCP_PROJECT=<id-do-projeto-gcp>
SIMIO_GCP_LOCATION=global
SIMIO_ANALYSIS_MODEL=gemini-3.8-flash
SIMIO_REVIEW_MODEL=gemini-3.8-flash
SIMIO_DISCOVERY_MODEL=gemini-3.8-flash
```

### Configuração Recomendada para Varredura Ampla
Preencha `SIMIO_SERPER_API_KEY` com a sua chave do Serper (dorks executados literalmente) e ajuste:
```ini
SIMIO_MAX_SEARCH_QUERIES=300                 # cobre todas as hipóteses em todas as fontes
SIMIO_MAX_LLM_REQUESTS=2500
SIMIO_MAX_LLM_TOKENS=25000000
SIMIO_COBAN_REGISTRY_PATH=<caminho>/data/coban_registry_bv.csv
SIMIO_COBAN_REGISTRY_AUTHORITATIVE=true
SIMIO_COBAN_SEED_LIMIT=40                    # 0 desativa a busca guiada por COBANs (H9)
```

Autentique no Google Cloud via Application Default Credentials (ADC):
```powershell
gcloud auth application-default login
```

Valide todo o ambiente com o comando de diagnóstico:
```powershell
python.exe -m simio doctor
```

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/agentes.svg" width="24" height="24" valign="middle"> 7. Como Usar

### 7.1. Investigação Completa com Descoberta Ativa
Executa todas as fontes de descoberta, coleta e análise:
```powershell
python.exe -m simio hunt --brand bv --limit 50
```

Varredura ampla (recomendada para a rodada mensal):
```bash
python -m simio hunt --brand bv --limit 1000 --workers 5
```

> `--limit` é o número máximo de **candidatos analisados** (URLs únicas), não de buscas. A descoberta roda inteira; o limite só corta a fila de análise, que é ordenada por risco.

### 7.2. Investigação a partir de Lista de URLs
Examina links específicos fornecidos em um arquivo `.txt` ou `.csv`:
```powershell
python.exe -m simio hunt --urls-file "urls_suspeitas.txt" --limit 30
```

### 7.3. Modo 100% Offline (Sem Custos de LLM)
Navega nas URLs importadas e aplica exclusivamente as heurísticas locais:
```powershell
python.exe -m simio hunt --offline --urls-file "urls_suspeitas.txt"
```

### 7.4. Retomada de Campanha Interrompida
Se a execução for pausada por esgotamento de orçamento ou queda de conexão, retoma exatamente do ponto em que parou sem refazer análises de IA já concluídas:
```powershell
python.exe -m simio resume
```

### 7.5. Geração de Relatórios
Compila os relatórios da última campanha (ou use `-c <ID>`):
```powershell
python.exe -m simio report
```
Arquivos gerados em `output/reports/<CAMPAIGN_ID>/`:
- `simio_report_<CAMPAIGN_ID>.xlsx` (Planilha consolidada com 8 abas)
- `dossiers/<CANDIDATE_ID>.html` (Dossiês visuais completos)

### 7.6. Atualização Mensal da Lista de COBANs
Limpa a exportação de correspondentes ativos (remove duplicados, CNPJs inválidos e entidades do próprio banco) e grava a base usada pelo `IdentityAgent`:
```bash
python scripts/prepare_coban_registry.py Todos_Cobans_Ativos.xlsx --out data/coban_registry_bv.csv
```
A base fica em `data/`, que o git ignora. Depois, atualize `SIMIO_COBAN_REGISTRY_REFERENCE_DATE` no `.env`.

### 7.7. Monitoramento de Status
Consulta o painel de status da campanha e consumo de tokens:
```powershell
python.exe -m simio status
```

<p align="center">
  <img src="assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="assets/icones/classificacao.svg" width="24" height="24" valign="middle"> 8. Possibilidades e Extensibilidade

- **Perfis de Marca (`src/simio/brands/`)**: Adicione novos arquivos `.toml` para investigar outras instituições financeiras e marcas corporativas.
- **Novos Conectores**: Plugue novos motores de busca ou feeds de ameaças em `src/simio/providers/search.py`.
- **Novas Hipóteses e Dorks**: Adicione vetores em `build_hypotheses()` (`src/simio/agents/discovery.py`); eles passam a rodar em todas as fontes.
- **Typosquatting**: Ajuste sementes, rótulos e palavras do setor em `typosquat_seeds`, `typosquat_combo_labels` e `typosquat_keywords` no `.toml` da marca.
- **Novas Normativas**: Estenda as regras de conformidade adicionando dispositivos regulatórios em `src/simio/knowledge/`.

<p align="center">
  <img src="assets/banners/footer.svg" alt="SIMIO Footer" width="100%">
</p>
