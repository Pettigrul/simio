<p align="center">
  <img src="../assets/logos/logo-claro.svg" alt="SIMIO - Metodologia" width="340">
</p>

<p align="center">
  <img src="../assets/badges/osint.svg" alt="OSINT">
  <img src="../assets/badges/marca.svg" alt="Marca">
  <img src="../assets/badges/search-grounding.svg" alt="Search Grounding">
  <img src="../assets/badges/alertas.svg" alt="Alertas">
</p>

# Metodologia de Investigação e Análise

<p align="center">
  <img src="../assets/banners/divisoria.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/protecao.svg" width="22" height="22" valign="middle"> 1. A pergunta central da investigação

O objetivo do SIMIO não é apontar qualquer página que mencione o nome do **Banco BV**, mas responder a uma pergunta objetiva de auditoria e proteção de marca:

> **A forma como a página ou perfil se apresenta pode induzir o consumidor a acreditar que está interagindo diretamente com o Banco BV, quando na verdade está lidando com um correspondente bancário ou terceiro independente?**

Para responder a essa pergunta sem depender de uma única palavra-chave isolada, o SIMIO correlaciona cinco frentes:

1. **Identidade declarada**: como o site se intitula no `<title>`, no cabeçalho, no `og:site_name`, no domínio e no rodapé (razão social e CNPJ).
2. **Identidade visual e destaque da marca**: presença de logotipo acima da dobra, cores e estrutura de página avaliadas a partir dos screenshots (`viewport.png` e `fullpage.png`).
3. **Identidade efetiva do responsável**: quem realmente opera a página (`IdentityAgent`), diferenciando CNPJs do próprio banco citados no texto do CNPJ da empresa responsável pelo site.
4. **Transparência da relação comercial**: se existe declaração expressa da condição de correspondente bancário (Resolução CMN nº 4.935/2021, art. 14, III), onde ela está posicionada (topo/área visível vs. escondida no rodapé) e se ela nomeia a instituição contratante.
5. **Jornada de atendimento e captação**: presença de linguagem em primeira pessoa, alegação de "canal oficial / central 0800 / SAC" ou captação ativa via WhatsApp para serviços críticos como **2ª via de boleto, quitação de contrato com desconto e renegociação**.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/busca.svg" width="22" height="22" valign="middle"> 2. Estratégia de descoberta e pré-filtragem (`DiscoveryAgent`)

### <img src="../assets/icones/dorks.svg" width="18" height="18" valign="middle"> 2.1. Construção das consultas e hipóteses
Em vez de executar listas genéricas de dorks, o `DiscoveryAgent` gera consultas estruturadas em torno dos padrões reais de abuso de marca e omissão de transparência:

- **H1 (`H1-identity`)**: uso da marca no `<title>` (`intitle:"Banco BV"`, `intitle:"BV Financeira"`, `intitle:"BV Financiamentos"`, `intitle:"BV Promotora"`, `intitle:"Agência BV"`, `intitle:"BV Veículos"`) ou na URL (`inurl:banco-bv`, `inurl:bv-financeira`).
- **H2 (`H2-product`)**: páginas de produtos financeiros (financiamento de veículos, energia solar, refinanciamento, empréstimo com garantia) mantidas por terceiros com aparência de canal próprio do banco.
- **H3 (`H3-service`)**: captação para WhatsApp ou telefone oferecendo `"2ª via"`, `"segunda via"`, `"boleto"`, `"quitação"`, `"simulação"` ou `"atendimento"` em nome da marca.
- **H4 (`H4-official-language`)**: páginas externas que utilizam expressões como `"canal oficial"`, `"site oficial"`, `"central de atendimento"`, `"parceiro oficial"`, `"representante oficial"` ou que publicam a razão social `"BV FINANCEIRA S.A."` e os CNPJs oficiais do banco (`59.588.111/0001-03` e `01.858.774/0001-10`).
- **H5 (`H5-establishment`)**: divulgação de `"Feirão Banco BV"` / `"Feirão BV Financeira"` e varredura regional das **27 UFs** (uma hipótese por macrorregião), onde a identidade da empresa responsável não fica evidente.
- **H6 (`H6-platform`)**: Instagram, Facebook, TikTok, LinkedIn, YouTube, Kwai, Threads, Telegram, grupos de WhatsApp e marketplaces (OLX, Webmotors, Mercado Livre, iCarros, Mobiauto).
- **H7 (`H7-threat-intel`)**: páginas de 2ª via, quitação e acordo, portais de login e simuladores com a marca, além de urlscan.io e certificados SSL (crt.sh).
- **H8 (`H8-lookalike`)**: domínios semelhantes aos oficiais que resolvem em DNS (typosquatting, combosquatting e troca de extensão). Entram na fila com prioridade extra.
- **H9 (`H9-coban`)**, opcional: nome de correspondentes cadastrados junto com a marca, para descobrir qual COBAN usa a marca de forma indevida. Roda sempre depois da varredura aberta.
- **H10 (`H10-fraud`)**: golpes típicos de financeira de veículos: leilão e veículos "retomados", venda de ágio e repasse, falso empréstimo com taxa antecipada, app/APK falso e phishing, central de atendimento falsa, "agência BV" em diretórios, vagas falsas, investimento e cartão falsos, "limpa nome".
- **H11 (`H11-napista`)**: uso indevido da marca NaPista em sites e redes sociais.
- **H12 (`H12-legacy-logo`)**: busca reversa de imagem (Google Lens) com uma cópia pública da logo antiga; encontra páginas que usam a imagem sem escrever a marca. Bancos de imagens (Shutterstock, Pinterest, etc.) vão para o fim da fila.

