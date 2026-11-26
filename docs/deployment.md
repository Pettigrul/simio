# Implantação e operação

Este guia cobre a instalação numa estação de trabalho corporativa. Não precisa de administrador, Docker nem banco externo: tudo roda no perfil do usuário e os dados ficam numa pasta local.

## Instalação

Windows (PowerShell):

```powershell
python.exe -m venv .venv
.venv\Scripts\Activate.ps1
pip.exe install --upgrade pip
pip.exe install -e ".[dev]"
python.exe -m playwright install chromium     # fica em %LOCALAPPDATA%\ms-playwright
```

Linux ou macOS:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
```

Se a rede usa um repositório interno de pacotes, defina `PIP_INDEX_URL` antes do `pip install`. Se o download do Chromium precisa passar por proxy, defina `HTTPS_PROXY` na sessão antes do `playwright install`.

## Vertex AI e Grounding

1. Autentique com a conta que tem acesso ao projeto:

   ```powershell
   gcloud auth application-default login
   ```

   Outra opção é apontar `SIMIO_CAMINHO_CREDENCIAIS_GCP` (ou `GOOGLE_APPLICATION_CREDENTIALS`) para o JSON de credenciais ADC no seu perfil.

2. No `.env` da raiz do projeto:

   ```ini
   SIMIO_PROJETO_GCP=<id-do-projeto>
   SIMIO_LOCAL_GCP=global
   SIMIO_MODELO_ANALISE=<modelo-gemini-multimodal>
   SIMIO_MODELO_REVISAO=        # vazio = mesmo da análise
   SIMIO_MODELO_DESCOBERTA=     # vazio = mesmo da análise
   ```

   Um modelo flash (por exemplo `gemini-3.8-flash`) serve para as três funções e custa menos.

3. Confira tudo:

   ```powershell
   python.exe -m simio diagnostico --real
   ```

   O diagnóstico verifica a versão do Python, a pasta de saída, o esquema do SQLite, se o Chromium abre com sandbox, o perfil `bv.toml`, a referência da logo antiga, o cadastro de COBANs e a configuração da Vertex. Com `--real`, faz também uma chamada `generate_content` e uma busca com Grounding.

## Proxy corporativo e inspeção TLS

Chamadas de API (Vertex AI e conectores de busca) usam o cliente `httpx` e o SDK do Google, que respeitam as variáveis do sistema: `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, e `SSL_CERT_FILE` ou `REQUESTS_CA_BUNDLE` para a autoridade certificadora interna.

O Chromium usa outro caminho. Para que a coleta passe pelo proxy corporativo:

```ini
SIMIO_PROXY_NAVEGADOR=http://proxy.interno:8080
```

O Chromium fala primeiro com o proxy local do SIMIO (`ProxySaida`), que valida a URL e resolve o DNS. Só então a conexão é encadeada para o proxy corporativo, mandando por padrão o IP público já validado (`SIMIO_PROXY_NAVEGADOR_FIXAR_IP=true`). Se o proxy recusa `CONNECT` para IP numérico porque filtra por nome de domínio, use `SIMIO_PROXY_NAVEGADOR_FIXAR_IP=false`; nesse caso a proteção contra DNS rebinding passa a depender do proxy.

Quando o filtro web bloqueia um site, o coletor reconhece a página de bloqueio, marca a coleta como Bloqueada (sem nova tentativa) e o relatório registra que o conteúdo real não pôde ser visto.

## Serper

No Grounding, os dorks são só orientação e o Gemini escreve as próprias buscas. O Serper executa cada dork como está escrito e é a principal fonte de resultados do Instagram e do Facebook.

```ini
SIMIO_CHAVE_API_SERPER=<chave>
SIMIO_PAGINAS_SERPER=1      # páginas de 10 resultados por dork, 1 crédito cada
```

