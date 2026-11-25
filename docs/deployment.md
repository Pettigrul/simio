<p align="center">
  <img src="../assets/logos/logo-claro.svg" alt="SIMIO - Implantação e Operação" width="340">
</p>

<p align="center">
  <img src="../assets/badges/automação.svg" alt="Automação">
  <img src="../assets/badges/agentes.svg" alt="Agentes">
  <img src="../assets/badges/monitoramento.svg" alt="Monitoramento">
</p>

# Guia de Implantação e Operação no Ambiente Corporativo

<p align="center">
  <img src="../assets/banners/divisoria.svg" alt="Divisória" width="100%">
</p>

Este guia descreve como configurar e operar o SIMIO em uma estação de trabalho corporativa sem necessidade de privilégios de administrador, Docker ou banco de dados externo.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/web.svg" width="22" height="22" valign="middle"> 1. Instalação do ambiente Python e Chromium

```powershell
# 1. Criar e ativar o ambiente virtual (opcional se usar instalação no perfil do usuário)
python.exe -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Instalar o pacote SIMIO e dependências de desenvolvimento/teste
pip.exe install --upgrade pip
pip.exe install -e ".[dev]"

# 3. Instalar o navegador Chromium do Playwright (fica salvo em %LOCALAPPDATA%\ms-playwright)
python.exe -m playwright install chromium
```