> **Grounding × Serper.** No Grounding, os dorks orientam o Gemini, que escreve as próprias buscas e pode ignorar `inurl:` e `intitle:`. No Serper, cada dork é executado literalmente. As duas fontes rodam juntas para combinar abrangência e precisão.

Todas as consultas já saem com exclusões explícitas dos domínios oficiais (`-site:bv.com.br -site:bancobv.com.br -site:bvfinanceira.com.br -site:napista.com.br ...`) e das principais fontes de ruído jurídico/reclamação (`-site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br`).

### <img src="../assets/icones/coleta.svg" width="18" height="18" valign="middle"> 2.2. O que é excluído ou despriorizado antes da coleta
Para evitar que o limite de candidatos (`--limit`) seja consumido por falsos positivos óbvios:

1. **Exclusão imediata (registrada no log de auditoria)**:
   - Domínios oficiais do ecossistema do banco (`bv.com.br`, `bancobv.com.br`, `bvfinanceira.com.br`, `napista.com.br`, `bvopen.com.br`, `meufinanciamentosolar.com.br`, `votorantim.com.br`);
   - Perfis e postagens oficiais do próprio banco nas redes sociais (`instagram.com/bancobv`, `facebook.com/bancobv`, `facebook.com/napistapp`, `tiktok.com/@bancobv`, `linkedin.com/company/bancobv`, `x.com/bancobv`);
   - Páginas automáticas de busca/tags de redes sociais (`instagram.com/popular/...`, `instagram.com/explore/...`, `tiktok.com/discover/...`, `facebook.com/public/...`).
2. **Despriorização (jogados para o final da fila)**:
   - Portais de notícias, imprensa, órgãos públicos (`gov.br`, `bcb.gov.br`), Reclame Aqui, Proteste, Jusbrasil, tribunais (`jus.br`), diretórios de CNPJ (`econodata.com.br`, `cnpj.biz`), plataformas de RH (`indeed.com`, `glassdoor.com.br`) e repositórios de logotipos (`brandsoftheworld.com`, `logospng.org`);
   - Anúncios individuais de veículos usados em classificados (`olx.com.br/.../autos-e-pecas/`, `carro.mercadolivre.com.br/MLB-...`, `webmotors.com.br/comprar/`, `mobiauto.com.br/comprar/`), **exceto** quando vêm de uma hipótese de fraude (H10), como um anúncio de "leilão BV";
   - Artigos informativos, blogs, estudos de caso e centrais de ajuda de software (`Como adicionar o simulador...`, `/blog/`, `ajuda.*`);
   - Subpáginas puramente burocráticas (`/politica-de-privacidade`, `/termos-de-uso`, `/lgpd`, `sitemap.xml`);
   - **Decaimento por repetição de domínio**: a partir da 3ª URL de um mesmo domínio registrável (exceto plataformas compartilhadas como Instagram/Facebook ou URLs importadas manualmente), o score sofre penalidade progressiva para garantir variedade de estabelecimentos analisados, preservando sempre a página inicial (*home page*) sem penalidade.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/validado.svg" width="22" height="22" valign="middle"> 3. Como os falsos positivos são evitados na análise

Mesmo quando uma página entra na coleta, o SIMIO aplica critérios contextuais no `compute_signals()`, na heurística, nos prompts do `BrandAnalyst` e no `Reviewer` para não transformar menções legítimas em alertas:

### 3.1. Lojas multimarcas e integradores solares (`multi_bank_context`)
É comum que lojas de carros seminovos, concessionárias e integradores de energia solar tenham uma página `/financiamento` exibindo os logotipos dos bancos com os quais operam.
- Quando a página opera sob domínio próprio de terceiro, não reivindica ser canal oficial do banco e menciona/exibe o Banco BV **junto com outras instituições financeiras concorrentes** (`Santander`, `Itaú`, `Banco Pan`, `Bradesco`, `Safra`, `C6`, `Solfácil`, `BTG`, etc.), o SIMIO ativa o sinal `multi_bank_context`.
- Esse sinal é contabilizado como indicador de relação comercial intermediada (`w_multi_bank_context = 2.0`), classificando a página no **Cenário A (`NO_INDICATION`)**.
- Da mesma forma, títulos comparativos ou multibanco (ex.: *"Financiamento de Energia Solar | Empresa X (BV e BTG)"* ou *"Financiamento pelos bancos Santander e BV"*) não são tratados como uso da marca como identidade própria do site.

