# Metodologia

## A pergunta

O SIMIO não aponta toda página que cita o Banco BV. A pergunta é: a forma como a página ou o perfil se apresenta pode levar o consumidor a achar que está falando diretamente com o Banco BV, quando é um correspondente bancário ou outro terceiro?

Para responder sem depender de uma palavra-chave solta, cada página é avaliada por cinco ângulos:

1. Identidade declarada: como o site se chama no `<title>`, no cabeçalho, no `og:site_name`, no domínio e no rodapé (razão social e CNPJ).
2. Identidade visual: logotipo acima da dobra, cores e estrutura, a partir das capturas (`tela-inicial.png` e `pagina-inteira.png`).
3. Quem opera a página de fato (`AgenteIdentidade`), separando o CNPJ do banco citado no texto do CNPJ de quem mantém o site.
4. Transparência: se há declaração expressa da condição de correspondente (Res. CMN 4.935/2021, art. 14, III), se ela aparece no topo ou escondida no rodapé e se nomeia a instituição contratante.
5. Jornada de atendimento: primeira pessoa, "canal oficial", "central 0800", "SAC", e captação por WhatsApp para 2ª via de boleto, quitação com desconto e renegociação.

## Descoberta (`AgenteDescoberta`)

### Hipóteses

As consultas são montadas em torno de padrões reais de abuso, não de listas genéricas de dorks. Na varredura ampla do BV são 38 hipóteses e 128 dorks.

| Família | O que procura |
|---|---|
| H1-identidade | Marca no título (`intitle:"Banco BV"`, `intitle:"BV Financeira"`, `intitle:"Agência BV"`...) ou na URL (`inurl:banco-bv`, `inurl:bv-financeira`) e arquivos de logotipo |
| H2-produto | Financiamento de veículos e motos, energia solar, consignado, crédito com garantia, "bancos parceiros", em páginas de terceiros com cara de canal do banco |
| H3-servico | Captação por WhatsApp ou telefone para "2ª via", "boleto", "quitação", "simulação" ou "atendimento" em nome da marca |
| H4-linguagem-oficial | "canal oficial", "site oficial", "central de atendimento", "parceiro oficial", "representante oficial", ou a razão social e os CNPJs do banco em site de terceiro |
| H5-estabelecimento | "Feirão Banco BV" e varredura das 27 UFs, uma hipótese por macrorregião |
| H6-plataforma | Instagram, Facebook, TikTok, LinkedIn, YouTube, Kwai, Threads, Telegram, grupos de WhatsApp, OLX, Webmotors, Mercado Livre, iCarros, Mobiauto |
| H7-inteligencia-ameacas | 2ª via, quitação e acordo, portais de login e simuladores com a marca; urlscan.io e certificados (crt.sh) |
| H8-semelhante | Domínios parecidos com os oficiais que resolvem em DNS; entram na fila com prioridade extra |
| H9-coban | Opcional. Nome de correspondentes cadastrados junto com a marca, para saber qual COBAN usa a marca indevidamente. Sempre depois da varredura ampla |
| H10-fraude | Leilão e veículos "retomados", venda de ágio e repasse, empréstimo com taxa antecipada, app ou APK falso, central de atendimento falsa, "agência BV" em diretórios, vagas falsas, investimento e cartão falsos, "limpa nome" |
| H11-napista | Uso indevido da marca NaPista em sites e redes sociais |
| H12-logo-antiga | Busca reversa de imagem (Google Lens) com uma cópia pública da logo antiga; acha páginas que usam a imagem sem escrever a marca. Bancos de imagens vão para o fim da fila |
| H13-foco-logo-antiga | Só na campanha de logo antiga: vitrines de "bancos parceiros", financiamento, solar, consignado e uma consulta por UF (9 hipóteses, 47 dorks) |
| H14-ameaca | Só na campanha de ameaças: login e portal de parceiro falsos, hospedagem gratuita, boleto e Pix, taxa antecipada, painéis e venda de dados, perfis e apps falsos (junto com H10, H7 e Telegram: 21 hipóteses, 41 dorks) |

No Grounding, os dorks orientam o Gemini, que escreve as próprias buscas e pode ignorar `inurl:` e `intitle:`. No Serper, cada dork roda como está escrito. As duas fontes rodam juntas: uma cobre mais, a outra é mais precisa.

