"""HybridCompleter - Sistema unificado de autocompletar para @ (arquivos) e / (comandos)"""

import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ...commands.registry import get_command_registry

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


def _preview_first_words(text: str, word_limit: int = 20) -> str:
    """Return a concise preview with the first *word_limit* words."""
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if not normalized:
        return "Sem descricao..."

    words = normalized.split(" ")
    if len(words) <= word_limit:
        return f"{normalized.rstrip('.')}..."
    return f"{' '.join(words[:word_limit])}..."


class HybridCompleter(Completer):
    """
    Completer unificado que suporta:
    - @ para arquivos/diretórios
    - / para comandos slash
    - Autocompletar normal para texto livre
    """
    
    def __init__(self, config_manager=None, working_directory: Optional[str] = None):
        self.config_manager = config_manager
        self.working_directory = working_directory or os.getcwd()
        self._command_registry = None
    
    def get_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        """Retorna completions baseado no contexto atual"""
        text = document.text_before_cursor
        
        # Determina o tipo de completion baseado no contexto
        if self._is_slash_command_context(text):
            # Completion de comandos slash
            yield from self._get_command_completions(document, complete_event)
        elif self._is_file_reference_context(text):
            # Completion de arquivos
            yield from self._get_file_completions(document, complete_event)
        else:
            # Completion padrão (pode incluir sugestões contextuais)
            yield from self._get_contextual_completions(document, complete_event)
    
    def _is_slash_command_context(self, text: str) -> bool:
        """Verifica se está no contexto de comando slash"""
        text_stripped = text.strip()
        
        # Comando slash sempre começa no início da linha
        if text_stripped.startswith('/'):
            return True
        
        # Se contém slash mas não no início, não é comando
        return False
    
    def _is_file_reference_context(self, text: str) -> bool:
        """Verifica se está no contexto de referência de arquivo"""
        # Arquivo pode ser referenciado em qualquer lugar da linha
        return '@' in text
    
    def _get_command_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        """Completions para comandos slash"""
        try:
            text = document.text_before_cursor.strip()
            
            # Verifica se é realmente um comando slash
            if not text.startswith('/'):
                return
            
            # Extrai o fragmento do comando (tudo após a barra)
            command_fragment = text[1:]  # Remove a barra inicial
            start_position = -(len(command_fragment))
            
            # Obtém registry de comandos
            if not self._command_registry:
                if self.config_manager:
                    self._command_registry = get_command_registry(self.config_manager)
                else:
                    return
            
            fragment_lower = command_fragment.lower()
            # Busca comandos que fazem match (case-insensitive)
            for command in self._command_registry.get_enabled_commands():
                if command.name.lower().startswith(fragment_lower):
                    # Cria completion com informações extras
                    display_text = f"/{command.name}"
                    description = command.description
                    
                    # Adiciona indicador de tipo
                    category = getattr(command, "category", "")
                    if category == "skills":
                        preview = _preview_first_words(getattr(command, "_skill_body", ""))
                        display_meta = f"✨ SKILL: {preview}"
                    elif category == "commands":
                        preview = _preview_first_words(getattr(command, "_skill_body", ""))
                        display_meta = f"🛠️ COMMAND: {preview}"
                    else:
                        cmd_type = "🤖 LLM" if command.has_prompt_template else "⚡ Direct"
                        display_meta = f"{cmd_type} - {description}"
                    
                    yield Completion(
                        text=command.name,
                        start_position=start_position,
                        display=display_text,
                        display_meta=display_meta
                    )
                
        except Exception as e:
            logger.error(f"Error in command completions: {e}")
    
    def _get_file_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        """Completions para arquivos com @"""
        try:
            text = document.text_before_cursor
            
            # Encontra a posição do último '@'
            at_pos = text.rfind('@')
            if at_pos == -1:
                return
            
            # Extrai o caminho após @
            path_fragment = text[at_pos + 1:]
            start_position = -(len(path_fragment))
            
            # Determina diretório base
            if path_fragment.startswith('/') or (os.name == 'nt' and ':' in path_fragment):
                # Caminho absoluto
                base_dir = Path(path_fragment).parent if path_fragment else Path('/')
                filename_fragment = Path(path_fragment).name if path_fragment else ""
            else:
                # Caminho relativo
                if '/' in path_fragment or '\\' in path_fragment:
                    base_dir = Path(self.working_directory) / Path(path_fragment).parent
                    filename_fragment = Path(path_fragment).name
                else:
                    base_dir = Path(self.working_directory)
                    filename_fragment = path_fragment
            
            # Lista arquivos no diretório
            if not base_dir.exists():
                return
                
            for item in sorted(base_dir.iterdir()):
                if item.name.startswith('.'):
                    continue  # Skip hidden files by default
                
                if item.name.lower().startswith(filename_fragment.lower()):
                    # Calcula caminho relativo se possível
                    try:
                        rel_path = item.relative_to(self.working_directory)
                        display_path = str(rel_path)
                    except ValueError:
                        display_path = str(item)
                    
                    # Adiciona / para diretórios
                    if item.is_dir():
                        completion_text = display_path + "/"
                        display_text = f"📁 {display_path}/"
                        display_meta = "Directory"
                    else:
                        completion_text = display_path
                        # Determina ícone baseado na extensão
                        suffix = item.suffix.lower()
                        icon = self._get_file_icon(suffix)
                        display_text = f"{icon} {display_path}"
                        
                        # Adiciona informações do arquivo
                        try:
                            size = item.stat().st_size
                            size_str = self._format_file_size(size)
                            display_meta = f"File ({size_str})"
                        except Exception:
                            display_meta = "File"
                    
                    yield Completion(
                        text=completion_text,
                        start_position=start_position,
                        display=display_text,
                        display_meta=display_meta
                    )
                    
        except Exception as e:
            logger.error(f"Error in file completions: {e}")
    
    def _get_contextual_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        """Completions contextuais gerais"""
        text = document.text_before_cursor.strip()
        
        # Se texto vazio, mostra dicas iniciais
        if not text:
            yield Completion(
                text="/help",
                start_position=0,
                display="💡 /help",
                display_meta="Ver comandos disponíveis"
            )
            yield Completion(
                text="@",
                start_position=0,
                display="📁 @",
                display_meta="Referenciar arquivos"
            )
            return
        
        # Sugestões baseadas em palavras comuns
        common_starters = [
            # --- Desenvolvimento Geral ---
            ("Crie", "Gerar arquivo, script ou esqueleto de código"),
            ("Implemente", "Implementar uma função, método ou nova feature"),
            ("Adicione", "Adicionar um trecho de código ou funcionalidade a um existente"),
            ("Gere", "Gerar uma classe, função, script de build"),
            ("Refatore", "Refatorar código existente para melhorar clareza e manutenção"),
            ("Otimize", "Otimizar código PowerScript ou uma query SQL para performance"),
            ("Corrija", "Corrigir um bug ou comportamento inesperado no código"),
            ("Debug", "Ajudar a investigar a causa raiz de um erro ou stacktrace"),
            ("Analise", "Fazer análise estática de um código em busca de melhorias"),
            ("Explique", "Explicar o funcionamento de um trecho de código complexo ou legado"),
            ("Documente", "Gerar documentação para uma função, objeto ou processo"),

            # --- Testes ---
            ("Gere testes para", "Gerar testes unitários ou de integração para o código"),
            ("Escreva testes", "Criar casos de teste específicos para uma funcionalidade"),

            # --- Banco de Dados (DB2) ---
            ("Crie script SQL DB2", "Gerar DDL ou DML específico para IBM DB2"),
            ("Otimize esta query DB2", "Analisar e sugerir índices ou reescrita para DB2"),
            ("Gere Stored Procedure", "Criar esqueleto de Stored Procedure para DB2"),
            ("Converta SQL para DB2", "Traduzir um script SQL de outro dialeto para a sintaxe do DB2"),

            # --- PowerBuilder Específico ---
            ("Converta PowerScript para", "Traduzir lógica de PowerScript para Python"),
            ("Crie script OrcaScript", "Gerar script de automação de build para PowerBuilder"),
            ("Modernize esta tela", "Sugerir melhorias de UX/UI para uma janela PowerBuilder"),

            # --- Python Específico ---
            ("Crie API Python com", "Gerar API REST (Flask/FastAPI) para acessar DB2"),
            ("Crie script Python", "Gerar um script de automação ou processamento de dados"),

            # --- Versionamento (GitLab) ---
            ("Crie branch", "Sugerir um nome de branch seguindo padrões e gerar comandos"),
            ("Revisar MR", "Sugerir um checklist e pontos de atenção para revisar um Merge Request"),

            # --- Automação e Ambiente ---
            ("Como configurar ambiente", "Descrever passos para configurar o ambiente de desenvolvimento local"),
            # --- PowerBuilder ---
            ("Analise profundamente este código PowerScript...", "para identificar 'code smells', gargalos de performance e pontos de melhoria."),
            ("Crie uma função PowerScript segura e otimizada para...", "[descreva aqui a finalidade da função, entradas e saídas esperadas]."),
            ("Refatore completamente este objeto PowerBuilder...", "para melhorar a manutenibilidade, performance e aplicar princípios SOLID."),
            ("Documente de forma técnica esta função/objeto PowerBuilder...", "explicando sua lógica de negócio, parâmetros e dependências."),
            ("Proponha uma modernização de UX/UI para esta janela PowerBuilder...", "sugerindo novos controles, layout e melhorias de usabilidade."),

            # --- IBM DB2 ---
            ("Revise e otimize esta query SQL para DB2...", "analisando o plano de execução e sugerindo a criação ou ajuste de índices."),
            ("Elabore um script SQL transacional para DB2 que...", "[descreva a operação de DDL ou DML, garantindo a atomicidade]."),
            ("Desenvolva uma Stored Procedure robusta em DB2 para...", "com tratamento completo de exceções, transações e logging de erros."),
            ("Crie a estrutura de tabelas no DB2 para armazenar...", "[descreva a entidade de negócio, ex: 'dados de nota fiscal eletrônica']."),

            # --- Python ---
            ("Desenvolva uma API REST em Python com FastAPI para...", "[descreva o recurso a ser exposto, ex: 'consultar clientes no DB2']."),
            ("Crie um script Python de automação para...", "[descreva a tarefa, ex: 'ler um CSV e inserir os dados no DB2 de forma segura']."),
            ("Escreva testes unitários com Pytest para esta função Python...", "garantindo a cobertura dos principais cenários de sucesso e de falha."),

            # --- Arquitetura e Estratégia ---
            ("Desenhe uma arquitetura de microsserviço para substituir...", "[descreva a funcionalidade do sistema PowerBuilder a ser modernizada]."),
            ("Estruture um pipeline de CI/CD no GitLab para automatizar...", "o processo de build e deploy de um projeto PowerBuilder usando OrcaScript."),
            ("Sugira uma estratégia de versionamento e branch no GitLab para...", "[descreva o contexto do projeto ou da equipe de desenvolvimento]."),
        ]
        
        for starter, description in common_starters:
            if starter.lower().startswith(text.lower()) and len(text) < len(starter):
                yield Completion(
                    text=starter,
                    start_position=-len(text),
                    display=f"💭 {starter}",
                    display_meta=description
                )
    
    def _get_file_icon(self, suffix: str) -> str:
        """Retorna ícone apropriado para arquivo baseado na extensão"""
        icon_map = {
            '.py': '🐍',
            '.js': '📜',
            '.ts': '📘',
            '.html': '🌐',
            '.css': '🎨',
            '.json': '📋',
            '.yaml': '⚙️',
            '.yml': '⚙️',
            '.md': '📝',
            '.txt': '📄',
            '.log': '📊',
            '.sql': '🗄️',
            '.jpg': '🖼️',
            '.png': '🖼️',
            '.gif': '🖼️',
            '.pdf': '📕',
            '.doc': '📘',
            '.docx': '📘',
        }
        return icon_map.get(suffix, '📄')
    
    def _format_file_size(self, size: int) -> str:
        """Formata tamanho do arquivo de forma legível"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f}{unit}"
            size /= 1024.0
        return f"{size:.1f}TB"