### 3.2. Declaração de correspondente bancário
- Uma frase como *"X Ltda é correspondente bancário do Banco BV"* precisa citar o nome do banco para cumprir o art. 14, III da Resolução CMN nº 4.935/2021. Por isso, o SIMIO remove as frases de divulgação (`without_disclosures`) antes de medir se a marca está sendo usada como título ou cabeçalho do site.
- Declarações que nomeiam a instituição e aparecem na área visível inicial/cabeçalho recebem peso alto de transparência (`w_disclosure_prominent = 2.5`).
- Declarações genéricas (*"somos empresa independente"* sem dizer de qual banco) ou contraditas na mesma frase (*"não somos correspondente, somos o Banco BV oficial"*) não recebem crédito de transparência plena.

### 3.3. Homônimos da sigla curta `"BV"` (`homonym_context`)
A sigla `"BV"` isolada é ambígua. O SIMIO evita falsos positivos de três formas:
1. Em texto corrido do corpo da página, apenas termos fortes (`"Banco BV"`, `"BV Financeira"`, `"Banco Votorantim"`, `"BV Financiamentos"`) são contados em `brand_mentions`.
2. Sufixos societários estrangeiros (`"Example Holding BV"`, `"B.V."`) e partes de URLs/e-mails são ignorados.
3. No título, cabeçalho ou domínio, a sigla curta `"BV"` só é considerada se houver contexto bancário/financeiro na página e não se tratar de um homônimo mapeado (`"Boa Vista"`, `"Roraima"`, `"Bela Vista"`, `"Agência de Marketing/Publicidade/Modelos"`, `"Bioinformatics"`).
   - *Proteção contra falso negativo*: se a página contiver marcadores bancários explícitos (como `"Banco"`, `"Financeira"`, `"Correspondente"`, `"SAC BV"`, `"0800"` ou oferta de `"2ª via de boleto"` / `"quitação"`), o filtro de homônimos não é ativado, impedindo que uma página maliciosa use a palavra `"Boa Vista"` no rodapé para escapar da análise.

### 3.4. Conteúdo editorial, notícias, reclamações e processos (`editorial_context`)
Páginas identificadas como artigos de imprensa, blogs educativos, tutoriais de sistemas B2B, reclamações no Reclame Aqui ou processos judiciais que apenas mencionam o banco em terceira pessoa — sem usar domínio da marca, sem cabeçalho próprio da marca e sem captação ativa via WhatsApp — são classificadas no **Cenário A (`NO_INDICATION`)**. Discursos reportados (*"segundo o site oficial do Banco BV"*, *"alerta sobre golpe"*) são separados em `reported_claims` e não contam como autoapresentação.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/alerta.svg" width="22" height="22" valign="middle"> 4. Sinais de alto risco (verdadeiros positivos priorizados)

1. **Isca de serviços pós-venda via WhatsApp (`sensitive_service_claims`)**:
   - Páginas de terceiros que oferecem `"2ª via de boleto"`, `"quitação de financiamento"`, `"desconto na quitação"`, `"antecipação de parcelas"` ou `"renegociação de dívida"` associadas à marca e captam o cliente para WhatsApp/formulário sem transparência destacada de correspondente ou assessoria credenciada recebem peso alto de confusão (`w_sensitive_service_bait = 2.5`) e são enquadradas nos cenários **C ou D (prioridade `HIGH`)**.
2. **Exibição indevida do CNPJ oficial da instituição (`w_institution_cnpj_impersonation`)**:
   - Quando um site externo exibe no rodapé ou no corpo o CNPJ oficial do próprio Banco BV (`59.588.111/0001-03` ou `01.858.774/0001-10`) sem informar nenhum CNPJ próprio da empresa responsável pela página e sem declaração de correspondente, o sistema registra um indicador de força `HIGH` em `DECLARED_COMMERCIAL_IDENTITY`.
3. **Alegação de canal oficial e domínios lookalike**:
   - Expressões como `"canal oficial"`, `"site oficial"`, `"central de atendimento BV"`, `"SAC BV"`, `"ambiente seguro Banco BV"` ou domínios contendo o padrão da marca (`bancobv-*`, `bvfinanon`, `bkbv`, `bvsol`) elevam diretamente o caso para revisão prioritária.
   - Domínios encontrados pelo typosquatting (`bancobva.com.br`, `feiraobv.com.br`) chegam com a técnica usada registrada (ex.: "marca combinada com palavra do setor") e recebem prioridade na fila.
