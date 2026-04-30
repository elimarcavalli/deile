# Changelog

All notable changes to the DEILE project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [5.1.0] — Multi-Provider Model Router

### Added
- **Multi-Provider Support**: Anthropic, OpenAI, DeepSeek providers alongside the existing Gemini integration.
- **ModelCatalog**: Immutable model registry loaded from `deile/config/model_providers.yaml` with pricing, context window, and capability data for 9+ models.
- **TierRouter + RoutingPolicy**: Tier-aware cascade routing (tier_1→flagship, tier_2→balanced, tier_3→fast, tier_4→ultra-fast) with `task_optimized` and `cost_optimized` strategies.
- **CircuitBreaker**: Per-provider consecutive-failure threshold with configurable cooldown (CLOSED→OPEN→HALF_OPEN→CLOSED).
- **UnifiedStreamEvent**: Typed streaming events (`TEXT_DELTA`, `TOOL_USE_START/END`, `USAGE_FINAL`, `ERROR`) across all providers.
- **UsageRepository**: SQLite-backed append-only usage store (`data/usage.db`) with per-session and per-provider-daily cost aggregation.
- **BudgetGuard**: Per-session and per-provider-daily spend limits with `BudgetExceeded` exception.
- **Intent tier classification**: `classify_tier(IntentAnalysisResult) → ModelTier` mapper + `tier:` field on all 11 intent patterns in YAML.
- **Prompt caching**: Anthropic `cache_control: ephemeral` on system prompt; OpenAI automatic caching extraction; DeepSeek `prompt_cache_hit_tokens`.
- **JSONL observability**: `debug_logger.log_router_event()` appends structured events to `logs/router_events.jsonl`.
- **`/model` command rewrite**: subcommands `list`, `current`, `use <provider:model>`, `use auto`, `strategy`, `cost`, `budget`.
- **Conditional bootstrap**: `bootstrap_providers()` in `deile/core/models/bootstrap.py` — registers only providers whose API key env var is set; zero providers → clear error.
- **Performance benchmarks**: router `select()` < 1ms avg; schema translation < 10ms for 10 tools.
- **Integration test stubs**: E2E tests for Anthropic and OpenAI fallback (skipped without real API keys).