Todas as consultas já saem excluindo os domínios oficiais (`-site:bv.com.br -site:bancobv.com.br -site:bvfinanceira.com.br -site:napista.com.br ...`) e as fontes de ruído jurídico e de reclamação (`-site:reclameaqui.com.br -site:jusbrasil.com.br -site:jus.br`).

Com operadores (`site:`, `intitle:`, `inurl:`, `OR`, `-site:`), o Google esconde de 6 a 9 em cada 10 resultados atrás de links opacos (`google.com/goto?url=...`), que nunca são seguidos. Nesses casos o SIMIO refaz a busca em até 3 versões sem operadores (`site:instagram.com ("Banco BV" OR "bv financeira") feirão` vira `instagram "Banco BV" feirão`, `instagram "bv financeira" feirão`...), filtra os `-site:` no próprio SIMIO e soma os resultados diretos. Dos opacos que sobram, a página inicial do domínio exibido entra na fila e o resto fica registrado na campanha.

O Grounding remove Instagram e Facebook dos resultados, por isso essas plataformas vêm do Serper pelas versões sem operadores, que trazem links diretos de posts, Reels e perfis.

### Domínios parecidos

O conector `typosquat` segue a linha do `dnstwist`: omissão, repetição e troca de posição de letra, tecla vizinha, caractere parecido (`0`/`o`, `rn`/`m`), troca de vogal, hífen, troca de extensão (`.com`, `.app`, `.online`...) e combinação com palavras do setor (`feiraobv.com.br`, `bvoficial.com.br`, `quitarbvfinanceira.com`), que é a forma mais usada na prática. São cerca de 1.900 nomes, e só entram os que resolvem para IP público. Antes de começar, o conector confere se o DNS responde para nomes inexistentes; se responder, ele se desliga para não gerar falso positivo. Sementes, rótulos e palavras ficam em `sementes_typosquat`, `rotulos_combo_typosquat` e `palavras_chave_typosquat` no `bv.toml`.

### O que sai da fila antes da coleta

Para o `--limite` não ser gasto com falsos positivos óbvios:

1. Excluídos de imediato (com registro no log de auditoria):
   - domínios oficiais do grupo (`dominios_oficiais` no `bv.toml`: `bv.com.br`, `bancobv.com.br`, `bvfinanceira.com.br`, `napista.com.br`, `bvopen.com.br`, `meufinanciamentosolar.com.br`, `votorantim.com.br`...);
   - perfis oficiais nas redes sociais (`perfis_sociais_oficiais`: `instagram.com/bancobv`, `facebook.com/bancobv`, `tiktok.com/@bancobv`, `linkedin.com/company/bancobv`, `x.com/bancobv`...);
   - páginas automáticas de busca e tags das redes (`instagram.com/explore/...`, `tiktok.com/discover/...`, `facebook.com/public/...`).
2. Jogados para o fim da fila (a pontuação só ordena; tudo acima de `SIMIO_PONTUACAO_MINIMA_FILA`, padrão `-10`, é analisado enquanto houver vaga):
   - notícias, órgãos públicos (`gov.br`, `bcb.gov.br`), Reclame Aqui, Proteste, Jusbrasil, tribunais, diretórios de CNPJ, sites de vagas e repositórios de logotipos (`dominios_despriorizados`);
   - anúncios individuais de veículos em classificados (OLX, Mercado Livre, Webmotors, Mobiauto), a não ser que venham de uma hipótese de fraude (H10), como um anúncio de "leilão BV";
   - artigos, blogs, estudos de caso e centrais de ajuda de software;
   - páginas burocráticas (`/politica-de-privacidade`, `/termos-de-uso`, `/lgpd`, `sitemap.xml`);
   - a partir da 3ª URL do mesmo domínio registrável, uma penalidade progressiva para variar os estabelecimentos analisados. A página inicial, plataformas compartilhadas (Instagram, Facebook) e URLs importadas não sofrem essa penalidade.

Na campanha de ameaças, construtores de site gratuitos (weebly, site123) não são excluídos, como na ampla, e ganham prioridade.

## Como os falsos positivos são evitados na análise