No Linux ou macOS, os comandos equivalentes são:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
```

Se a sua rede exigir repositório interno de pacotes Python, defina `PIP_INDEX_URL` antes do `pip.exe install`. Caso o download do Chromium precise passar por proxy corporativo, defina `HTTPS_PROXY` na sessão antes de rodar `playwright install chromium`.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/grounding.svg" width="22" height="22" valign="middle"> 2. Configuração da Vertex AI e Google Search Grounding

1. **Autenticação (ADC)**:
   Autentique via Google Cloud CLI com a conta autorizada no projeto GCP:
   ```powershell
   gcloud auth application-default login
   ```
   *(Alternativamente, aponte `SIMIO_GCP_CREDENTIALS_PATH` no `.env` para o arquivo JSON de credenciais ADC no seu perfil de usuário).*

2. **Arquivo `.env` na raiz do projeto**:
   ```ini
   SIMIO_GCP_PROJECT=<id-do-projeto-gcp>
   SIMIO_GCP_LOCATION=global
   SIMIO_ANALYSIS_MODEL=<modelo-gemini-multimodal>
   SIMIO_REVIEW_MODEL=<modelo-gemini-revisor>
   SIMIO_DISCOVERY_MODEL=<modelo-gemini-grounding>
   ```
   Um modelo *flash* (ex.: `gemini-3.8-flash`) atende as três funções com custo menor.

3. **Diagnóstico completo**:
   ```powershell
   python.exe -m simio doctor --live
   ```
   O comando verifica: versão do Python, bibliotecas instaladas, permissão de escrita em `output/`, migrações do SQLite, abertura real do Chromium headless com sandbox, perfil `bv.toml`, credenciais ADC, uma chamada real `generate_content` à Vertex AI e uma busca real com Google Search Grounding.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/protecao.svg" width="22" height="22" valign="middle"> 3. Operação com Proxy Corporativo e Inspeção TLS

### 3.1. Chamadas de API (Vertex AI e conectores de busca)
O SDK da Vertex AI e o cliente HTTP (`httpx`) respeitam automaticamente as variáveis padrão do sistema operacional:
- `HTTPS_PROXY`, `HTTP_PROXY` e `NO_PROXY`;
- `SSL_CERT_FILE` e `REQUESTS_CA_BUNDLE` (para autoridades certificadoras corporativas).

### 3.2. Navegador Chromium (`SIMIO_BROWSER_PROXY`)
Para que a coleta de páginas pelo Chromium passe por um proxy corporativo, configure no `.env`:
```ini
SIMIO_BROWSER_PROXY=http://endereco-do-proxy:8080
```
Como funciona internamente:
- O Chromium conecta primeiro ao `EgressProxy` local do SIMIO (que valida a URL contra a política anti-SSRF e resolve o DNS).
- Em seguida, o `EgressProxy` encadeia a conexão para o `SIMIO_BROWSER_PROXY` enviando por padrão o **IP público já validado** (`SIMIO_BROWSER_PROXY_PIN_IP=true`).
- Se o proxy corporativo bloquear requisições `CONNECT` direcionadas a endereços IP numéricos (exigindo o nome de domínio para aplicar filtros de categoria web), defina `SIMIO_BROWSER_PROXY_PIN_IP=false` no `.env`.
- Quando um site é bloqueado pelo filtro web corporativo (ex.: página de bloqueio de política de acesso), o coletor reconhece automaticamente o bloqueio, marca a coleta como `BLOCKED` (não repetível) e registra no relatório que o conteúdo real do site não pôde ser inspecionado devido ao filtro da rede.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/busca.svg" width="22" height="22" valign="middle"> 4. Serper: dorks executados literalmente

O Grounding usa os dorks como orientação, e o Gemini escreve as próprias buscas. O Serper executa cada dork exatamente como está escrito e é a principal fonte de resultados do Instagram e do Facebook.

Preencha `SIMIO_SERPER_API_KEY` no `.env` com a sua chave e, se quiser mais resultados por dork:

```ini
SIMIO_SERPER_PAGES=1      # páginas de 10 resultados por dork (1 crédito cada)
```

- **Conta gratuita**: 2.500 créditos no cadastro e no máximo 10 resultados por página. O conector já pagina de 10 em 10 e para quando uma página volta incompleta.
- **Consumo**: cerca de 170 créditos por rodada completa com `SIMIO_SERPER_PAGES=1`.
- **Sem chave**: o conector fica indisponível e a rodada segue só com as outras fontes.
- **Busca reversa de imagem (`lens`)**: usa a mesma chave. As imagens pesquisadas ficam em `legacy_logo_search_urls` no `bv.toml` (3 créditos cada). Precisam ser URLs públicas que o Google consiga abrir; a da Wikimedia, por exemplo, não retorna resultados.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/validado.svg" width="22" height="22" valign="middle"> 5. Cadastro de Correspondentes Autorizados (COBANs)

### 5.1. Preparar a base (mensalmente)
```bash
python scripts/prepare_coban_registry.py Todos_Cobans_Ativos.xlsx --out data/coban_registry_bv.csv
```
O script aceita `.xlsx` ou `.csv` com as colunas `cnpj`, `nmpessoa`, `dtcadastro` e `login_parceiro`, e:
- remove CNPJs inválidos;
- remove entidades do próprio banco (raízes de CNPJ de `institution_cnpjs` no `bv.toml` e nomes internos como "COMISSAO PARCERIA");
- junta linhas duplicadas do mesmo CNPJ (vários logins de parceiro) e corrige acentuação corrompida;
- lê a data de referência do nome do arquivo ou da aba.

### 5.2. Configurar
```ini
SIMIO_COBAN_REGISTRY_PATH=<caminho>/data/coban_registry_bv.csv
SIMIO_COBAN_REGISTRY_AUTHORITATIVE=true
SIMIO_COBAN_REGISTRY_REFERENCE_DATE=2026-10-06
SIMIO_COBAN_SEED_LIMIT=40     # correspondentes pesquisados por campanha (0 desativa a H9)
SIMIO_COBAN_SEED_BATCH=4      # correspondentes por prompt de busca
```

### 5.3. Como a base é usada
| Uso | Efeito |
|---|---|
| **Classificação** | CNPJ exato na base: `CONFIRMED_COBAN`. Mesma raiz de CNPJ (matriz/filial): `POSSIBLE_COBAN` com confiança média. |
| **Busca guiada (H9)** | Pesquisa nome do correspondente + marca, começando pelos cadastros mais recentes e alternando a cada campanha. |
| **Varredura aberta** | **Não depende da base.** H1–H8, H10 e H11 rodam sempre. |

A base contém dados de parceiros e deve ficar só na máquina de execução (`data/` está no `.gitignore`).

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/regras.svg" width="22" height="22" valign="middle"> 6. Dimensionamento de Orçamento e Concorrência

| Parâmetro CLI | Variável `.env` | Padrão | Recomendação prática |
|---|---|---|---|
| `--limit` | — | `50` | `50` para triagem rápida; `1000` para a varredura mensal completa |
| `--max-queries` | `SIMIO_MAX_SEARCH_QUERIES` | `40` | `300` cobre todas as hipóteses em todas as fontes; `0` para analisar só `--urls-file` |
| `--max-llm-requests` | `SIMIO_MAX_LLM_REQUESTS` | `300` | Cerca de 2 por candidato + 1 por prompt de Grounding. Para `--limit 1000`, use `2500` |
| `--max-tokens` | `SIMIO_MAX_LLM_TOKENS` | `2000000` | Cerca de 21 mil por candidato. Para `--limit 1000`, use `25000000` |
| `--workers` | `SIMIO_WORKERS` | `3` | `3` a `5` em máquinas locais; mais que isso pesa na memória por causa das abas do Chromium |

**Ordem de grandeza** (gemini-3.8-flash): cerca de US$ 0,03 por candidato analisado. Uma varredura de 1.000 candidatos fica entre US$ 20 e 35. Se o orçamento acabar, a campanha pausa como `PAUSED_BUDGET` e continua com `simio resume`.

<p align="center">
  <img src="../assets/banners/footer.svg" alt="SIMIO Footer" width="100%">
</p>
