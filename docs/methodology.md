# Metodologia

## Pergunta investigativa

O consumidor pode ser levado a crer que trata **diretamente com a instituição**, quando trata com
um estabelecimento independente? A resposta combina:

1. **Identidade declarada** — título, nome do site, razão social, CNPJ, textos de identificação.
2. **Identidade visual** — logotipo, cores e atributos semelhantes aos canais da instituição.
3. **Identidade efetiva** — quem controla a página (Identity Agent).
4. **Transparência da relação** — declaração de correspondente, destaque, legibilidade e
   localização (topo/área visível × rodapé), canais da contratante e ouvidoria.
5. **Percepção potencial** — linguagem de oficialidade, jornada de atendimento, contatos.

Regras de interpretação (também nos prompts):

- Mencionar a marca **não** é, isoladamente, irregularidade.
- "Parceiro" **não** resolve, isoladamente, a análise (registrado como incerteza).
- Ausência de palavra-chave **não** é, sozinha, evidência suficiente; ausência no screenshot não
  prova ausência no site. O sistema diferencia falha de coleta, ausência observada e conteúdo
  não inspecionado.
- Confiança do modelo **não** é probabilidade estatística de irregularidade.

## Eixos de saída (independentes)

| Eixo | Valores | Fonte |
|---|---|---|
| Classificação | NO_INDICATION, REVIEW_REQUIRED, STRONG_INDICATION, INCONCLUSIVE | Analista + guardrails + revisor |
| Prioridade | LOW, MEDIUM, HIGH | idem |
| Cenário | A–E, NOT_RELEVANT | Analista |
| Confiança da extração | HIGH…NONE | Analista (leitura das evidências) |
| Confiança da atribuição | HIGH…NONE | Identity Agent |
| Suficiência da evidência | SUFFICIENT, PARTIAL, INSUFFICIENT | Analista + coleta |
| Categoria de identidade | CONFIRMED_COBAN, POSSIBLE_COBAN, OTHER_THIRD_PARTY, UNKNOWN_IDENTITY | Identity Agent |

Exemplo: forte indício de confusão visual com responsável desconhecido →
`STRONG_INDICATION` + `UNKNOWN_IDENTITY`; o responsável **não** é tratado como COBAN.

## Guardrails determinísticos (`core/decision.py`)

- Coleta sem conteúdo utilizável → cenário E / `INCONCLUSIVE`.
- `STRONG_INDICATION` exige ≥ 2 indicadores com referências válidas e evidência não insuficiente
  (`strong_min_indicators`).
- Cenário A/NOT_RELEVANT com classificação de alerta, ou C/D com `NO_INDICATION`, é inconsistente
  → `REVIEW_REQUIRED`.
- Sinal de prompt injection → revisão humana obrigatória; nunca `NO_INDICATION`.
- O revisor pode confirmar ou rebaixar; não pode elevar a `STRONG_INDICATION` sozinho.

## Identidade

- Fatos observados (com ref) e inferências de associação ficam separados.
- CNPJ: validação de dígitos (formato numérico e alfanumérico 2026). CNPJs da própria instituição
  (perfil `institution_cnpjs`) ou citados em contexto do nome da instituição são tratados como
  referência à contratante, não como titular da página.
- `CONFIRMED_COBAN` exige correspondência **por CNPJ** em cadastro autoritativo
  (`SIMIO_COBAN_REGISTRY_PATH`, CSV com coluna `cnpj`). Uma fonte possível é a relação de
  contratados que a contratante deve manter em dados abertos (Res. 4.935, art. 21), ou a base
  interna. Correspondência só por nome → `POSSIBLE_COBAN`.

## Conformidade

`knowledge/cmn_4935.toml` contém o **texto literal** dos dispositivos usados (art. 3º; art. 14,
II e III; controles dos arts. 18, 19 e 21), transcrito da versão consolidada publicada pelo BCB
(consulta em 2026-11-18; art. 8º revogado pela Resolução Conjunta BCB/CMN nº 17/2025, não usado).
Nota: os incisos II e III do art. 14 são **cláusulas obrigatórias do contrato de correspondente** —
a obrigação do correspondente decorre do contrato; o relatório explicita isso.

Para cada requisito o Compliance Analyst separa: requisito (normativo/contratual), evidência
observada (refs), hipótese de não conformidade, informação faltante e necessidade de revisão
humana. Status possíveis: `HYPOTHESIS_OF_NONCONFORMITY`, `NO_INDICATION_OBSERVED`,
`CANNOT_ASSESS`, `NOT_APPLICABLE_UNLESS_COBAN` (responsável não identificado como correspondente:
o caso pode ser uso indevido de marca/fraude, fora do escopo da norma).

Requisitos contratuais/internos podem ser adicionados pelo operador (não versionar o arquivo real):

```toml
[[requirement]]
id = "CONTRATO-CL-7.2"
source = "CONTRACTUAL"            # ou INTERNAL_POLICY
citation = "Contrato padrão de correspondente, cláusula 7.2"
text = "…texto da cláusula…"
dimensions = ["LOGO_USAGE", "VISUAL_IDENTITY"]
hypothesis = "Uso de identidade visual sem autorização"
```

**Revalidar a vigência da norma antes de cada ciclo de auditoria.**

## Pontuação parametrizável

A heurística offline e os limiares do cenário usam `ScoringConfig`; para ajustar, crie
`<output_dir>/scoring.toml` com os campos de `core/decision.py` (`w_*`, `threshold_*`,
`strong_min_indicators`). Pontuações apenas ordenam e roteiam casos — não confirmam infrações.

## Descoberta

Hipóteses (H1–H6): marca como identidade principal de terceiros; páginas de produto com aparência
institucional; atendimento/simulação apresentados como do banco; expressões de oficialidade;
estabelecimentos por tipo × região; plataformas/canais. A priorização pré-coleta usa padrão da
marca no domínio, menções e linguagem de oficialidade no título/trecho, convergência de fontes e
despriorização de notícias/reguladores; domínios oficiais são excluídos e registrados no log.