Depois da coleta, `calcular_sinais()`, a heurística, o prompt do `AnalistaMarca` e o `Revisor` aplicam critérios de contexto para não transformar menção legítima em alerta.

### Lojas e integradores que trabalham com vários bancos

Lojas de seminovos, concessionárias e integradores de energia solar costumam ter uma página `/financiamento` com os logotipos dos bancos com que operam. Quando a página está em domínio próprio do terceiro, não diz ser canal oficial e mostra o Banco BV junto com concorrentes (Santander, Itaú, Banco Pan, Bradesco, Safra, C6, Solfácil, BTG...), o sinal `contexto_multibanco` é ligado. Ele conta como indicador de relação comercial intermediada (`p_contexto_multibanco = 2.0`) e leva a página ao cenário A · Menção comercial legítima.

Títulos comparativos ("Financiamento de Energia Solar | Empresa X (BV e BTG)", "Financiamento pelos bancos Santander e BV") também não contam como uso da marca como identidade do site.

### Declaração de correspondente

Uma frase como "X Ltda é correspondente bancário do Banco BV" precisa citar o banco para cumprir o art. 14, III. Por isso as frases de declaração são removidas (`sem_declaracoes()`) antes de medir se a marca está sendo usada como título ou cabeçalho do site.

Declaração que nomeia a instituição e aparece no topo ou na área visível inicial tem peso alto de transparência (`p_declaracao_destacada = 2.5`). Declaração genérica ("somos empresa independente", sem dizer de qual banco) ou contradita na mesma frase ("não somos correspondente, somos o Banco BV oficial") não recebe esse crédito.

### Homônimos da sigla "BV"

A sigla sozinha é ambígua. O SIMIO trata isso assim:

1. No corpo da página, só termos fortes (`termos_fortes`: "Banco BV", "BV Financeira", "Banco Votorantim", "BV Financiamentos"...) contam em `mencoes_marca`.
2. Sufixos societários estrangeiros ("Example Holding BV", "B.V.") e pedaços de URL e e-mail são ignorados.
3. No título, cabeçalho ou domínio, "BV" só conta se houver contexto bancário na página e não for um homônimo mapeado (Boa Vista, Roraima, Bela Vista, agências de marketing ou de modelos, bioinformática). Isso é o `contexto_homonimo`.

O filtro de homônimos não é ativado quando a página tem marcadores bancários explícitos ("Banco", "Financeira", "Correspondente", "SAC BV", "0800", oferta de 2ª via ou quitação). Assim uma página maliciosa não escapa só por escrever "Boa Vista" no rodapé.

### Notícias, reclamações e processos

Artigos de imprensa, blogs, tutoriais de sistemas, reclamações e processos que só citam o banco em terceira pessoa, sem domínio com a marca, sem cabeçalho da marca e sem captação por WhatsApp, ficam no cenário A (`contexto_editorial`). Frases reportadas ("segundo o site oficial do Banco BV", "alerta sobre golpe") vão para `alegacoes_noticiadas` e não contam como autoapresentação.

## Sinais de alto risco

1. Isca de serviço pós-venda: página de terceiro que oferece 2ª via de boleto, quitação, desconto na quitação, antecipação de parcelas ou renegociação associada à marca e leva o cliente para WhatsApp ou formulário, sem declaração destacada de correspondente, recebe peso alto de confusão (`p_isca_servico_sensivel = 2.5`) e cai nos cenários C ou D, com prioridade Alta.
2. CNPJ do banco no lugar do próprio: site externo que mostra o CNPJ do Banco BV (`59.588.111/0001-03` ou `01.858.774/0001-10`) sem nenhum CNPJ próprio e sem declaração de correspondente recebe um indicador de força Alta em Identidade comercial declarada (`p_personificacao_cnpj_instituicao = 2.5`).
3. Alegação de canal oficial e domínio com a marca: "canal oficial", "site oficial", "central de atendimento BV", "SAC BV", "ambiente seguro Banco BV" ou domínio com o padrão da marca (`padroes_dominio` no `bv.toml`) levam o caso para revisão prioritária. Domínios do typosquatting chegam com a técnica registrada (por exemplo "marca combinada com palavra do setor").
4. Golpes que usam a marca como isca (H10): leilão de "retomados", venda de ágio, crédito "pré-aprovado" mediante taxa, aplicativo fora das lojas oficiais e vagas de "digitador" ou "correspondente" passam pelo mesmo fluxo de evidências.

