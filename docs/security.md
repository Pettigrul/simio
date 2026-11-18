# Segurança e privacidade

## Modelo de ameaça

Páginas investigadas são controladas por terceiros potencialmente hostis. O SIMIO roda em máquina
corporativa com acesso a redes internas e credenciais de nuvem. Riscos principais: SSRF/acesso à
rede interna, prompt injection, vazamento de segredos/dados, execução no host via navegador,
conteúdo malicioso em relatórios.

## Navegação

- **Proxy de egress por página** (`security/egress_proxy.py`): todo tráfego do Chromium passa por
  um proxy local que valida o destino com `UrlPolicy`, resolve DNS ele mesmo e conecta ao **IP
  validado** (DNS fixado — elimina o TOCTOU de DNS rebinding). Necessário porque `route()` do
  Playwright **não é chamado em saltos de redirecionamento** (comprovado em teste de regressão).
- Chromium com `--proxy-bypass-list=<-loopback>` (loopback não é isento), `--disable-quic`, WebRTC
  restrito a UDP proxificado; proxy global fictício ("fail closed") caso um contexto não defina o
  seu.
- `UrlPolicy`: apenas http/https; sem credenciais na URL; portas permitidas; bloqueio de
  `localhost`, `*.local`, `*.internal`, `metadata.google.internal`, nomes de rótulo único; todos os
  IPs resolvidos devem ser públicos (privados, loopback, link-local 169.254/16, CGNAT, multicast,
  reservados, IPv4-mapeado, 6to4/Teredo, NAT64, formas numéricas legadas como `2130706433`).
- Sem preenchimento de formulários, cliques, login, downloads (`accept_downloads=False`), service
  workers bloqueados, popups fechados, diálogos descartados, sem permissões; sandbox do Chromium
  mantido; limite de tempo por página, de requisições e de tamanho de HTML/texto/screenshot.
- Desafios anti-bot/CAPTCHA são detectados e registrados; **não** há tentativa de contorno.
- HTML bruto é salvo como `page.html.txt` para nunca ser renderizado por engano.
- Proxy corporativo: `SIMIO_BROWSER_PROXY` encadeia o proxy de egress ao proxy da organização.
- `SIMIO_UNSAFE_ALLOW_HOSTS` existe **apenas para testes locais**; `simio doctor` alerta se ativo.

## LLM e prompt injection

- Conteúdo coletado vai somente na mensagem do usuário, dentro de `<conteudo_nao_confiavel>`
  (com neutralização da tag de fechamento); as regras ficam no *system instruction*.
- Chamadas de análise/revisão **não têm ferramentas** (sem busca, sem function calling): não há
  caminho de ação ou exfiltração a partir do modelo. O Grounding recebe apenas hipóteses geradas
  pelo próprio SIMIO, nunca conteúdo de páginas.
- Saídas validadas por Pydantic; referências a evidências inexistentes são descartadas.
- Padrões de injeção são detectados deterministicamente; o caso nunca termina como
  `NO_INDICATION` e exige revisão humana.
- URLs de descoberta vêm somente dos metadados de grounding/APIs, nunca do texto do modelo.

## Relatórios

- Dossiês: Jinja2 com autoescape, CSP `default-src 'none'`, sem scripts, links de terceiros
  exibidos como texto (não clicáveis).
- Excel: neutralização de fórmulas (`=`, `+`, `-`, `@`) e de caracteres de controle.

## Segredos e dados

- Segredos apenas por variável de ambiente/`.env` local (nunca versionado); `SecretStr` nas
  configurações; logs com redação de padrões de chave/token; `Settings.redacted()` para registros.
- Credenciais Google via ADC; o SIMIO não lê, copia nem armazena tokens e não usa mecanismos não
  oficiais (ex.: sessões do Antigravity).
- Sem telemetria. Resultados ficam em `output/` (ignorado pelo Git) na máquina corporativa.
- `.gitignore` bloqueia `.env*`, credenciais, bancos, planilhas, imagens (exceto fixtures), logs e
  diretórios de evidência. `scripts/check_secrets.py [--history]` audita árvore e histórico antes
  de publicar.
- Nunca copie dados de investigação do ambiente corporativo para máquinas pessoais.

## Riscos residuais

- O conteúdo de páginas pode induzir o modelo a erros sutis de julgamento (não a ações): mitigado
  por guardrails, revisor e revisão humana obrigatória para alertas.
- Com proxy corporativo encadeado, a resolução final de DNS é feita pelo proxy da organização
  (a validação local ocorre antes); a política do proxy corporativo complementa a proteção.
- JavaScript de páginas públicas executa no Chromium (necessário à renderização), confinado pelo
  sandbox do navegador e pelo proxy de egress.
