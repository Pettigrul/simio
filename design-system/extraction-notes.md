# Design System Extraction Notes

## Metodologia
A extração do Design System do Banco BV baseou-se em dois processos principais:
1. **Análise de Assets Locais (`/Assets/`)**: Foram revisados os backgrounds oficiais (ex: `image1.png` demonstrando o uso expressivo de gradientes em azul profundo e formas super curvas) e tipografia / branding (como `image2.png`).
2. **Inspeção do site live (`bv.com.br`)**: Utilização de ferramentas headless para navegar na homepage oficial e injetar scripts de extração das variáveis computadas na DOM (`window.getComputedStyle`).

## Fontes Extraídas

### Cores Base:
- **Azuis Principais**: O azul chumbo institucional (`#001A6B` e variações `#003087`), o azul principal de ação (`#0B5FFF`), e o azul bebê/claro (`#EFF4FB`).
- **Textos e Escalas Neutras**: A inspeção capturou `#272833` e `#292929` para textos primários, `#6B6C7E` para textos de apoio, e uma escala para bordas / dividers (`#E7E7ED`, `#F5F5F5`).

### Tipografia:
- Identificamos o uso de famílias tipográficas proprietárias `"BV Leve Titulos"` e `"BV Leve Textos"`.
- Devido à falta do arquivo `.woff2` diretamente acessível nos assets para uso imediato num contexto web genérico, declarou-se `"Inter", sans-serif` como fallback mandatório, uma vez que preserva a legibilidade, kerning e anatomia clean similar ao "BV Leve". 

## Componentes Analisados:
- **Cards (Soluções / Produtos)**: Grande destaque na home, usando `border-radius` suave, interações flat (subindo shadow no hover), com hierarquia clara (Ícone -> H3 -> Paragraph -> Ghost Button).
- **CTAs (Botões)**: Totalmente arredondados (`radius: 999px`) e blocados. Cores chapadas que passam solidez.
- **Navegação / Headers**: Fundo branco ou transparente absoluto na hero area.

## Nível de Confiança (Confidence Score)
- **Cores**: High (Alto) - Valores extraídos perfeitamente dos computes styles em produção.
- **Tipografia**: Medium (Médio) - Propriedades estão corretas (tamanhos, weights), mas a fonte real depende do licenciamento ou carregamento webfont do BV.
- **Espaçamento e Shadows**: High (Alto) - Identificado o grid claro e o uso comedido de z-index (sombras).

## Limitações / Lacunas (Gaps)
- Interações complexas, como formulários de multi-steps, não puderam ser varridas de imediato sem simulação de logins (`/login`).
- Estados de erro (Error States) não estavam aparentes apenas pela homepage deslogada. Para desenvolvimento futuro, o Designer/Dev deverá projetar usando, por exemplo, um Red System (ex: `#E3262E`) caso aplicável à UI.

## Conclusão
A pasta foi gerada com a maior fidelidade visual possível e entrega o valor necessário para qualquer LLM/Agente de IA e/ou desenvolvedores front-end montarem estruturas nativas para o ecossistema digital do banco BV.