### Logo antiga

A logo antiga do BV (B vermelho sobre V azul) não pode ser usada por terceiros, nem por correspondentes contratados. A atual tem o mesmo desenho, toda em azul. A detecção tem duas camadas independentes:

| Camada | Como funciona |
|---|---|
| Comparação de imagem (`agentes/visual.py`, sem IA) | Compara regiões das capturas com a referência separando a tinta vermelha da azul, de modo que a logo atual nunca casa. Cada achado vira uma evidência `E-VIS-LEGACY-*` com posição e similaridade |
| Gemini multimodal | Preenche `identidade_visual_antiga` quando vê a logo nas capturas |

Com qualquer uma das duas, o caso fica no mínimo em Requer revisão com prioridade Alta, e o revisor não pode descartá-lo sozinho. O relatório diz a origem ("modelo", "comparação de imagem" ou as duas), o Excel e o HTML marcam "LOGO ANTIGA" e a aba Logo antiga traz o recorte de cada achado.

- O trecho sintético "semelhante ao logotipo antigo" dos resultados da busca reversa (itens `E-DISC-*`) nunca conta como indício. Só a comparação de imagem e o modelo, olhando a própria página, confirmam a logo.
- Sites que só catalogam ou noticiam a logo (repositórios de logos, bancos de imagens, enciclopédias, imprensa), listados em `dominios_isentos_logo_antiga`, registram o achado como "site de referência" sem subir a classificação. Redes sociais e marketplaces não entram nessa lista.
- A comparação de imagem não vê logo cortada na borda da captura, coberta por aviso de cookies ou montada sobre foto; nesses casos depende do Gemini. Logo em escala de cinza (carrossel de parceiros com filtro) não permite distinguir a antiga da atual, e logo aplicada direto sobre fundo vermelho pode escapar da comparação.
- Nas campanhas de 25/11/2026, a logo antiga apareceu sobretudo em vitrines de "bancos parceiros" de revendas de veículos e de energia solar, em páginas de consignado de correspondentes, em fotos de perfil de empresas com iniciais "BV" e em blogs de crédito. O arquivo `logo-bv-financeira-512.png` circula entre vários sites.

Na campanha focada (`--foco logo-antiga`), as fontes padrão são `serper` e `lens`, com H13, H12 e H9. Páginas sem logo confirmada e sem imagem com o nome da marca no arquivo ou no `alt` fecham como Sem indício sem chamar os modelos, e a limitação fica registrada.

### Campanha de ameaças (`--foco ameacas`)

Não usa o contexto de COBANs (nem H9 nem as hipóteses de produto e regionais). Busca H14, H10, H7 e Telegram, mais domínios parecidos (`typosquat`) e certificados (`crtsh`). Os prompts de Grounding pedem o que a página oferece ou pede (login, senha, CPF, cartão, token, boleto, Pix, app, WhatsApp, Telegram) e por que parece falsa.

O analista e o revisor recebem o adendo `prompts/modo_ameacas.md`, que tem precedência sobre o escopo de COBANs. A pergunta passa a ser "isto se passa pelo banco ou põe clientes em risco?": parceiro identificado com a própria marca é tema de conformidade e fica para a varredura ampla; notícia, diretório e blog só contam se desviarem o cliente para canal não oficial. A versão gravada fica `analista-marca/v9+modo-ameacas/v3`.

#### Coleta de credenciais

O coletor registra os campos de formulário (tipo, nome, `placeholder`, rótulo, `autocomplete`) e o destino de cada formulário, sem preencher nada. Senha, cartão, CVV, token ou código e agência/conta são credenciais; CPF é registrado, mas sozinho não dispara a regra, porque é comum em simuladores legítimos. Página fora dos domínios oficiais que usa a marca e pede uma credencial recebe evidências `E-FORM-*` e fica no mínimo em Requer revisão com prioridade Alta, sem que o revisor possa descartá-la. Formulário que envia os dados para outro domínio também é registrado (`E-FORM-ACTION`).

#### Triagem de ameaças (`agentes/triagem_ameacas.py`)

