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
- **Sandbox do Chromium ativo** (`SIMIO_BROWSER_SANDBOX=true`; o Playwright o desativa por padrão).
  Teste de regressão verifica a ausência de `--no-sandbox` nos processos.
- Respostas reescritas com `Connection: close` (nenhuma requisição é encaminhada sem passar pela
  validação); limite de bytes transferidos por página; texto/HTML truncados dentro da página.
- `UrlPolicy`: apenas http/https; sem credenciais na URL; portas permitidas; bloqueio de
  `localhost`, `*.local`, `*.internal`, `metadata.google.internal`, nomes de rótulo único; todos os
  IPs resolvidos devem ser públicos (privados, loopback, link-local 169.254/16, CGNAT, multicast,
  reservados, IPv4-mapeado, 6to4/Teredo, NAT64, formas numéricas legadas como `2130706433`).
- Sem preenchimento de formulários, cliques, login, downloads (`accept_downloads=False`), service
  workers bloqueados, popups fechados, diálogos descartados, sem permissões; sandbox do Chromium
  mantido; limite de tempo por página, de requisições e de tamanho de HTML/texto/screenshot.
- Desafios anti-bot/CAPTCHA são detectados e registrados; **não** há tentativa de contorno.
- HTML bruto é salvo como `page.html.txt` para nunca ser renderizado por engano.
- Proxy corporativo: `SIMIO_BROWSER_PROXY` encadeia o proxy de egress ao proxy da organização
  **mantendo o DNS fixado**: o proxy corporativo recebe o IP validado (`CONNECT 203.0.113.7:443`,
  `GET http://203.0.113.7/...` com o `Host` original), com `Proxy-Authorization` também no CONNECT.
- `SIMIO_UNSAFE_ALLOW_HOSTS` existe **apenas para testes locais**; `simio doctor` alerta se ativo.

## LLM e prompt injection

- Conteúdo coletado vai somente na mensagem do usuário, dentro de `<conteudo_nao_confiavel>`
  (com neutralização da tag de fechamento); as regras ficam no *system instruction*.
- Chamadas de análise/revisão **não têm ferramentas** (sem busca, sem function calling): não há
  caminho de ação ou exfiltração a partir do modelo. O Grounding recebe apenas hipóteses geradas
  pelo próprio SIMIO, nunca conteúdo de páginas.
- Saídas validadas por Pydantic; referências a evidências inexistentes são descartadas.
- Padrões de injeção são detectados deterministicamente (texto, metadados, títulos, alt de imagens);
  o caso nunca termina como `NO_INDICATION` e exige revisão humana.
- **Verificação cruzada modelo × regras**: se as regras determinísticas veem cenário B/C/D e o
  modelo responde `NO_INDICATION` (possível persuasão por texto da página ou de imagens, que
  regex não detecta), o caso é forçado a `REVIEW_REQUIRED` com revisão humana.
- URLs e erros de coleta (influenciáveis pelo site) também ficam dentro do delimitador, que é
  neutralizado em qualquer combinação de maiúsculas/espaços.
- URLs de descoberta vêm somente dos metadados de grounding/APIs, nunca do texto do modelo.

## Relatórios

- Dossiês: Jinja2 com autoescape, CSP `default-src 'none'`, sem scripts, links de terceiros
  exibidos como texto (não clicáveis).
- Excel: neutralização de fórmulas (`=`, `+`, `-`, `@`) e de caracteres de controle.

## Segredos e dados

- Segredos apenas por variável de ambiente/`.env` local (nunca versionado); `SecretStr` nas
  configurações; logs com redação de chaves/tokens, cabeçalhos `Authorization`/`Proxy-Authorization`
  (Basic/Bearer) e URLs com credenciais; `Settings.redacted()` para registros.
- Credenciais Google via ADC; o SIMIO não lê, copia nem armazena tokens e não usa mecanismos não
  oficiais (ex.: sessões do Antigravity).
- Sem telemetria. Resultados ficam em `output/` (ignorado pelo Git) na máquina corporativa.
- `.gitignore` bloqueia `.env*`, credenciais, bancos, planilhas, imagens (exceto fixtures), logs e
  diretórios de evidência. `scripts/check_secrets.py [--history]` audita árvore e histórico antes
  de publicar (chaves, tokens, chaves privadas, URLs com credenciais, cabeçalhos de autenticação,
  nomes `*.env*`). Fixtures de teste montam segredos falsos em tempo de execução; a marcação
  `# pragma: allowlist secret` só é aceita em `tests/`.
- Nunca copie dados de investigação do ambiente corporativo para máquinas pessoais.

## Riscos residuais

- O conteúdo de páginas pode induzir o modelo a erros sutis de julgamento (não a ações): mitigado
  por guardrails, revisor e revisão humana obrigatória para alertas.
- Com `SIMIO_BROWSER_PROXY_PIN_IP=false` (proxy corporativo que exige hostname), a resolução final
  de DNS volta a ser do proxy da organização: um domínio com DNS rebinding poderia direcionar a
  requisição a destino interno alcançável pelo proxy. Mantenha `true` sempre que possível.
- Sem sandbox (`SIMIO_BROWSER_SANDBOX=false`), uma falha explorável do renderizador executaria
  código com o usuário do operador (acesso a ADC, `.env`, rede). Prefira usuário dedicado sem ADC.
- `SIMIO_UNSAFE_ALLOW_HOSTS` desativa a proteção SSRF para os hosts listados; o `hunt` alerta e
  registra o uso na campanha. Nunca use fora de testes.
- JavaScript de páginas públicas executa no Chromium (necessário à renderização), confinado pelo
  sandbox do navegador e pelo proxy de egress.
