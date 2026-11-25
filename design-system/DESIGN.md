---
version: alpha
name: Banco BV Design System
description: Sistema de design oficial extraído de bv.com.br e acervo local de assets.
colors:
  - primary
  - secondary
  - neutral
  - background
  - surface
typography:
  - display
  - body
rounded: true
spacing: true
components:
  - button
  - card
  - navigation
  - footer
  - input
  - accordion
---

# Banco BV Design System

## Overview
O Design System do banco BV transmite credibilidade, inovação e acessibilidade. O design é marcado por tons vibrantes de azul contrastando com fundos brancos e cinzas claros, formas com cantos arredondados (soft radius) e tipografia limpa, sem serifa, criando uma interface moderna e convidativa ("Crédito para suas conquistas").

## Colors

A paleta de cores foca em tons de azul institucionais, suportados por uma escala neutra de alto contraste para leitura e fundos suaves para organizar o conteúdo.

**Primary (Brand)**
- `bv-blue-primary`: `#0B5FFF` (Azul vibrante, usado em CTAs principais, links de destaque e ícones ativos).
- `bv-blue-dark`: `#001A6B` (Azul escuro profundo, usado em tipografia de alto contraste, footers ou fundos institucionais fortes).
- `bv-blue-medium`: `#003087` (Azul médio institucional, usado em backgrounds e banners).

**Neutral & Text**
- `bv-text-primary`: `#272833` (Cinza chumbo quase preto, cor padrão de parágrafos e títulos).
- `bv-text-secondary`: `#6B6C7E` (Cinza médio, usado para descrições, textos de apoio e placeholders).
- `bv-text-white`: `#FFFFFF` (Para texto sobre fundos escuros/primários).

**Background & Surface**
- `bv-bg-main`: `#FFFFFF` (Fundo padrão de páginas).
- `bv-bg-light`: `#F5F5F5` (Usado para diferenciar seções e cards secundários).
- `bv-bg-blue-light`: `#EFF4FB` (Azul bem claro, frequentemente usado em seções de destaque sutis ou hover states de cards).
- `bv-surface-border`: `#E7E7ED` (Bordas de cards, divisórias e inputs).

## Typography

A tipografia do banco BV é proprietária e sem serifa, conferindo leveza e tom direto. Na ausência da fonte proprietária, utiliza-se `Inter` ou `Lato` como fallback.

- **Font Families**: 
  - Títulos: `"BV Leve Titulos", "Inter", sans-serif`
  - Textos: `"BV Leve Textos", "Inter", sans-serif`
- **H1**: 32px a 48px, peso Médio (500) ou Negrito (600), cor `#272833` ou `#001A6B`.
- **H2**: 24px a 32px, peso Médio (500), cor `#272833`.
- **H3**: 18px a 20px, peso Médio (500), cor `#20262C`.
- **Body 1 (Regular)**: 16px, peso Normal (400), entrelinha 150%, cor `#272833`.
- **Body 2 (Small)**: 14px, peso Normal (400), entrelinha 150%, cor `#6B6C7E`.
- **Links**: Seguem o peso do corpo do texto, com cor `#0B5FFF` e underline no hover.

## Layout

- **Container Max-Width**: O conteúdo é centralizado em containers com largura máxima em torno de `1140px` a `1200px`.
- **Grid**: Sistema clássico de 12 colunas no desktop, reduzindo para 4 a 8 no mobile.
- **Section Spacing**: `64px` ou `80px` de espaçamento vertical entre seções maiores (desktop), cainendo para `40px` no mobile.

## Elevation & Depth

O uso de sombras é sutil, focado em trazer elevação apenas onde há interatividade explícita.
- **Card Shadow (Rest)**: Sombra muito suave, ex: `0 4px 12px rgba(0, 0, 0, 0.05)`, ou card flat com borda `#E7E7ED`.
- **Card Shadow (Hover)**: `0 8px 24px rgba(0, 0, 0, 0.1)` - o card "sobe" levemente.
- **Dropdown/Modal Shadow**: `0 12px 32px rgba(0, 0, 0, 0.15)`.