Sem chamar o modelo, a triagem relê o que já foi analisado e dá a cada página um nível a partir de fatos que dá para conferir. Funciona em campanhas antigas também, basta gerar o relatório de novo (`simio relatorio -c <id>`).

| Fato | Pontos |
|---|---|
| Pede senha, cartão, CVV, token ou agência/conta numa página com a marca | +5 |
| Analisador registrou personificação (cenário D) ou falso canal oficial (cenário C), se a classificação final não for Sem indício | +4 ou +3 |
| APK com a marca fora das lojas oficiais | +3 |
| Domínio parecido (typosquatting) ou com a marca no nome; +1 se o conteúdo cita a instituição | +2 (+1) |
| Página com a marca em plataforma gratuita; domínio criado há 180 dias ou menos; boleto, quitação ou 2ª via citando a marca | +2 cada |
| Indício forte na análise / Requer revisão (só se já houver pontos); contato direto por WhatsApp ou Telegram | +2 / +1; +1 |
| Empresa identificada falando com a própria marca (cenários A ou B, sem pedir credencial) / menção comercial legítima | -4 / -2 |
| Sem relação com a instituição (Não relevante) | -3 e teto de 3 |

Níveis: Crítico a partir de 8, Alto a partir de 5, Médio a partir de 3, Baixo a partir de 2, abaixo disso Sem ameaça. Domínio oficial, loja oficial de apps, homônimo conhecido (`dominios_homonimos` no perfil) e domínio registrado pelo próprio grupo (titular ou CNPJ do RDAP em `nomes_instituicao` ou `cnpjs_instituicao`, ou seja, registro defensivo) ficam com teto 0.

Na campanha de ameaças, a aba Ameaças e o `ameacas.html` listam tudo acima de Sem ameaça. Na varredura ampla, só Crítico, Alto e páginas que pedem credencial, numa aba depois das de conformidade. Os dois trazem nível, categoria, fatos, URL, registro do domínio, hospedagem, contatos de abuso, dados pedidos e destino do formulário, prontos para bloqueio e pedido de remoção. O HTML mostra a captura de cada página e as URLs desarmadas (`hxxps://site[.]com`).

#### Quem está por trás do domínio

Para cada achado (Indício forte, Requer revisão ou página que pede credencial), o SIMIO consulta o RDAP (o WHOIS estruturado) e o DNS, sem chave e sem custo. O resultado vai para a aba Domínios, para a aba Ameaças e para os dossiês.

| Informação | Para que serve |
|---|---|
| Data de criação e idade | Domínio criado há poucos dias é sinal forte de golpe |
| Registrador e titular | No `.br`, o Registro.br publica nome e CNPJ de empresas. Pessoa física aparece só como "pessoa física" |
| NS, MX e se ainda resolve | Infraestrutura usada e se o site continua no ar |
| Rede do IP | Quem hospeda a página (Vercel, Cloudflare, Hostinger...) |
| Contatos de abuso | E-mails de abuso do registrador e do provedor, para pedir a derrubada |

Em plataformas (`*.weebly.com`, `*.lovable.app`, `*.site123.me`...), registro e DNS são da plataforma: o responsável pela página só é identificável por ela, e o relatório indica o contato de abuso da própria plataforma.

## Decisão e revisão (`nucleo/decisao.py` e `agentes/revisor.py`)

A resposta do modelo é uma proposta, conferida por regras fixas:

- Toda evidência citada pelo modelo precisa existir no catálogo da página (`E-TITLE`, `E-TXT-001`...). Referências inexistentes são descartadas e indicadores que ficam sem referência válida são removidos.
- Indício forte exige pelo menos dois indicadores de confusão com referência válida (`min_indicadores_forte = 2`), coleta utilizável e evidência que não seja Insuficiente. Se faltar algo, o caso vira Requer revisão.
- Cenário e classificação precisam combinar:
  - E · Evidência insuficiente força Inconclusiva;
  - C · Possível canal oficial e D · Personificação forte nunca ficam como Sem indício;
  - A · Menção comercial legítima ou Não relevante com classificação de alerta viram Requer revisão;
  - B · Comunicação ambígua com Sem indício sobe para Requer revisão se houver indicador de confusão válido, ou passa para o cenário A se não houver nenhum.
