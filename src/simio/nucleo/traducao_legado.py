"""Translation of data written by the English-named SIMIO builds (schema v1/v2).

Used by migration v3 (``simio.nucleo.armazenamento``) to rewrite every stored JSON payload and
text code, and by readers of files written on disk by older runs (``domain-intel.json``).
The walk is driven by the Pydantic models: keys are renamed per model class, enum values per
enum-typed field and the few free-text codes per field. Payloads coming from third-party APIs
(``metadados`` values, page text) are never walked.

Tables generated from the name map (analise/mapa_nomes.json) of the Portuguese rename.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import types
from collections.abc import Callable
from enum import Enum
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

# ---------------------------------------------------------------------------- generated tables
CAMPOS_POR_MODELO: dict[str, Any] = {
    "AchadoConformidade": {
        "requirement_id": "id_requisito",
        "requirement_source": "fonte_requisito",
        "citation": "citacao",
        "requirement_text": "texto_requisito",
        "observed_evidence": "evidencias_observadas",
        "hypothesis": "hipotese",
        "missing_information": "informacoes_faltantes",
        "requires_human_review": "exige_revisao_humana",
    },
    "Artefato": {"kind": "tipo", "path": "caminho", "size_bytes": "tamanho_bytes"},
    "AtribuicaoIdentidade": {
        "candidate_id": "id_candidata",
        "classification": "classificacao",
        "entity_key": "chave_entidade",
        "display_name": "nome_exibicao",
        "owner_cnpjs": "cnpjs_responsavel",
        "referenced_institution_cnpjs": "cnpjs_instituicao_referenciados",
        "facts": "fatos",
        "inferences": "inferencias",
        "registry_match": "correspondencia_cadastro",
        "attribution_confidence": "confianca_atribuicao",
        "limitations": "limitacoes",
        "rationale": "fundamentacao",
    },
    "AvaliacaoConformidade": {
        "findings": "achados",
        "out_of_scope_notes": "notas_fora_escopo",
        "related_controls": "controles_relacionados",
        "limitations": "limitacoes",
        "disclaimer": "ressalva",
    },
    "AvaliacaoDimensao": {
        "dimension": "dimensao",
        "signal": "sinal",
        "finding": "constatacao",
        "evidence_refs": "refs_evidencia",
    },
    "AvaliacaoMarca": {
        "analyzer": "analisador",
        "visual_matches": "correspondencias_visuais",
        "legacy_visual_identity_source": "fonte_identidade_visual_antiga",
        "legacy_logo_exempt": "logo_antiga_isenta",
        "sensitive_data_requested": "dados_sensiveis_solicitados",
        "prompt_version": "versao_prompt",
        "dropped_references": "referencias_descartadas",
        "guardrail_notes": "notas_salvaguarda",
    },
    "Candidata": {
        "campaign_id": "id_campanha",
        "normalized_url": "url_normalizada",
        "registrable_domain": "dominio_registravel",
        "priority_score": "pontuacao_prioridade",
        "priority_reasons": "motivos_prioridade",
        "attempts": "tentativas",
        "recollects": "recoletas",
        "last_error": "ultimo_erro",
    },
    "ConfigPontuacao": {
        "w_brand_in_domain": "p_marca_no_dominio",
        "w_brand_in_title": "p_marca_no_titulo",
        "w_brand_in_header": "p_marca_no_cabecalho",
        "w_brand_logo_above_fold": "p_logo_marca_acima_dobra",
        "w_brand_linked_official_claim": "p_oficialidade_ligada_marca",
        "w_generic_official_claim": "p_oficialidade_generica",
        "w_no_disclosure": "p_sem_declaracao",
        "w_whatsapp_with_prominent_brand": "p_whatsapp_com_marca_destacada",
        "w_sensitive_service_bait": "p_isca_servico_sensivel",
        "w_institution_cnpj_impersonation": "p_personificacao_cnpj_instituicao",
        "w_disclosure_prominent": "p_declaracao_destacada",
        "w_disclosure_present": "p_declaracao_presente",
        "w_multi_bank_context": "p_contexto_multibanco",
        "w_ouvidoria": "p_ouvidoria",
        "w_owner_identified": "p_responsavel_identificado",
        "threshold_b": "limiar_b",
        "threshold_c": "limiar_c",
        "threshold_d": "limiar_d",
        "strong_min_indicators": "min_indicadores_forte",
    },
    "Configuracoes": {
        "gcp_project": "projeto_gcp",
        "gcp_location": "local_gcp",
        "gcp_credentials_path": "caminho_credenciais_gcp",
        "analysis_model": "modelo_analise",
        "review_model": "modelo_revisao",
        "discovery_model": "modelo_descoberta",
        "llm_max_output_tokens": "llm_max_tokens_saida",
        "llm_temperature": "llm_temperatura",
        "llm_max_retries": "llm_max_retentativas",
        "max_llm_requests": "max_requisicoes_llm",
        "max_llm_tokens": "max_tokens_llm",
        "max_search_queries": "max_consultas_busca",
        "coban_seed_limit": "limite_sementes_coban",
        "coban_seed_batch": "lote_sementes_coban",
        "workers": "paralelismo",
        "max_attempts": "max_tentativas",
        "max_recollects": "max_recoletas",
        "retry_base_delay_s": "atraso_base_retentativa_s",
        "brave_api_key": "chave_api_brave",
        "serper_api_key": "chave_api_serper",
        "urlscan_api_key": "chave_api_urlscan",
        "serper_pages": "paginas_serper",
        "serper_plain_variants": "variantes_simples_serper",
        "min_queue_score": "pontuacao_minima_fila",
        "http_timeout_s": "timeout_http_s",
        "domain_intel": "inteligencia_dominio",
        "rdap_base_url": "url_base_rdap",
        "doh_url": "url_doh",
        "nav_timeout_ms": "timeout_navegacao_ms",
        "page_budget_s": "orcamento_pagina_s",
        "settle_ms": "espera_estabilizar_ms",
        "viewport_width": "largura_viewport",
        "viewport_height": "altura_viewport",
        "max_fullpage_height": "max_altura_pagina_inteira",
        "max_html_bytes": "max_bytes_html",
        "max_text_chars": "max_caracteres_texto",
        "max_requests_per_page": "max_requisicoes_por_pagina",
        "browser_sandbox": "sandbox_navegador",
        "browser_proxy_pin_ip": "proxy_navegador_fixar_ip",
        "browser_proxy": "proxy_navegador",
        "browser_user_agent": "user_agent_navegador",
        "ignore_https_errors": "ignorar_erros_https",
        "allowed_ports": "portas_permitidas",
        "unsafe_allow_hosts": "hosts_liberados_inseguro",
        "output_dir": "dir_saida",
        "coban_registry_path": "caminho_cadastro_coban",
        "coban_registry_authoritative": "cadastro_coban_oficial",
        "coban_registry_reference_date": "data_referencia_cadastro_coban",
        "contract_requirements_path": "caminho_requisitos_contratuais",
        "brand_file": "arquivo_marca",
        "log_level": "nivel_log",
    },
    "Contestacao": {
        "topic": "topico",
        "concern": "preocupacao",
        "evidence_refs": "refs_evidencia",
        "severity": "severidade",
    },
    "CorrespondenciaCadastro": {
        "source": "fonte",
        "matched_on": "casado_por",
        "record": "registro",
        "authoritative": "oficial",
    },
    "CorrespondenciaVisual": {
        "kind": "tipo",
        "screenshot_ref": "ref_captura",
        "score": "pontuacao",
        "image_src": "src_imagem",
        "above_fold": "acima_dobra",
    },
    "DadosPagina": {
        "title": "titulo",
        "canonical": "canonica",
        "lang": "idioma_pagina",
        "headings": "titulos_secao",
        "nav_items": "itens_nav",
        "contact_links": "links_contato",
        "images": "imagens",
        "header_text": "texto_cabecalho",
        "footer_text": "texto_rodape",
        "above_fold_text": "texto_acima_dobra",
        "visible_text": "texto_visivel",
        "forms_present": "formularios_presentes",
        "inputs": "campos_entrada",
        "form_actions": "acoes_formulario",
    },
    "DecisaoFinal": {
        "candidate_id": "id_candidata",
        "classification": "classificacao",
        "priority": "prioridade",
        "case": "caso",
        "identity_class": "classe_identidade",
        "extraction_confidence": "confianca_extracao",
        "attribution_confidence": "confianca_atribuicao",
        "evidence_sufficiency": "suficiencia_evidencia",
        "requires_human_review": "exige_revisao_humana",
        "human_review_status": "status_revisao_humana",
        "justification": "justificativa",
        "key_evidence": "evidencias_chave",
        "limitations": "limitacoes",
        "guardrail_notes": "notas_salvaguarda",
        "routing": "encaminhamento",
        "compliance_summary": "resumo_conformidade",
        "legacy_visual_identity": "identidade_visual_antiga",
        "legacy_visual_identity_source": "fonte_identidade_visual_antiga",
        "sensitive_data_requested": "dados_sensiveis_solicitados",
        "decided_at": "decidida_em",
    },
    "DesfechoDescoberta": {
        "hits": "resultados",
        "queries": "consultas",
        "diagnostics": "diagnosticos",
        "sources_used": "fontes_usadas",
    },
    "Evidencia": {
        "candidate_id": "id_candidata",
        "attempt": "tentativa",
        "original_url": "url_original",
        "final_url": "url_final",
        "redirect_chain": "cadeia_redirecionamentos",
        "http_status": "status_http",
        "server_ip": "ip_servidor",
        "collected_at": "coletada_em",
        "page": "pagina",
        "artifacts": "artefatos",
        "blocked_requests": "requisicoes_bloqueadas",
        "errors": "erros",
        "not_inspected": "nao_inspecionado",
        "retryable": "repetivel",
        "manifest_path": "caminho_manifesto",
        "manifest_sha256": "sha256_manifesto",
    },
    "FatoIdentidade": {
        "kind": "tipo",
        "value": "valor",
        "evidence_ref": "ref_evidencia",
        "context": "contexto",
        "valid": "valido",
    },
    "HipoteseBusca": {
        "description": "descricao",
        "grounding_prompt": "prompt_grounding",
        "keyword_queries": "consultas_palavras_chave",
    },
    "Indicador": {
        "dimension": "dimensao",
        "description": "descricao",
        "evidence_refs": "refs_evidencia",
        "strength": "forca",
    },
    "InfoCampoEntrada": {"type": "tipo_campo", "name": "nome", "label": "rotulo"},
    "InfoImagem": {
        "location": "local",
        "above_fold": "acima_dobra",
        "width": "largura",
        "height": "altura",
        "page_x": "pagina_x",
        "page_y": "pagina_y",
    },
    "InfoLink": {"text": "texto", "location": "local"},
    "InteligenciaDominio": {
        "registered_domain": "dominio_registrado",
        "platform": "plataforma",
        "notes": "notas",
        "errors": "erros",
        "collected_at": "coletada_em",
    },
    "InteligenciaIp": {
        "network": "rede",
        "organisation": "organizacao",
        "country": "pais",
        "abuse_emails": "emails_abuso",
    },
    "InteligenciaRdap": {
        "registrar": "registrador",
        "created": "criado",
        "expires": "expira",
        "updated": "atualizado",
        "age_days": "idade_dias",
        "name_servers": "servidores_nomes",
        "holder": "titular",
        "holder_document": "documento_titular",
        "holder_kind": "tipo_titular",
        "abuse_emails": "emails_abuso",
    },
    "ItemEvidencia": {"kind": "tipo", "location": "local", "content": "conteudo", "above_fold": "acima_dobra"},
    "PerfilMarca": {
        "display_name": "nome_exibicao",
        "language": "idioma",
        "strong_terms": "termos_fortes",
        "weak_terms": "termos_fracos",
        "official_domains": "dominios_oficiais",
        "official_social_profiles": "perfis_sociais_oficiais",
        "institution_names": "nomes_instituicao",
        "institution_cnpjs": "cnpjs_instituicao",
        "domain_patterns": "padroes_dominio",
        "products": "produtos",
        "establishment_types": "tipos_estabelecimento",
        "regions": "regioes",
        "platforms": "plataformas",
        "official_claim_phrases": "frases_oficialidade",
        "correspondent_disclosure_phrases": "frases_declaracao_correspondente",
        "partnership_phrases": "frases_parceria",
        "competitor_institutions": "instituicoes_concorrentes",
        "sensitive_service_phrases": "frases_servicos_sensiveis",
        "homonym_context_phrases": "frases_contexto_homonimo",
        "deprioritized_domains": "dominios_despriorizados",
        "homonym_domains": "dominios_homonimos",
        "typosquat_seeds": "sementes_typosquat",
        "typosquat_combo_labels": "rotulos_combo_typosquat",
        "typosquat_keywords": "palavras_chave_typosquat",
        "legacy_logo_references": "referencias_logo_antiga",
        "legacy_logo_exempt_domains": "dominios_isentos_logo_antiga",
        "legacy_logo_search_urls": "urls_busca_logo_antiga",
    },
    "RegistroConsulta": {
        "source": "fonte",
        "query": "consulta",
        "hypothesis_id": "id_hipotese",
        "result_count": "qtd_resultados",
        "error": "erro",
        "metadata": "metadados",
        "executed_at": "executada_em",
    },
    "ResultadoDescoberta": {
        "source": "fonte",
        "original_url": "url_original",
        "query": "consulta",
        "hypothesis_id": "id_hipotese",
        "title": "titulo",
        "snippet": "trecho",
        "rank": "posicao",
        "metadata": "metadados",
        "discovered_at": "descoberto_em",
    },
    "Revisao": {"reviewer": "revisor", "prompt_version": "versao_prompt"},
    "SaidaAnaliseMarca": {
        "case": "caso",
        "classification": "classificacao",
        "priority": "prioridade",
        "dimensions": "dimensoes",
        "brand_confusion_indicators": "indicadores_confusao_marca",
        "legitimate_relationship_indicators": "indicadores_relacao_legitima",
        "observed_facts": "fatos_observados",
        "uncertainties": "incertezas",
        "evidence_references": "referencias_evidencia",
        "reasoning_summary": "resumo_raciocinio",
        "requires_human_review": "exige_revisao_humana",
        "extraction_confidence": "confianca_extracao",
        "evidence_sufficiency": "suficiencia_evidencia",
        "possible_prompt_injection": "possivel_injecao_prompt",
        "legacy_visual_identity": "identidade_visual_antiga",
    },
    "SaidaRevisao": {
        "decision": "decisao",
        "revised_classification": "classificacao_revisada",
        "revised_priority": "prioridade_revisada",
        "challenges": "contestacoes",
        "contradictory_evidence": "evidencias_contraditorias",
        "recollect_reason": "motivo_recoleta",
        "reasoning_summary": "resumo_raciocinio",
        "requires_human_review": "exige_revisao_humana",
    },
}

VALORES_ENUM: dict[str, Any] = {
    "DISCOVERED": "DESCOBERTA",
    "QUEUED": "NA_FILA",
    "COLLECTING": "COLETANDO",
    "COLLECTED": "COLETADA",
    "ANALYZING": "ANALISANDO",
    "REVIEWING": "REVISANDO",
    "COMPLETED": "CONCLUIDA",
    "FAILED": "FALHOU",
    "INCONCLUSIVE": "INCONCLUSIVA",
    "NO_INDICATION": "SEM_INDICIO",
    "REVIEW_REQUIRED": "REQUER_REVISAO",
    "STRONG_INDICATION": "INDICIO_FORTE",
    "LOW": "BAIXA",
    "MEDIUM": "MEDIA",
    "HIGH": "ALTA",
    "NONE": "NENHUMA",
    "A_LEGITIMATE_COMMERCIAL_MENTION": "A_MENCAO_COMERCIAL_LEGITIMA",
    "B_AMBIGUOUS_COMMUNICATION": "B_COMUNICACAO_AMBIGUA",
    "C_POSSIBLE_OFFICIAL_CHANNEL": "C_POSSIVEL_CANAL_OFICIAL",
    "D_STRONG_IMPERSONATION": "D_PERSONIFICACAO_FORTE",
    "E_INSUFFICIENT_EVIDENCE": "E_EVIDENCIA_INSUFICIENTE",
    "NOT_RELEVANT": "NAO_RELEVANTE",
    "SUFFICIENT": "SUFICIENTE",
    "PARTIAL": "PARCIAL",
    "INSUFFICIENT": "INSUFICIENTE",
    "CONFIRMED_COBAN": "COBAN_CONFIRMADO",
    "POSSIBLE_COBAN": "COBAN_POSSIVEL",
    "OTHER_THIRD_PARTY": "OUTRO_TERCEIRO",
    "UNKNOWN_IDENTITY": "IDENTIDADE_DESCONHECIDA",
    "VISUAL_IDENTITY": "IDENTIDADE_VISUAL",
    "DECLARED_COMMERCIAL_IDENTITY": "IDENTIDADE_COMERCIAL_DECLARADA",
    "CORRESPONDENT_TRANSPARENCY": "TRANSPARENCIA_CORRESPONDENTE",
    "INSTITUTIONAL_LANGUAGE": "LINGUAGEM_INSTITUCIONAL",
    "LOGO_USAGE": "USO_LOGO",
    "SERVICE_JOURNEY": "JORNADA_ATENDIMENTO",
    "CONTACT_CHANNELS": "CANAIS_CONTATO",
    "CONSUMER_CONFUSION": "CONFUSAO_CONSUMIDOR",
    "CONFUSION": "CONFUSAO",
    "LEGITIMATE": "LEGITIMO",
    "NEUTRAL": "NEUTRO",
    "NOT_ASSESSED": "NAO_AVALIADO",
    "BLOCKED": "BLOQUEADA",
    "NOT_COLLECTED": "NAO_COLETADA",
    "NORMATIVE": "NORMATIVO",
    "CONTRACTUAL": "CONTRATUAL",
    "INTERNAL_POLICY": "POLITICA_INTERNA",
    "HYPOTHESIS_OF_NONCONFORMITY": "HIPOTESE_NAO_CONFORMIDADE",
    "HYPOTHESIS_CONDITIONAL_ON_LINK": "HIPOTESE_CONDICIONADA_VINCULO",
    "NO_INDICATION_OBSERVED": "SEM_INDICIO_OBSERVADO",
    "CANNOT_ASSESS": "NAO_AVALIAVEL",
    "NOT_APPLICABLE_UNLESS_COBAN": "NAO_APLICAVEL_SALVO_COBAN",
    "CONFIRM": "CONFIRMAR",
    "DOWNGRADE": "REBAIXAR",
    "RECOLLECT": "RECOLETAR",
    "HUMAN_REVIEW": "REVISAO_HUMANA",
}

FAMILIAS_HIPOTESE: dict[str, Any] = {
    "H1-identity": "H1-identidade",
    "H2-product": "H2-produto",
    "H3-service": "H3-servico",
    "H4-official-language": "H4-linguagem-oficial",
    "H5-establishment": "H5-estabelecimento",
    "H6-platform": "H6-plataforma",
    "H7-threat-intel": "H7-inteligencia-ameacas",
    "H8-lookalike": "H8-semelhante",
    "H10-fraud": "H10-fraude",
    "H12-legacy-logo:lens": "H12-logo-antiga:lens",
    "H13-legacy-focus": "H13-foco-logo-antiga",
    "H14-threat": "H14-ameaca",
    "H12-legacy-logo": "H12-logo-antiga",
}

FONTES: dict[str, Any] = {"import:text": "importacao:texto"}

TIPO_ARTEFATO: dict[str, Any] = {
    "screenshot_viewport": "captura_viewport",
    "screenshot_full": "captura_completa",
    "text": "texto",
    "page_data": "dados_pagina",
}

TIPO_ITEM_EVIDENCIA: dict[str, Any] = {
    "text": "texto",
    "title": "titulo",
    "heading": "cabecalho_secao",
    "contact": "contato",
    "image": "imagem",
    "form": "formulario",
    "screenshot": "captura",
    "network": "rede",
    "domain": "dominio",
    "legacy_logo": "logo_antiga",
}

TIPO_FATO: dict[str, Any] = {
    "legal_name": "razao_social",
    "trade_name": "nome_fantasia",
    "phone": "telefone",
    "social_profile": "perfil_social",
    "institution_name_displayed": "nome_instituicao_exibido",
    "correspondent_declaration": "declaracao_correspondente",
    "partnership_declaration": "declaracao_parceria",
    "domain": "dominio",
}

ANALISADOR: dict[str, Any] = {
    "heuristic-v1": "heuristica-v1",
    "heuristic-reviewer-v1": "heuristica-revisor-v1",
    "heuristic-v1+grounding-index": "heuristica-v1+indice-grounding",
    "focus:logo-antiga": "foco:logo-antiga",
}

STATUS_CAMPANHA: dict[str, Any] = {
    "RUNNING": "EM_EXECUCAO",
    "INTERRUPTED": "INTERROMPIDA",
    "FAILED": "FALHOU",
    "PAUSED_BUDGET": "PAUSADA_ORCAMENTO",
    "PAUSED": "PAUSADA",
    "COMPLETED": "CONCLUIDA",
}

STATUS_CONSULTA: dict[str, Any] = {"ERROR": "ERRO", "EMPTY": "VAZIA", "SKIPPED": "PULADA"}

STATUS_REVISAO_HUMANA: dict[str, Any] = {"PENDING": "PENDENTE", "NOT_REQUIRED": "NAO_EXIGIDA"}

TIPO_AVALIACAO: dict[str, Any] = {
    "identity": "identidade",
    "brand": "marca",
    "compliance": "conformidade",
    "review": "revisao",
}

ETAPA: dict[str, Any] = {
    "prepare": "preparar",
    "identity": "identidade",
    "brand": "marca",
    "compliance": "conformidade",
    "review": "revisao",
    "collect": "coleta",
    "orchestrator": "orquestrador",
}

AGENTE: dict[str, Any] = {
    "orchestrator": "orquestrador",
    "discovery": "descoberta",
    "evidence": "evidencia",
    "identity": "identidade",
    "brand_analyst": "analista_marca",
    "compliance": "conformidade",
    "reviewer": "revisor",
    "discovery.grounding": "descoberta.grounding",
}

DECISAO: dict[str, Any] = {
    "campaign_started": "campanha_iniciada",
    "UNSAFE_ALLOW_HOSTS_ACTIVE": "HOSTS_LIBERADOS_INSEGURO_ATIVO",
    "resumed": "retomada",
    "rejected_url": "url_rejeitada",
    "excluded_url": "url_excluida",
    "completed": "concluida",
    "llm_unavailable": "llm_indisponivel",
    "run_finished": "execucao_finalizada",
    "mapped": "mapeado",
    "reused": "reaproveitado",
}

CHAVE_CHECKPOINT: dict[str, Any] = {
    "imported_hits": "resultados_importados",
    "discovery_partial_hits": "resultados_parciais_descoberta",
    "imported_recorded": "importadas_registradas",
    "discovery": "descoberta",
    "discovery_done": "descoberta_concluida",
}

METADADOS: dict[str, Any] = {
    "query_db_id": "id_consulta_db",
    "recovered_from_google_redirect": "recuperado_de_redirecionamento_google",
    "lookalike_of": "semelhante_a",
    "plain_variant_of": "variante_simples_de",
    "plain_variants": "variantes_simples",
    "displayed_link": "link_exibido",
    "matched_page": "pagina_correspondente",
    "unresolved_google_redirects": "redirecionamentos_google_nao_resolvidos",
    "recovered_google_redirect_hosts": "hosts_redirecionamento_google_recuperados",
    "visual_match_of_legacy_logo": "correspondencia_visual_logo_antiga",
    "source_site": "site_fonte",
    "image_url": "url_imagem",
    "scan_time": "momento_varredura",
    "entry_id": "id_entrada",
    "logged_at": "registrado_em",
    "grounding_uri": "uri_grounding",
    "grounding_domain": "dominio_grounding",
    "web_search_queries": "consultas_busca_web",
    "model": "modelo",
    "grounding_chunks": "fragmentos_grounding",
    "unresolved": "nao_resolvidos",
    "search_entry_point_html": "html_ponto_entrada_busca",
    "model_text_excerpt": "trecho_texto_modelo",
    "supplement": "complemento",
    "addresses": "enderecos",
    "permutations_checked": "permutacoes_verificadas",
    "resolved_public": "resolvidos_publicos",
    "resolved_non_public": "resolvidos_nao_publicos",
}

VALORES_METADADOS: dict[str, Any] = {"social_dork": "dork_social"}

PARAMETROS: dict[str, Any] = {
    "allowed_ports": "portas_permitidas",
    "analysis_model": "modelo_analise",
    "brand": "marca",
    "brand_file": "arquivo_marca",
    "brave_api_key": "chave_api_brave",
    "browser_proxy": "proxy_navegador",
    "browser_proxy_pin_ip": "proxy_navegador_fixar_ip",
    "browser_sandbox": "sandbox_navegador",
    "browser_user_agent": "user_agent_navegador",
    "coban_registry_authoritative": "cadastro_coban_oficial",
    "coban_registry_path": "caminho_cadastro_coban",
    "coban_registry_reference_date": "data_referencia_cadastro_coban",
    "coban_seed_batch": "lote_sementes_coban",
    "coban_seed_limit": "limite_sementes_coban",
    "contract_requirements_path": "caminho_requisitos_contratuais",
    "discovery_model": "modelo_descoberta",
    "doh_url": "url_doh",
    "domain_intel": "inteligencia_dominio",
    "focus": "foco",
    "gcp_credentials_path": "caminho_credenciais_gcp",
    "gcp_location": "local_gcp",
    "gcp_project": "projeto_gcp",
    "http_timeout_s": "timeout_http_s",
    "ignore_https_errors": "ignorar_erros_https",
    "imported": "importadas",
    "imported_urls": "urls_importadas",
    "limit": "limite",
    "llm_max_output_tokens": "llm_max_tokens_saida",
    "llm_max_retries": "llm_max_retentativas",
    "llm_temperature": "llm_temperatura",
    "log_level": "nivel_log",
    "max_attempts": "max_tentativas",
    "max_fullpage_height": "max_altura_pagina_inteira",
    "max_html_bytes": "max_bytes_html",
    "max_llm_requests": "max_requisicoes_llm",
    "max_llm_tokens": "max_tokens_llm",
    "max_recollects": "max_recoletas",
    "max_requests_per_page": "max_requisicoes_por_pagina",
    "max_search_queries": "max_consultas_busca",
    "max_text_chars": "max_caracteres_texto",
    "min_queue_score": "pontuacao_minima_fila",
    "nav_timeout_ms": "timeout_navegacao_ms",
    "output_dir": "dir_saida",
    "page_budget_s": "orcamento_pagina_s",
    "rdap_base_url": "url_base_rdap",
    "retry_base_delay_s": "atraso_base_retentativa_s",
    "review_all": "revisar_todas",
    "review_model": "modelo_revisao",
    "serper_api_key": "chave_api_serper",
    "serper_pages": "paginas_serper",
    "serper_plain_variants": "variantes_simples_serper",
    "settle_ms": "espera_estabilizar_ms",
    "sources": "fontes",
    "unsafe_allow_hosts": "hosts_liberados_inseguro",
    "urlscan_api_key": "chave_api_urlscan",
    "viewport_height": "altura_viewport",
    "viewport_width": "largura_viewport",
    "workers": "paralelismo",
}

PAYLOAD_ENTIDADE: dict[str, Any] = {"owner_cnpjs": "cnpjs_responsavel", "registry_match": "correspondencia_cadastro"}

TIPOS_ERRO: dict[str, Any] = {
    "BudgetExceeded": "OrcamentoEsgotado",
    "CampaignBusy": "CampanhaOcupada",
    "ConcurrentModification": "ModificacaoConcorrente",
    "ConnectorError": "ErroConector",
    "InvalidTransition": "TransicaoInvalida",
    "LLMOutputInvalid": "SaidaLLMInvalida",
    "LLMUnavailable": "LLMIndisponivel",
    "PolicyViolation": "ViolacaoPolitica",
    "TransientLLMError": "ErroTransitorioLLM",
}

TABELAS: dict[str, Any] = {
    "agent_decisions": [
        "decisoes_agentes",
        {
            "campaign_id": "id_campanha",
            "candidate_id": "id_candidata",
            "agent": "agente",
            "decision": "decisao",
            "detail_json": "detalhe_json",
            "at": "em",
        },
    ],
    "assessments": [
        "avaliacoes",
        {
            "candidate_id": "id_candidata",
            "kind": "tipo",
            "data_json": "dados_json",
            "created_at": "criado_em",
            "evidence_id": "id_evidencia",
        },
    ],
    "campaign_leases": ["travas_campanha", {"campaign_id": "id_campanha", "owner": "dono", "heartbeat": "pulso"}],
    "campaigns": [
        "campanhas",
        {
            "brand": "marca",
            "params_json": "parametros_json",
            "created_at": "criado_em",
            "updated_at": "atualizado_em",
            "finished_at": "finalizado_em",
        },
    ],
    "candidates": [
        "candidatas",
        {
            "campaign_id": "id_campanha",
            "domain_id": "id_dominio",
            "normalized_url": "url_normalizada",
            "registrable_domain": "dominio_registravel",
            "priority_score": "pontuacao_prioridade",
            "priority_reasons_json": "motivos_prioridade_json",
            "attempts": "tentativas",
            "recollects": "recoletas",
            "last_error": "ultimo_erro",
            "created_at": "criado_em",
            "updated_at": "atualizado_em",
        },
    ],
    "checkpoints": [
        "checkpoints",
        {"campaign_id": "id_campanha", "key": "chave", "value_json": "valor_json", "updated_at": "atualizado_em"},
    ],
    "discovery_results": [
        "resultados_descoberta",
        {
            "candidate_id": "id_candidata",
            "query_id": "id_consulta",
            "source": "fonte",
            "original_url": "url_original",
            "hit_json": "resultado_json",
            "discovered_at": "descoberto_em",
        },
    ],
    "domains": [
        "dominios",
        {"campaign_id": "id_campanha", "registrable_domain": "dominio_registravel", "first_seen": "visto_em"},
    ],
    "entities": [
        "entidades",
        {
            "campaign_id": "id_campanha",
            "entity_key": "chave_entidade",
            "display_name": "nome_exibicao",
            "classification": "classificacao",
            "data_json": "dados_json",
            "updated_at": "atualizado_em",
        },
    ],
    "entity_links": [
        "vinculos_entidade",
        {"entity_id": "id_entidade", "candidate_id": "id_candidata", "basis": "fundamento"},
    ],
    "errors": [
        "erros",
        {
            "campaign_id": "id_campanha",
            "candidate_id": "id_candidata",
            "stage": "etapa",
            "error_type": "tipo_erro",
            "message": "mensagem",
            "at": "em",
        },
    ],
    "evidence": [
        "evidencias",
        {
            "candidate_id": "id_candidata",
            "attempt": "tentativa",
            "final_url": "url_final",
            "manifest_path": "caminho_manifesto",
            "manifest_sha256": "sha256_manifesto",
            "data_json": "dados_json",
            "collected_at": "coletada_em",
        },
    ],
    "llm_usage": [
        "uso_llm",
        {
            "campaign_id": "id_campanha",
            "agent": "agente",
            "model": "modelo",
            "prompt_tokens": "tokens_prompt",
            "output_tokens": "tokens_saida",
            "total_tokens": "tokens_total",
            "at": "em",
        },
    ],
    "queries": [
        "consultas",
        {
            "campaign_id": "id_campanha",
            "source": "fonte",
            "hypothesis_id": "id_hipotese",
            "query_text": "texto_consulta",
            "result_count": "qtd_resultados",
            "error": "erro",
            "metadata_json": "metadados_json",
            "executed_at": "executada_em",
        },
    ],
    "schema_migrations": ["schema_migrations", {}],
    "state_transitions": [
        "transicoes_estado",
        {
            "candidate_id": "id_candidata",
            "from_status": "status_origem",
            "to_status": "status_destino",
            "note": "nota",
            "at": "em",
        },
    ],
}

INDICES: dict[str, Any] = {
    "idx_candidates_status": "idx_candidatas_status",
    "idx_assessments": "idx_avaliacoes",
    "idx_assessments_ev": "idx_avaliacoes_evid",
    "idx_discovery_unique": "idx_descoberta_unico",
}

# Keys of free-form payloads (agent_decisions.detail_json, campaigns.params_json, checkpoint values).
DETALHE: dict[str, str] = {
    **PARAMETROS,
    "priority": "prioridade",
    "human_review": "revisao_humana",
    "counts": "contagens",
    "llm_requests": "requisicoes_llm",
    "llm_tokens": "tokens_llm",
    "recovered_in_flight": "recuperadas_em_andamento",
    "retry_failed": "repetir_falhas",
    "effective_params": "parametros_efetivos",
    "overrides": "sobrescritas",
    "reason": "motivo",
    "hits": "resultados",
    "unique_urls": "urls_unicas",
    "rejected": "rejeitados",
    "queued": "na_fila",
    "sources_used": "fontes_usadas",
    "queries": "consultas",
    "diagnostics": "diagnosticos",
    "attempt": "tentativa",
    "evidence_id": "id_evidencia",
    "final_url": "url_final",
    "errors": "erros",
    "blocked_requests": "requisicoes_bloqueadas",
    "manifest": "manifesto",
    "entity_key": "chave_entidade",
    "confidence": "confianca",
    "case": "caso",
    "analyzer": "analisador",
    "guardrails": "salvaguardas",
    "revised": "revisada",
    "challenges": "contestacoes",
    "reviewer": "revisor",
    "findings": "achados",
    "warning": "aviso",
    "source": "fonte",
}


# ---------------------------------------------------------------------------- scalar codes
def hipotese(valor: Any) -> Any:
    """``H2-product:financiamento`` -> ``H2-produto:financiamento`` (the suffix is data)."""
    if not isinstance(valor, str):
        return valor
    familia, sep, resto = valor.partition(":")
    return FAMILIAS_HIPOTESE.get(familia, familia) + sep + resto


def fonte(valor: Any) -> Any:
    if not isinstance(valor, str):
        return valor
    if valor.startswith("import:file:"):
        return "importacao:arquivo:" + valor[len("import:file:") :]
    return FONTES.get(valor, valor)


def analisador(valor: Any) -> Any:
    if not isinstance(valor, str):
        return valor
    if valor.startswith("focus:"):
        return "foco:" + valor[len("focus:") :]
    return ANALISADOR.get(valor, valor)


def enum(valor: Any) -> Any:
    return VALORES_ENUM.get(valor, valor) if isinstance(valor, str) else valor


def decisao(valor: str) -> str:
    if valor.startswith("final:"):
        return "final:" + enum(valor[len("final:") :])
    return DECISAO.get(valor, VALORES_ENUM.get(valor, valor))


def _tabela(mapa: dict[str, Any]) -> Callable[[Any], Any]:
    return lambda v: mapa.get(v, v) if isinstance(v, str) else v


def metadados(valor: Any) -> Any:
    """Top-level keys only: nested entries mirror third-party API payloads."""
    if not isinstance(valor, dict):
        return valor
    saida: dict[str, Any] = {}
    for k, v in valor.items():
        if k == "supplement" and isinstance(v, str):
            v = VALORES_METADADOS.get(v, v)
        saida[METADADOS.get(k, k)] = v
    return saida


def _resumo_conformidade(valor: Any) -> Any:
    if not isinstance(valor, list):
        return valor
    saida = []
    for linha in valor:
        if isinstance(linha, str) and ": " in linha:
            req, _, status = linha.rpartition(": ")
            linha = f"{req}: {enum(status)}"
        saida.append(linha)
    return saida


def _lista(f: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda v: [f(x) for x in v] if isinstance(v, list) else v


ESPECIAIS: dict[tuple[str, str], Callable[[Any], Any]] = {
    ("Artefato", "tipo"): _tabela(TIPO_ARTEFATO),
    ("ItemEvidencia", "tipo"): _tabela(TIPO_ITEM_EVIDENCIA),
    ("CorrespondenciaVisual", "tipo"): _tabela(TIPO_ITEM_EVIDENCIA),
    ("FatoIdentidade", "tipo"): _tabela(TIPO_FATO),
    ("AvaliacaoMarca", "analisador"): analisador,
    ("Revisao", "revisor"): analisador,
    ("ResultadoDescoberta", "fonte"): fonte,
    ("RegistroConsulta", "fonte"): fonte,
    ("ResultadoDescoberta", "id_hipotese"): hipotese,
    ("RegistroConsulta", "id_hipotese"): hipotese,
    ("HipoteseBusca", "id"): hipotese,
    ("RegistroConsulta", "status"): _tabela(STATUS_CONSULTA),
    ("ResultadoDescoberta", "metadados"): metadados,
    ("RegistroConsulta", "metadados"): metadados,
    ("DecisaoFinal", "status_revisao_humana"): _tabela(STATUS_REVISAO_HUMANA),
    ("DecisaoFinal", "resumo_conformidade"): _resumo_conformidade,
    # free-text topic; the model sometimes names a dimension (``LOGO_USAGE``): exact matches only
    ("Contestacao", "topico"): enum,
}


# ---------------------------------------------------------------------------- model-driven walk
def _por_tipo(anotacao: Any, valor: Any) -> Any:
    origem = get_origin(anotacao)
    args = get_args(anotacao)
    if origem is Annotated:
        return _por_tipo(args[0], valor)
    if origem in (Union, types.UnionType):
        for a in args:
            if a is type(None):
                continue
            base = get_origin(a) or a
            if isinstance(valor, dict) and (base is dict or (inspect.isclass(base) and issubclass(base, BaseModel))):
                return _por_tipo(a, valor)
            if isinstance(valor, list) and base in (list, tuple, set, frozenset):
                return _por_tipo(a, valor)
            if isinstance(valor, str) and inspect.isclass(base) and issubclass(base, Enum):
                return _por_tipo(a, valor)
        return valor
    if origem in (list, tuple, set, frozenset) and isinstance(valor, list):
        elem = args[0] if args else Any
        return [_por_tipo(elem, x) for x in valor]
    if origem is dict and isinstance(valor, dict):
        tipo_valor = args[1] if len(args) == 2 else Any
        return {k: _por_tipo(tipo_valor, v) for k, v in valor.items()}
    if inspect.isclass(anotacao):
        if issubclass(anotacao, BaseModel) and isinstance(valor, dict):
            return traduzir_modelo(anotacao, valor)
        if issubclass(anotacao, Enum) and isinstance(valor, str):
            return enum(valor)
    return valor


def traduzir_modelo(cls: type[BaseModel], dados: Any) -> Any:
    """Rename the keys/values of a dumped ``cls`` written by an English-named build.

    Idempotent: payloads already in the new vocabulary come back unchanged."""
    if not isinstance(dados, dict):
        return dados
    mapa: dict[str, str] = {}
    for base in reversed(cls.__mro__):
        mapa.update(CAMPOS_POR_MODELO.get(base.__name__, {}))
    saida: dict[str, Any] = {}
    for chave, valor in dados.items():
        nova = mapa.get(chave, chave)
        campo = cls.model_fields.get(nova)
        if campo is not None:
            especial = ESPECIAIS.get((cls.__name__, nova))
            if especial is None:
                for base in cls.__mro__[1:]:
                    especial = ESPECIAIS.get((base.__name__, nova))
                    if especial:
                        break
            valor = especial(valor) if especial else _por_tipo(campo.annotation, valor)
        saida[nova] = valor
    return saida


def detalhe(valor: Any, chave: str | None = None) -> Any:
    """Free-form payloads (decision details, campaign params, discovery checkpoint)."""
    if isinstance(valor, dict):
        saida: dict[str, Any] = {}
        for k, v in valor.items():
            nk = DETALHE.get(k, k)
            if nk == "consultas" and isinstance(v, dict):
                v = {_chave_consulta(ck): cv for ck, cv in v.items()}
            elif nk == "contagens" and isinstance(v, dict):
                v = {enum(ck): cv for ck, cv in v.items()}
            elif nk == "achados" and isinstance(v, dict):
                v = {ck: enum(cv) for ck, cv in v.items()}
            else:
                v = detalhe(v, nk)
            saida[nk] = v
        return saida
    if isinstance(valor, list):
        return [detalhe(x, chave) for x in valor]
    if isinstance(valor, str):
        if chave in ("fonte", "fontes", "fontes_usadas"):
            return fonte(valor)
        if chave in ("analisador", "revisor"):
            return analisador(valor)
        if chave == "status":
            return STATUS_CAMPANHA.get(valor, VALORES_ENUM.get(valor, valor))
        return VALORES_ENUM.get(valor, valor)
    return valor


def _chave_consulta(chave: str) -> str:
    """``serper:EMPTY`` -> ``serper:VAZIA`` (see ``resumir_consultas``)."""
    src, sep, status = chave.rpartition(":")
    return f"{fonte(src)}{sep}{STATUS_CONSULTA.get(status, status)}" if sep else chave


# ---------------------------------------------------------------------------- migration v3
def _json_compacto(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_livre(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _reescrever(con: sqlite3.Connection, tabela: str, colunas: dict[str, Callable[[Any], Any]]) -> int:
    """Apply ``colunas[col](value)`` to every row; returns how many rows changed."""
    nomes = list(colunas)
    sel = ", ".join(f'"{c}"' for c in nomes)
    alteradas = 0
    for linha in con.execute(f'SELECT rowid, {sel} FROM "{tabela}"').fetchall():  # noqa: S608
        novos = []
        mudou = False
        for c, atual in zip(nomes, linha[1:], strict=True):
            novo = colunas[c](atual) if atual is not None else None
            mudou = mudou or novo != atual
            novos.append(novo)
        if mudou:
            sets = ", ".join(f'"{c}"=?' for c in nomes)
            con.execute(f'UPDATE "{tabela}" SET {sets} WHERE rowid=?', (*novos, linha[0]))  # noqa: S608
            alteradas += 1
    return alteradas


def _json_modelo(cls: type[BaseModel]) -> Callable[[str], str]:
    def f(texto: str) -> str:
        antigo = json.loads(texto)
        novo = traduzir_modelo(cls, antigo)
        return texto if novo == antigo else _json_compacto(novo)

    return f


def _json_com(fn: Callable[[Any], Any], estilo: Callable[[Any], str] = _json_livre) -> Callable[[str], str]:
    def f(texto: str) -> str:
        antigo = json.loads(texto)
        novo = fn(antigo)
        return texto if novo == antigo else estilo(novo)

    return f


def migrar_v3(con: sqlite3.Connection) -> None:
    """Schema v3: Portuguese table/column/index names and payload vocabulary.

    Runs inside the migration transaction with foreign keys disabled by the caller."""
    from simio.nucleo import modelos as m

    for velha, (nova, colunas) in TABELAS.items():
        if nova != velha:
            con.execute(f'ALTER TABLE "{velha}" RENAME TO "{nova}"')
        for col, nova_col in colunas.items():
            con.execute(f'ALTER TABLE "{nova}" RENAME COLUMN "{col}" TO "{nova_col}"')
    for velho, novo in INDICES.items():
        (sql,) = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (velho,)).fetchone()
        con.execute(f'DROP INDEX "{velho}"')
        con.execute(sql.replace(f"INDEX {velho} ", f"INDEX {novo} ", 1))

    por_tipo: dict[str, type[BaseModel]] = {
        "identidade": m.AtribuicaoIdentidade,
        "marca": m.AvaliacaoMarca,
        "conformidade": m.AvaliacaoConformidade,
        "revisao": m.Revisao,
        "final": m.DecisaoFinal,
    }
    hits = _json_com(_lista(lambda h: traduzir_modelo(m.ResultadoDescoberta, h)))

    def payload_entidade(d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        saida = {PAYLOAD_ENTIDADE.get(k, k): v for k, v in d.items()}
        if isinstance(saida.get("correspondencia_cadastro"), dict):
            saida["correspondencia_cadastro"] = traduzir_modelo(
                m.CorrespondenciaCadastro, saida["correspondencia_cadastro"]
            )
        return saida

    tab = _tabela
    _reescrever(con, "campanhas", {"status": tab(STATUS_CAMPANHA), "parametros_json": _json_com(detalhe)})
    _reescrever(
        con,
        "consultas",
        {
            "fonte": fonte,
            "id_hipotese": hipotese,
            "status": tab(STATUS_CONSULTA),
            "metadados_json": _json_com(metadados),
        },
    )
    _reescrever(con, "candidatas", {"status": enum})
    _reescrever(con, "resultados_descoberta", {"fonte": fonte, "resultado_json": _json_modelo(m.ResultadoDescoberta)})
    _reescrever(con, "transicoes_estado", {"status_origem": enum, "status_destino": enum})
    _reescrever(con, "evidencias", {"status": enum, "dados_json": _json_modelo(m.Evidencia)})
    _reescrever(con, "entidades", {"classificacao": enum, "dados_json": _json_com(payload_entidade)})
    _reescrever(con, "avaliacoes", {"tipo": tab(TIPO_AVALIACAO)})
    for tipo, cls in por_tipo.items():
        conv = _json_modelo(cls)
        for rowid, dados in con.execute("SELECT rowid, dados_json FROM avaliacoes WHERE tipo=?", (tipo,)).fetchall():
            novo = conv(dados)
            if novo != dados:
                con.execute("UPDATE avaliacoes SET dados_json=? WHERE rowid=?", (novo, rowid))
    _reescrever(
        con, "decisoes_agentes", {"agente": tab(AGENTE), "decisao": decisao, "detalhe_json": _json_com(detalhe)}
    )
    _reescrever(con, "erros", {"etapa": tab(ETAPA), "tipo_erro": tab(TIPOS_ERRO)})
    _reescrever(con, "uso_llm", {"agente": tab(AGENTE)})
    _reescrever(con, "checkpoints", {"chave": tab(CHAVE_CHECKPOINT)})
    for rowid, chave, valor in con.execute("SELECT rowid, chave, valor_json FROM checkpoints").fetchall():
        if chave == "descoberta":
            novo = _json_com(detalhe)(valor)
        elif chave in ("resultados_parciais_descoberta", "resultados_importados"):
            novo = hits(valor)
        else:
            continue
        if novo != valor:
            con.execute("UPDATE checkpoints SET valor_json=? WHERE rowid=?", (novo, rowid))