- A conta gratuita dá 2.500 créditos no cadastro e no máximo 10 resultados por página. O conector pagina de 10 em 10 e para quando uma página volta incompleta.
- Sem chave, o conector fica indisponível e a campanha segue com as outras fontes.
- Quando o Google esconde a maior parte dos resultados de um dork atrás de links opacos, o SIMIO busca até `SIMIO_VARIANTES_SIMPLES_SERPER` versões sem operadores (padrão 3, 1 crédito cada, `0` desliga). Na prática uma campanha ampla gasta de 2 a 4 vezes o número de dorks, algo entre 400 e 700 créditos.
- A busca reversa de imagem (`lens`) usa a mesma chave. As imagens ficam em `urls_busca_logo_antiga` no `bv.toml` (3 créditos cada, até 60 resultados por imagem). Precisam ser URLs públicas que o Google consiga abrir; a da Wikimedia, por exemplo, não traz resultado.
- Candidatas com pontuação abaixo de `SIMIO_PONTUACAO_MINIMA_FILA` (padrão `-10`) não são analisadas; as outras entram em ordem de pontuação até o `--limite`.
- Parte dos resultados vem como `google.com/goto?url=...`. Quando há domínio de destino, a página inicial entra na fila; o registro de cada consulta mostra quantos foram recuperados e quantos ficaram sem destino ([security.md](security.md#descoberta)).

Consumo típico por modo:

| Modo | Serper | Gemini |
|---|---|---|
| Ampla | 128 dorks mais variantes e até 40 consultas de COBANs (400 a 700 créditos) | 38 prompts de Grounding mais cerca de 2 chamadas por página |
| `--foco logo-antiga` | cerca de 100 créditos (47 dorks, 40 COBANs, 3 imagens) | poucas dezenas de chamadas, porque só páginas com indício da logo vão aos modelos |
| `--foco ameacas` | 41 dorks (41 a 160 créditos) | 21 prompts de Grounding e todas as páginas da fila |

Na campanha de ameaças, cerca de 1.900 domínios parecidos são testados por DNS e o crt.sh é consultado, ambos sem custo (o crt.sh às vezes fica instável). Uma campanha de logo antiga com cerca de 230 páginas levou uns 20 minutos com `--paralelismo 5`.

## Inteligência de domínio (RDAP e DNS)

Não precisa de chave nem tem custo. Roda no fim do `investigar` e do `retomar` para os achados (Indício forte, Requer revisão ou página que pede credencial) e grava `resultados/campanhas/<id>/dominios.json`.

Leva cerca de um minuto para 50 hosts, porque as consultas RDAP são espaçadas em 1,2 s (Registro.br, ARIN e RIPE limitam rajadas). Hosts que falharem podem ser refeitos depois:

```powershell
python.exe -m simio enriquecer -c <ID>               # só os hosts ainda sem dados
python.exe -m simio enriquecer -c <ID> --atualizar   # consulta tudo de novo
```

Em rede corporativa, libere `rdap.org`, os servidores RDAP dos registros (`rdap.registro.br`, `rdap.arin.net`, `rdap.db.ripe.net`, `rdap.verisign.com`...) e `cloudflare-dns.com`, ou aponte `SIMIO_URL_DOH` para o DoH interno. `SIMIO_URL_BASE_RDAP` troca o redirecionador RDAP e `SIMIO_INTELIGENCIA_DOMINIO=false` desliga a etapa.

## Cadastro de correspondentes (COBANs)

Prepare a base todo mês a partir da exportação de COBANs ativos:

```bash
python scripts/preparar_cadastro_coban.py Todos_Cobans_Ativos.xlsx --saida data/coban_registry_bv.csv --data-referencia 2026-10-06
```

O script aceita `.xlsx` ou `.csv` com as colunas `cnpj`, `nmpessoa`, `dtcadastro` e `login_parceiro`. Ele remove CNPJs inválidos e entidades do próprio banco (raízes de `cnpjs_instituicao` no `bv.toml` e contas internas como "COMISSAO PARCERIA"), junta linhas do mesmo CNPJ com vários logins de parceiro e corrige acentuação corrompida. Sem `--data-referencia`, tenta ler a data do nome do arquivo ou da aba.

Depois, no `.env`:

```ini
SIMIO_CAMINHO_CADASTRO_COBAN=data/coban_registry_bv.csv
SIMIO_CADASTRO_COBAN_OFICIAL=true       # só se a base for fonte oficial ou aprovada
SIMIO_DATA_REFERENCIA_CADASTRO_COBAN=2026-10-06
SIMIO_LIMITE_SEMENTES_COBAN=40          # correspondentes pesquisados por campanha (padrão 0 = H9 desligada)
SIMIO_LOTE_SEMENTES_COBAN=4             # correspondentes por prompt de busca
```

Como a base é usada:

| Uso | Efeito |
|---|---|
| Classificação | CNPJ exato na base: Correspondente confirmado (só com `SIMIO_CADASTRO_COBAN_OFICIAL=true`). Mesma raiz de CNPJ (matriz ou filial): Possível correspondente, com confiança média. |
| Busca guiada (H9) | Pesquisa o nome do correspondente junto com a marca, começando pelos cadastros mais recentes e em rodízio entre campanhas. |
| Varredura ampla | Não depende da base. H1 a H8, H10 e H11 rodam sempre. |

A base tem dados de parceiros e fica só na máquina de execução (`data/` está no `.gitignore`).

## Limites e custo

| Opção | Variável | Padrão | Na prática |
|---|---|---|---|
| `--limite` | | 50 | 50 para uma triagem rápida; 1000 na varredura mensal |
| `--max-consultas` | `SIMIO_MAX_CONSULTAS_BUSCA` | 40 | 300 cobre todas as hipóteses em todas as fontes; 0 para analisar só `--arquivo-urls` |
| `--max-requisicoes-llm` | `SIMIO_MAX_REQUISICOES_LLM` | 300 | cerca de 2 por página mais 1 por prompt de Grounding; para `--limite 1000`, 2500 |
| `--max-tokens` | `SIMIO_MAX_TOKENS_LLM` | 2000000 | cerca de 21 mil por página; para `--limite 1000`, 25000000 |
| `--paralelismo` | `SIMIO_PARALELISMO` | 3 | 3 a 5 numa máquina local; mais que isso pesa na memória por causa das abas do Chromium |

Com gemini-3.8-flash, cada página analisada custa perto de US$ 0,03. Uma varredura de 1.000 páginas fica entre US$ 20 e 35. Se o limite acabar, a campanha fica como `PAUSADA_ORCAMENTO` e continua com `simio retomar --max-requisicoes-llm <novo> --max-tokens <novo>`.

## Migrar do layout antigo

As versões anteriores gravavam em `output/`, com banco em inglês, ids de campanha como `simio-20261118-184932-cc75` e evidências em `output/campaigns/<id>/evidence/<candidata>/attempt-N/`. O código atual lê e grava em `resultados/`.

1. Faça uma cópia do banco (`output/simio.db`). A migração do esquema não tem volta.
2. Abra o banco antigo com o SIMIO atual para levar o esquema à v3. Qualquer comando serve:

   ```bash
   python -m simio status -o output
   ```

3. Veja o plano e depois execute:

   ```bash
   python scripts/migrar_resultados.py --simular
   python scripts/migrar_resultados.py
   ```

   `--origem` e `--destino` mudam as pastas (padrão `output` e `resultados`).

O que o script faz:

- Recusa rodar se houver banco nas duas pastas, se o banco não estiver na v3 ou se alguma campanha tiver dado sinal de vida nos últimos 5 minutos.
- Dá a cada campanha o id novo (`AAAA-MM-DD_HHhMM_marca_modo`, a partir da data de criação) e grava o de/para em `resultados/ids_campanhas.json` e no campo `id_anterior` dos parâmetros da campanha.
- Move as evidências para `evidencias/<host>_<id>/tentativa-N/` com os nomes novos (`viewport.png` vira `tela-inicial.png`, `fullpage.png` vira `pagina-inteira.png`, `page.html.txt` vira `codigo-fonte.html.txt`, `text.txt` vira `texto-visivel.txt`, `page.json` vira `dados-da-pagina.json`, `manifest.json` vira `manifesto.json`). O conteúdo não muda. Como os manifestos antigos citam os nomes antigos, a lista de/para fica em `evidencias/renomeacoes.json`.
- Move `domain-intel.json` para `dominios.json`, os relatórios antigos (`output/reports/<id>/`) para `anteriores/legado/` e o log para `resultados/registros/simio.jsonl`.
- Nunca sobrescreve arquivo com conteúdo diferente. Pode ser rodado de novo se parar no meio.
- No final confere o SHA-256 de cada arquivo contra o manifesto e o do manifesto contra o banco, roda `PRAGMA integrity_check`, lista o que ficou para trás em `output/` e sugere os comandos `simio relatorio -c <id>` para gerar os relatórios no layout novo.

Os nomes das variáveis de ambiente, comandos e opções também mudaram. O `.env` antigo não dá erro: as variáveis com nome antigo são simplesmente ignoradas, então confira o `.env` com `simio diagnostico` depois de renomear.

| Antes | Agora |
|---|---|
| `SIMIO_GCP_PROJECT`, `SIMIO_GCP_LOCATION`, `SIMIO_GCP_CREDENTIALS_PATH` | `SIMIO_PROJETO_GCP`, `SIMIO_LOCAL_GCP`, `SIMIO_CAMINHO_CREDENCIAIS_GCP` |
| `SIMIO_ANALYSIS_MODEL`, `SIMIO_REVIEW_MODEL`, `SIMIO_DISCOVERY_MODEL` | `SIMIO_MODELO_ANALISE`, `SIMIO_MODELO_REVISAO`, `SIMIO_MODELO_DESCOBERTA` |
| `SIMIO_LLM_MAX_OUTPUT_TOKENS`, `SIMIO_LLM_TEMPERATURE` | `SIMIO_LLM_MAX_TOKENS_SAIDA`, `SIMIO_LLM_TEMPERATURA` |
| `SIMIO_MAX_LLM_REQUESTS`, `SIMIO_MAX_LLM_TOKENS`, `SIMIO_MAX_SEARCH_QUERIES` | `SIMIO_MAX_REQUISICOES_LLM`, `SIMIO_MAX_TOKENS_LLM`, `SIMIO_MAX_CONSULTAS_BUSCA` |
| `SIMIO_WORKERS`, `SIMIO_MAX_ATTEMPTS`, `SIMIO_MAX_RECOLLECTS` | `SIMIO_PARALELISMO`, `SIMIO_MAX_TENTATIVAS`, `SIMIO_MAX_RECOLETAS` |
| `SIMIO_SERPER_API_KEY`, `SIMIO_BRAVE_API_KEY`, `SIMIO_URLSCAN_API_KEY` | `SIMIO_CHAVE_API_SERPER`, `SIMIO_CHAVE_API_BRAVE`, `SIMIO_CHAVE_API_URLSCAN` |
| `SIMIO_SERPER_PAGES`, `SIMIO_SERPER_PLAIN_VARIANTS`, `SIMIO_MIN_QUEUE_SCORE` | `SIMIO_PAGINAS_SERPER`, `SIMIO_VARIANTES_SIMPLES_SERPER`, `SIMIO_PONTUACAO_MINIMA_FILA` |
| `SIMIO_BROWSER_PROXY`, `SIMIO_BROWSER_PROXY_PIN_IP`, `SIMIO_BROWSER_SANDBOX` | `SIMIO_PROXY_NAVEGADOR`, `SIMIO_PROXY_NAVEGADOR_FIXAR_IP`, `SIMIO_SANDBOX_NAVEGADOR` |
| `SIMIO_NAV_TIMEOUT_MS`, `SIMIO_PAGE_BUDGET_S`, `SIMIO_IGNORE_HTTPS_ERRORS` | `SIMIO_TIMEOUT_NAVEGACAO_MS`, `SIMIO_ORCAMENTO_PAGINA_S`, `SIMIO_IGNORAR_ERROS_HTTPS` |
| `SIMIO_ALLOWED_PORTS`, `SIMIO_UNSAFE_ALLOW_HOSTS` | `SIMIO_PORTAS_PERMITIDAS`, `SIMIO_HOSTS_LIBERADOS_INSEGURO` |
| `SIMIO_DOMAIN_INTEL`, `SIMIO_RDAP_BASE_URL`, `SIMIO_DOH_URL` | `SIMIO_INTELIGENCIA_DOMINIO`, `SIMIO_URL_BASE_RDAP`, `SIMIO_URL_DOH` |
| `SIMIO_OUTPUT_DIR` (padrão `output`) | `SIMIO_DIR_SAIDA` (padrão `resultados`) |
| `SIMIO_COBAN_REGISTRY_PATH`, `SIMIO_COBAN_REGISTRY_AUTHORITATIVE`, `SIMIO_COBAN_REGISTRY_REFERENCE_DATE` | `SIMIO_CAMINHO_CADASTRO_COBAN`, `SIMIO_CADASTRO_COBAN_OFICIAL`, `SIMIO_DATA_REFERENCIA_CADASTRO_COBAN` |
| `SIMIO_COBAN_SEED_LIMIT`, `SIMIO_COBAN_SEED_BATCH` | `SIMIO_LIMITE_SEMENTES_COBAN`, `SIMIO_LOTE_SEMENTES_COBAN` |
| `SIMIO_CONTRACT_REQUIREMENTS_PATH`, `SIMIO_LOG_LEVEL` | `SIMIO_CAMINHO_REQUISITOS_CONTRATUAIS`, `SIMIO_NIVEL_LOG` |

Comandos: `hunt` virou `investigar`, `resume` virou `retomar`, `enrich` virou `enriquecer`, `report` virou `relatorio`, `doctor` virou `diagnostico` e `test` virou `testar`. Opções: `--brand` é `--marca`, `--limit` é `--limite`, `--workers` é `--paralelismo`, `--focus` é `--foco`, `--sources` é `--fontes`, `--urls-file` é `--arquivo-urls`, `--max-queries` é `--max-consultas`, `--max-llm-requests` é `--max-requisicoes-llm`, `--campaign` é `--campanha`, `--retry-failed` é `--repetir-falhas`, `--refresh` é `--atualizar` e `--live` é `--real`.

No perfil de marca, as chaves também foram traduzidas (por exemplo `legacy_logo_references` é `referencias_logo_antiga` e `typosquat_seeds` é `sementes_typosquat`). O arquivo de pesos antigo (`scoring.toml`) ainda é lido se não houver `pontuacao.toml`, mas os nomes dos pesos mudaram (`w_multi_bank_context` agora é `p_contexto_multibanco`, por exemplo) e nomes antigos são ignorados sem aviso. A lista atual está em [methodology.md](methodology.md#pesos-da-heurística).