- Verificação cruzada (`verificacao_cruzada()`): se o modelo responde Sem indício e a heurística vê cenário B, C ou D, o caso vai para Requer revisão com revisão humana obrigatória e os indicadores da heurística são mantidos.
- O revisor é o filtro cético dos casos suspeitos ou duvidosos. Pode confirmar, pedir nova coleta, declarar Inconclusiva ou rebaixar (por exemplo, quando há declaração de correspondente ou contexto multibanco), mas nunca promove sozinho um caso a Indício forte. Os pisos da logo antiga e do pedido de credencial continuam valendo depois da revisão.

Cada decisão final sugere um encaminhamento: gestão de correspondentes (Res. 4.935), jurídico e proteção de marca, segurança e prevenção a fraudes, ou verificação manual com nova coleta.

### Cenários e classificação padrão

| Cenário | Classificação | Prioridade |
|---|---|---|
| Não relevante | Sem indício | Baixa |
| A · Menção comercial legítima | Sem indício | Baixa |
| B · Comunicação ambígua | Requer revisão | Média |
| C · Possível canal oficial | Requer revisão | Alta |
| D · Personificação forte | Indício forte | Alta |
| E · Evidência insuficiente | Inconclusiva | Média |

### Pesos da heurística

A heurística offline soma pesos de confusão e de legitimidade e escolhe o cenário pelos limiares. A pontuação só ordena e encaminha; nunca confirma infração. Os valores padrão estão em `ConfigPontuacao` e podem ser trocados num `resultados/pontuacao.toml` com os mesmos nomes:

```toml
p_marca_no_dominio = 2.0
p_marca_no_titulo = 1.5
p_marca_no_cabecalho = 1.0
p_logo_marca_acima_dobra = 1.0
p_oficialidade_ligada_marca = 3.0
p_oficialidade_generica = 0.5
p_sem_declaracao = 1.5
p_whatsapp_com_marca_destacada = 0.5
p_isca_servico_sensivel = 2.5
p_personificacao_cnpj_instituicao = 2.5
p_declaracao_destacada = 2.5
p_declaracao_presente = 1.0
p_contexto_multibanco = 2.0
p_ouvidoria = 0.5
p_responsavel_identificado = 0.5
limiar_b = 1.5
limiar_c = 3.5
limiar_d = 5.5
min_indicadores_forte = 2
```

Nomes que não existem em `ConfigPontuacao` são ignorados sem aviso.

## Conformidade (`agentes/conformidade.py`)

O enquadramento normativo não usa LLM, para nunca inventar artigo. Ele só cita requisitos de `conhecimento/cmn_4935.toml`, transcrito do texto consolidado publicado pelo Banco Central, e de um arquivo contratual opcional:

- art. 14, II: vedação ao uso, pelo correspondente, de logomarca ou atributos parecidos com os da instituição contratante;
- art. 14, III: divulgação ao público da condição de prestador de serviços, com identificação da contratante e dos canais de atendimento e ouvidoria.

Cada achado sai como Hipótese de não conformidade, Hipótese condicionada ao vínculo (quando não se sabe se a empresa é correspondente), Não aplicável (salvo se for correspondente), Sem indício observado ou Não avaliável. A área jurídica deve revalidar a vigência do texto antes de cada ciclo de auditoria.

Requisitos do contrato ou de política interna entram por um TOML apontado em `SIMIO_CAMINHO_REQUISITOS_CONTRATUAIS`, no mesmo formato do arquivo normativo:

```toml
[[requisito]]
id = "CONTRATO-CL7"
citacao = "Contrato de correspondente, cláusula 7.2"
fonte = "CONTRATUAL"            # ou POLITICA_INTERNA
dimensoes = ["IDENTIDADE_VISUAL", "USO_LOGO"]
texto = """Texto literal da cláusula."""
hipotese = "Uso da marca fora do manual de identidade aprovado"
```

As dimensões possíveis são `IDENTIDADE_VISUAL`, `IDENTIDADE_COMERCIAL_DECLARADA`, `TRANSPARENCIA_CORRESPONDENTE`, `LINGUAGEM_INSTITUCIONAL`, `USO_LOGO`, `JORNADA_ATENDIMENTO`, `CANAIS_CONTATO` e `CONFUSAO_CONSUMIDOR`.