### Changed
- `deile.py` entry point no longer requires `GOOGLE_API_KEY` — any of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY` is sufficient.
- `GeminiProvider.chat_with_tools` now implements unified contract `(messages, tools, system_instruction) → (str, list, ModelUsage)`.
- `_generate_response_stream` in `agent.py` consumes `UnifiedStreamEvent` from all providers; legacy str-yielding providers remain backward compatible.
- `pytest.ini` section corrected from `[tool:pytest]` to `[pytest]`; `perf` and `integration` markers registered.

### Fixed
- `deile/storage/` package was blocked by `.gitignore` `storage/` rule — added `!deile/storage/` negation.
- `SandboxCommand` import in `deile/commands/builtin/__init__.py` wrapped in try/except to avoid crash when `deile.infrastructure.security` is absent.

## [Unreleased]

### Added
- **Complete DEILE 5.0 ULTRA Transformation**:
  - **GitHub Infrastructure**: CODEOWNERS, issue/PR templates, Dependabot, workflow CI/CD completa (272 linhas)
  - **Intent Analysis System**: `intent_patterns.yaml` (436 linhas) + `intent_analyzer.py` (833 linhas) + `intent_metrics.py` (657 linhas)
  - **Task Orchestration**: SQLite Task Manager (574 linhas) + Workflow Executor (404 linhas) + Task Manager base (570 linhas)
  - **Memory System**: Working Memory (458 linhas) + Persistent Memory (635 linhas) + Memory Models (229 linhas)
  - **Enhanced Personas**: Sistema BaseAutonomousPersona (915 linhas) + Developer instructions (64 linhas) + Loader com MD support
  - **Universal File Support**: Análise de arquivos binários, detecção de magic numbers, suporte a imagens/PDFs/archives
  - **Advanced Metrics**: Sistema completo de tracking de performance para intent analysis com cache e alertas
  - **Legal & Compliance**: MIT License, .gitignore abrangente (40 entradas), documentação estruturada

### Changed
- **Arquitetura Completamente Reestruturada**:
  - Versão 4.0.0 → 5.0.0 ("deile-5.0-ultra")
  - Migração de personas hardcoded para sistema dinâmico MD-based
  - Context manager aprimorado com integração de personas
  - Agent core com detecção automática de workflows via intent analyzer
  - File tools com suporte universal a arquivos (binários + texto)
  - Timeout de requests aumentado de 30s → 120s
  - Paths relativos em configurações para melhor portabilidade

### Fixed
- **Correções Críticas de Autonomia**:
  - Sistema não detectava workflows automaticamente
  - Personas não carregavam instruções dinâmicas de arquivos MD
  - Context manager não integrava corretamente com personas
  - File tools falhavam com arquivos binários ou encodings complexos
  - Clear command não estava atualizado para v5.0
  - Configurações hardcoded impediam flexibilidade

### Security
- **Melhorias Substanciais de Segurança**:
  - Audit logger expandido com logs de planos e aprovações
  - Permission manager com instância singleton segura
  - API keys nunca mais salvas em arquivos de configuração
  - Validação robusta de tamanhos de arquivo e tipos permitidos
  - Proteção contra exposição de dados sensíveis via .gitignore expandido

## [5.0.0] - 2025-09-14

### Added
- **Complete GitHub Infrastructure**:
  - CODEOWNERS file for code ownership management
  - Issue templates for bug reports and feature requests
  - Pull request template with comprehensive checklist
  - Dependabot configuration for automated dependency updates
  - Comprehensive CI/CD pipeline with multi-OS testing, security scans, and quality checks
- **New Core Modules**:
  - Intent analysis system with configurable patterns (intent_patterns.yaml)
  - Intent analyzer and metrics modules for better user input understanding
  - Advanced orchestration system with SQLite task manager and workflow executor
- **Memory & Personas System**:
  - Multi-layer memory system (working, persistent, models)
  - Dynamic persona system with developer instructions
  - Enhanced persona loader with instruction management
- **Project Documentation & Legal**:
  - MIT License
  - Comprehensive .gitignore with project-specific exclusions
- **Enhanced Configuration**:
  - Extended settings.json with new features and optimizations
  - File encoding detection and size limits
  - Improved security and safety checks

### Changed
- **Version Upgrade**: 4.0.0 → 5.0.0
- **Build Information**: Updated to "deile-5.0-ultra" (2025-09-14)
- **Configuration Improvements**:
  - Increased request timeout from 30s to 120s for better reliability
  - Updated working directories to use relative paths
  - Enhanced file handling with encoding detection
- **Core System Updates**:
  - Enhanced agent, context manager, and security modules
  - Improved UI and console interface
  - Updated file tools with better functionality
  - Enhanced clear command implementation

### Fixed
- BG001: DEILE não funcionando de forma autônoma - Corrigida captura de tool results do Chat Session response
- BG002: Sistema de personas hardcoded - Implementada integração completa do PersonaManager com system instructions dinâmicas
- BG003: Instruções hardcoded no código - Criado sistema InstructionLoader para carregar todas as instruções de arquivos MD
- BG004: Sistema não detecta intenção corretamente - Implementado sistema de self-awareness para perguntas sobre o DEILE com resposta completa e formatada

### Security
- Enhanced audit logger and permissions system
- Improved file safety checks and validation
- Added security scanning in CI/CD pipeline
- Protected sensitive files through .gitignore updates

## Development Notes

This CHANGELOG.md is automatically updated when work items are completed.
Each feature, bugfix, improvement, and major change will be documented here.