# Projeto: Design System Banco BV

## Objetivo
Estabelecer um Design System (DS) coerente e escalável, baseado nos assets locais da pasta `Assets` e na identidade visual extraída do portal `bv.com.br`. Este material servirá como fonte de verdade central (Source of Truth) para pautar o desenvolvimento de interfaces HTML, documentos do projeto e campanhas de comunicação.

## Estrutura do Design System
- **`DESIGN.md`**: Definição canônica e detalhada dos tokens (cores, tipografia, espaçamento, sombras) e componentes, com regras de *Do's and Don'ts*.
- **`design-system-preview.html`**: Um arquivo interativo renderizando visualmente os tokens e componentes base para homologação.
- **`extraction-notes.md`**: Notas metodológicas sobre o processo de extração, fontes utilizadas e lacunas.
- **`tokens.css`**: Variáveis CSS prontas para integração no front-end.
- **`assets/`**: Diretório referenciando os recursos gráficos (imagens, logos, backgrounds) fornecidos originalmente.

## Aplicação
Qualquer nova interface desenvolvida para este projeto deve obrigatoriamente importar o `tokens.css` e seguir as diretrizes documentadas no `DESIGN.md`. Agentes de IA e desenvolvedores devem tratar essa pasta como a base fundacional de estilo do banco BV.
