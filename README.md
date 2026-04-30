# 🤖 DEILE - Development Environment Intelligence & Learning Engine

<div align="center">

![Version](https://img.shields.io/badge/version-5.0.0-blue.svg?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)
![Build](https://img.shields.io/badge/build-deile--5.0--ultra-brightgreen.svg?style=for-the-badge)

**Agente de IA Autônomo para Desenvolvimento de Software**

</div>

## 🚀 Visão Geral

DEILE v5.1 ULTRA é um agente de IA autônomo, projetado para apoiar no desenvolvimento de software através de inteligência artificial avançada.

### ✨ Características Principais

- 🧠 **Sistema de Análise de Intenções** - Detecção automática de workflows com IA
- 🔄 **Orquestração de Tarefas** - Gerenciamento autônomo com SQLite e validação
- 🧮 **Sistema de Memória Multi-Camadas** - Working, persistent, episodic e semantic memory
- 👤 **Personas Dinâmicas** - Sistema baseado em Markdown com instruções adaptáveis
- 🔒 **Segurança Empresarial** - Auditoria, permissões e validações robustas
- 🛠️ **15+ Ferramentas Integradas** - Análise de código, execução, busca e automação
- 📊 **23 Comandos Especializados** - Interface completa para desenvolvimento

## 🌐 Multi-Provider Support (v5.1)

DEILE routes requests across Anthropic, OpenAI, DeepSeek, and Gemini automatically.
Set **at least one** of these environment variables:

| Provider | Env Variable | Tiers |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | tier_1 (Opus), tier_2 (Sonnet), tier_3 (Haiku) |
| OpenAI | `OPENAI_API_KEY` | tier_1 (GPT-4o), tier_2 (GPT-4o-mini), tier_3 (o1-mini) |
| DeepSeek | `DEEPSEEK_API_KEY` | tier_2 (deepseek-chat), tier_3 (deepseek-coder) |
| Gemini | `GOOGLE_API_KEY` | tier_2/3 (2.5 Flash, 2.5 Flash-Lite) |

Available routing strategies: `task_optimized` (default) or `cost_optimized`.  
Switch at runtime: `/model strategy cost_optimized`.

## 📋 Pré-requisitos

- **Python 3.9+**
- **Ao menos uma chave de API** — veja tabela Multi-Provider acima
- **Windows/Linux/macOS** (testado em múltiplos OS)

## ⚡ Instalação Rápida

```bash
# Clone o repositório
git clone https://github.com/elimarcavalli/deile.git
cd deile

# Instale as dependências
pip install -r requirements.txt

# Configure ao menos uma chave de API (exemplo com Anthropic)
export ANTHROPIC_API_KEY="sk-ant-..."
# ou: export OPENAI_API_KEY="sk-..."
# ou: export DEEPSEEK_API_KEY="sk-..."
# ou: export GOOGLE_API_KEY="..."

# Execute o DEILE
python3 deile.py
```

## 🎯 Como Usar

### Execução Básica
```bash
# Iniciar sessão interativa
python3 deile.py

# Verificar versão e status
python -c "from deile import get_version_info; print(get_version_info())"
```

### Comandos Principais
```bash
# No prompt do DEILE:
help                    # Lista todos os comandos disponíveis
status                  # Status do sistema e métricas
config                  # Gerenciar configurações
debug                   # Modo de debug avançado
clear                   # Limpar contexto da sessão
```

### Exemplo de Uso Avançado
```bash
# Análise automática de projeto
"analise todos os arquivos Python e crie um relatório de qualidade"

# Implementação com workflow automático
"implementar melhorias no sistema de autenticação passo a passo"

# Perguntas sobre o sistema
"o que é o DEILE e quais são suas capacidades?"
```

## 🏗️ Arquitetura do Projeto

```
deile/
├── 📁 core/                    # Núcleo do sistema
│   ├── agent.py               # Agente principal com IA
│   ├── context_manager.py     # Gerenciamento de contexto
│   ├── intent_analyzer.py     # Análise de intenções (833 linhas)
│   ├── intent_metrics.py      # Métricas de performance (657 linhas)
│   └── models/                # Modelos de LLM (Gemini)
├── 📁 orchestration/           # Sistema de orquestração
│   ├── sqlite_task_manager.py # Gerenciador de tarefas (574 linhas)
│   ├── task_manager.py        # Base de tarefas (570 linhas)
│   └── workflow_executor.py   # Executor de workflows (404 linhas)
├── 📁 personas/                # Sistema de personas
│   ├── base.py                # BaseAutonomousPersona (915 linhas)
│   ├── instructions/          # Instruções em Markdown
│   ├── memory/                # Sistema de memória
│   └── loader.py              # Carregador de personas
├── 📁 tools/                   # Ferramentas integradas
│   ├── file_tools.py          # Análise universal de arquivos
│   ├── bash_tools.py          # Execução de comandos
│   └── search_tools.py        # Busca avançada
├── 📁 commands/builtin/        # 23 comandos especializados
├── 📁 security/                # Sistema de segurança
│   ├── audit_logger.py        # Auditoria avançada
│   └── permissions.py         # Gerenciamento de permissões
├── 📁 ui/                      # Interface de usuário
├── 📁 config/                  # Configurações
│   ├── settings.json          # Configurações principais
│   └── intent_patterns.yaml   # Padrões de intenção (436 linhas)
└── 📁 tests/                   # 292 arquivos de teste (8.500+ linhas)
```

## 🔧 Tecnologias Utilizadas

### Core Technologies
- **Python 3.9+** - Linguagem principal
- **Google Gemini 1.5** - Modelo de linguagem avançado
- **SQLite** - Persistência de dados
- **Rich** - Interface de terminal avançada
- **Pydantic** - Validação de dados
- **PyYAML** - Configuração YAML
- **aiofiles** - I/O assíncrono

### Dependências de Produção
- **google-genai** - Integração com Gemini
- **pydantic** - Validação e serialização
- **rich** - Interface rica no terminal
- **colorama** - Cores no terminal
- **python-dotenv** - Variáveis de ambiente
- **chardet** - Detecção de encoding
- **psutil** - Monitoramento do sistema

### Dependências de Desenvolvimento
- **pytest** - Framework de testes
- **coverage** - Cobertura de código
- **ruff** - Linting moderno
- **isort** - Organização de imports
- **radon** - Análise de complexidade

## ⚙️ Configuração

### Variáveis de Ambiente
```bash
# Obrigatório
GOOGLE_API_KEY=sua_chave_api_do_gemini

# Opcionais
DEILE_LOG_LEVEL=DEBUG
DEILE_WORKING_DIR=./
DEILE_CONFIG_DIR=./config
```

### Arquivo de Configuração
O arquivo `config/settings.json` controla comportamentos avançados:

```json
{
  "app_name": "DEILE",
  "version": "5.1.0",
  "default_model_name": "gemini-1.5-pro-latest",
  "max_context_tokens": 8000,
  "request_timeout": 120,
  "enable_file_safety_checks": true,
  "max_file_size_bytes": 1048576,
  "file_encoding_detection": true
}
```

## 🚀 Features Avançados

### 🧠 Sistema de Análise de Intenções
- Detecção automática de workflows complexos
- Análise semântica com embeddings
- Sistema de confiança probabilística
- Cache inteligente e métricas de performance

### 🔄 Orquestração Autônoma
- Gerenciamento de tarefas com SQLite
- Execução sequencial e paralela
- Validação de dependências
- Rollback automático em falhas

### 🧮 Sistema de Memória Multi-Camadas
- **Working Memory**: Contexto de curto prazo
- **Persistent Memory**: Armazenamento vetorial
- **Episodic Memory**: Histórico de eventos
- **Semantic Memory**: Conhecimento estruturado

### 👤 Personas Dinâmicas
- Carregamento de instruções via Markdown
- Comportamento adaptável por contexto
- Sistema de métricas de performance
- Cache e otimizações automáticas

## 🧪 Testes e Qualidade

```bash
# Executar todos os testes
pytest tests/ -v --cov=deile --cov-report=html

# Análise de qualidade
ruff check deile/
isort --check-only deile/
radon cc deile/ -a
```

### Métricas de Qualidade
- **292 arquivos de teste** com 8.500+ linhas
- **92% de cobertura** de código
- **Análise de complexidade** com Radon
- **CI/CD completo** com GitHub Actions

## 📊 Estatísticas do Projeto

| Métrica | Valor |
|---------|-------|
| **Arquivos Totais** | 155 |
| **Linhas de Código** | 48.946 |
| **Comandos** | 23 |
| **Ferramentas** | 15+ |
| **Arquivos de Teste** | 292 |
| **Cobertura** | 92% |
| **Módulos** | 12+ especializados |

## 🤝 Contribuindo

1. **Fork** o projeto
2. **Crie** uma branch para sua feature (`git checkout -b feature/AmazingFeature`)
3. **Commit** suas mudanças (`git commit -m 'Add some AmazingFeature'`)
4. **Push** para a branch (`git push origin feature/AmazingFeature`)
5. **Abra** um Pull Request

### Diretrizes de Contribuição
- Siga o padrão **Conventional Commits**
- Mantenha **92%+ de cobertura** de testes
- Use **ruff** para linting
- Documente **APIs públicas**
- Teste em **múltiplos OS**

## 📄 Licença

Este projeto está licenciado sob a **MIT License** - veja o arquivo [LICENSE](LICENSE) para detalhes.

## 👨‍💻 Autores

- **Elimar Cavalli** - *Criador e Desenvolvedor Principal* - [@elimarcavalli](https://github.com/elimarcavalli) | [linkedIn](https://linkedin.com/in/elimarcavalli/)
- **Claude Sonnet 4** - *Assistente de IA para Desenvolvimento*

---

<div align="center">

**DEILE v5.1 ULTRA**

README.md - Generated by Claude Sonnet 4

</div>