4. **Identidade visual antiga (logo descontinuada)**:
   - A logo antiga (B vermelho/carmim sobre V azul) não pode ser usada por terceiros, nem por correspondentes contratados. O logo atual tem o mesmo desenho, em uma única cor.
   - **Detecção em duas camadas independentes**: o Gemini preenche `legacy_visual_identity` ao ver a logo; a comparação de imagem (`agents/visual.py`) compara as regiões dos prints com a referência **separadamente nas tintas vermelha e azul** (o logo atual, só azul, nunca casa) e registra `E-VIS-LEGACY-*` com posição e similaridade.
   - **Regra**: com qualquer uma das camadas, o caso fica no mínimo `REVIEW_REQUIRED` com prioridade `HIGH`, e o revisor não pode descartá-lo sozinho. O relatório informa a origem ("modelo", "comparação de imagem" ou ambos).
   - **Campanha focada** (`--focus logo-antiga`): hipóteses próprias (H13: vitrine multibanco, páginas de financiamento, energia solar, crédito/consignado e uma consulta por UF) + busca reversa (H12) + COBANs do cadastro (H9). Fontes padrão: `serper,lens`. Páginas sem logo confirmada e sem imagem com o nome da marca no arquivo/alt são encerradas como `NO_INDICATION` sem chamar os modelos, com a limitação registrada.
   - **Exceção**: sites que só catalogam ou noticiam a logo (repositórios de logos, bancos de imagens, enciclopédias, imprensa), listados em `legacy_logo_exempt_domains`, registram o achado como "site de referência" sem elevar a classificação. Redes sociais e marketplaces não entram nessa lista.
   - **Limitações**: logos exibidos em escala de cinza (ex.: carrossel de parceiros com filtro) não permitem distinguir a versão antiga da atual; logo aplicado diretamente sobre fundo vermelho pode escapar da comparação de imagem e depende do Gemini.
5. **Golpes que usam a marca como isca**:
   - Leilão de veículos "retomados", venda de ágio, crédito "pré-aprovado" mediante taxa, aplicativo para baixar fora das lojas oficiais e vagas de "digitador/correspondente" são buscados ativamente (H10) e analisados pelo mesmo fluxo de evidências.

<p align="center">
  <img src="../assets/banners/divisoria-simples.svg" alt="Divisória" width="100%">
</p>

## <img src="../assets/icones/classificacao.svg" width="22" height="22" valign="middle"> 5. Guardrails determinísticos e revisão (`core/decision.py` e `agents/reviewer.py`)

As respostas do modelo Gemini são tratadas como propostas sujeitas a validação determinística:

- **Validação de referências**: toda evidência citada pelo modelo precisa existir no catálogo (`E-TITLE`, `E-TXT-001`...). Referências inexistentes são descartadas; indicadores que ficarem sem referência válida são removidos.
- **Requisito mínimo para `STRONG_INDICATION`**: exige pelo menos 2 indicadores de confusão com referências válidas (`strong_min_indicators = 2`), coleta utilizável e suficiência de evidência diferente de `INSUFFICIENT`. Caso contrário, é rebaixado para `REVIEW_REQUIRED`.
- **Coerência Cenário × Classificação**:
  - `E_INSUFFICIENT_EVIDENCE` força `INCONCLUSIVE`;
  - `C_POSSIBLE_OFFICIAL_CHANNEL` ou `D_STRONG_IMPERSONATION` nunca podem ter classificação `NO_INDICATION`;
  - `A_LEGITIMATE_COMMERCIAL_MENTION` ou `NOT_RELEVANT` com classificação de alerta são convertidos em `REVIEW_REQUIRED`;
  - `B_AMBIGUOUS_COMMUNICATION` com `NO_INDICATION` é elevado a `REVIEW_REQUIRED` se houver indicadores de confusão válidos, ou ajustado para `A_LEGITIMATE_COMMERCIAL_MENTION` caso não haja nenhum indicador de confusão.
- **Cruzamento Modelo × Regras (`cross_check`)**: se o modelo responder `NO_INDICATION`, mas a heurística determinística apontar cenário B, C ou D, o candidato é forçado para `REVIEW_REQUIRED` com revisão humana obrigatória.
- **Papel do Reviewer**: atua como filtro cético nos casos suspeitos ou duvidosos. Pode confirmar, pedir nova coleta (`RECOLLECT`), declarar `INCONCLUSIVE` ou rebaixar (`DOWNGRADE` — por exemplo, quando há declaração de correspondente na página ou contexto multimarcas), mas nunca pode promover sozinho um caso para `STRONG_INDICATION`.

<p align="center">
  <img src="../assets/banners/footer.svg" alt="SIMIO Footer" width="100%">
</p>