## Shapes

- **Border Radius**: 
  - Cards, Imagens e Inputs: `8px` ou `12px` (Soft radius).
  - Botões Principais (Pills): Totalmente arredondados `999px` ou radius de `24px` dependendo do contexto. As implementações mais modernas do banco adotam cantos arredondados contínuos para CTAs.
- Elementos gráficos nos banners (como visto nos backgrounds extraídos de `Assets/image1.png` e `image10.png`) usam formas orgânicas, cantos super arredondados, ondas e sobreposições de tons de azul.

## Components

### Buttons
- **Primary**: Background `#0B5FFF`, Texto `#FFFFFF`, Radius `999px`, padding `12px 24px`. Fonte em bold/medium. 
  - *Hover*: Escurece o tom de azul (ex: `#0047D4`).
- **Secondary (Outline)**: Background `transparent`, Borda `2px solid #0B5FFF`, Texto `#0B5FFF`.
- **Ghost (Tertiary)**: Background `transparent`, sem borda, Texto `#0B5FFF`. Usado para links como "Saiba mais".
- **Institucional**: Em backgrounds escuros, o botão primário pode assumir cor branca com texto `#001A6B`.

### Cards
- Utilizados exaustivamente na página inicial (Soluções, Financiamentos, Empréstimos).
- Fundo branco (`#FFFFFF`) sobre o container mais escuro ou fundo claro (`#F5F5F5`).
- Possuem um ícone ilustrativo ao topo, título (`H3`), breve descrição e um link/botão "Saiba mais" ao final.
- Padding interno geralmente de `24px`.

### Navigation (Header)
- Background branco constante ou transparente no topo absoluto.
- Logo à esquerda.
- Links de navegação centralizados ou dispostos em linha, cor `#272833`.
- Barra de busca oculta ou expansível, e botão "Área do cliente" com destaque à direita.

### Inputs & Forms
- Borda de `1px solid #E7E7ED`.
- Background branco.
- Radius de `8px`.
- Ao focar, a borda recebe a cor `#0B5FFF` (anel de foco).

## Do's and Don'ts

**Do (O que fazer)**
- **Do** usar branco abundante (white space) para deixar o layout "respirar".
- **Do** manter alta legibilidade; se o fundo for azul escuro (`#001A6B`), usar fontes exclusivamente brancas.
- **Do** usar os shapes orgânicos/curvos em imagens de cabeçalho ou backgrounds decorativos para suavizar a solidez financeira.
- **Do** usar ícones lineares ou ilustrações flat com tons de azul para guiar o usuário visualmente.

**Don't (O que NÃO fazer)**
- **Don't** utilizar botões quadrados (`radius: 0`). A interface do BV é arredondada e humana.
- **Don't** usar o azul principal (`#0B5FFF`) para textos longos de corpo (body text); reserve-o para interatividade.
- **Don't** poluir com sombras duras (hard shadows) ou bordas pesadas (pretas).

## Iconography & Imagery
- O banco utiliza fotografia fotorrealista mostrando pessoas autênticas, felizes e do cotidiano brasileiro (ex: Mulher no carro, homem no sofá, reuniões em escritório).
- Ícones de interface são vetoriais (SVGs), com linhas arredondadas, geralmente nas cores azuis do banco.

## Agent Implementation Notes
Ao gerar código HTML/CSS ou React/Tailwind para este Design System:
- Assuma `font-family: 'Inter', sans-serif` como fallback padrão de alta qualidade para as fontes "BV Leve".
- Ao criar seções "Hero" (topo da página), você pode usar o asset `Assets/image1.png` como background-image de cobertura (`background-size: cover; background-position: center`) com textos brancos.
- Utilize estruturação semântica correta (`<nav>`, `<main>`, `<section>`, `<article>`) para garantir a mesma acessibilidade da página canônica.