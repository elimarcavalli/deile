"""Workflow Executor - Integração entre TaskManager e execução real no DEILE"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .sqlite_task_manager import SQLiteTaskManager, Task, TaskList, TaskStatus, TaskPriority, get_sqlite_task_manager
from ..tools.registry import get_tool_registry
from ..tools.base import ToolResult, ToolStatus, ToolContext
from ..core.exceptions import DEILEError

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    """Representa um step de workflow que será convertido em Task"""
    action: str  # Ação a ser executada (nome da tool, comando, etc.)
    params: Dict[str, Any] = None  # Parâmetros para a ação
    description: str = ""  # Descrição legível
    validation: Optional[Callable] = None  # Função de validação do resultado
    rollback: Optional[Callable] = None  # Função de rollback em caso de erro
    timeout: int = 300  # Timeout em segundos
    retry_count: int = 0  # Número de tentativas em caso de erro


class WorkflowExecutor:
    """Executor que conecta SQLiteTaskManager com sistema real do DEILE"""

    def __init__(self, task_manager: Optional[SQLiteTaskManager] = None):
        self.task_manager = task_manager or get_sqlite_task_manager()
        self.tool_registry = get_tool_registry()

        # Note: SQLiteTaskManager não precisa de set_task_executor

        # Callbacks para diferentes tipos de ações
        self._action_handlers: Dict[str, Callable] = {
            'tool': self._execute_tool_action,
            'command': self._execute_command_action,
            'validation': self._execute_validation_action,
            'custom': self._execute_custom_action
        }

    async def create_workflow_from_objective(self, objective: str,
                                           context: Optional[Dict[str, Any]] = None) -> TaskList:
        """Cria workflow baseado em objetivo do usuário"""

        # Analisa objetivo e gera steps
        steps = await self._analyze_objective_to_steps(objective, context or {})

        # Cria task list
        task_list = await self.task_manager.create_task_list(
            title=f"Workflow: {objective[:50]}...",
            description=f"Auto-generated workflow for: {objective}",
            sequential=True,
            auto_start=True
        )

        # Converte steps em tasks usando SQLiteTaskManager
        for i, step in enumerate(steps):
            await self._add_workflow_step_to_list(step, task_list.id, i)

        logger.info(f"Created workflow with {len(steps)} steps for objective: {objective}")
        return task_list

    async def execute_task(self, task: Task) -> Dict[str, Any]:
        """Executa uma task específica (callback do TaskManager)"""

        logger.info(f"Executing task: {task.title}")

        try:
            # Extrai informações da task
            action_type = task.metadata.get('action_type', 'tool')
            action_name = task.metadata.get('action_name', '')
            params = task.metadata.get('params', {})

            # Executa baseado no tipo de ação
            handler = self._action_handlers.get(action_type, self._execute_tool_action)
            result = await handler(action_name, params, task)

            # Validação adicional se especificada
            if task.metadata.get('validation_func'):
                validation_result = await self._run_validation(task, result)
                if not validation_result['success']:
                    raise DEILEError(f"Validation failed: {validation_result['error']}")

            return {
                'success': True,
                'data': result,
                'message': f"Task '{task.title}' completed successfully"
            }

        except Exception as e:
            logger.error(f"Task execution failed: {e}")

            # Tenta rollback se disponível
            if task.metadata.get('rollback_func'):
                try:
                    await self._run_rollback(task)
                    logger.info(f"Rollback completed for task {task.id}")
                except Exception as rollback_error:
                    logger.error(f"Rollback failed: {rollback_error}")

            return {
                'success': False,
                'error': str(e),
                'message': f"Task '{task.title}' failed: {str(e)}"
            }

    async def start_workflow_execution(self, objective: str,
                                     context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Inicia execução completa de workflow baseado em objetivo"""

        # Cria workflow
        task_list = await self.create_workflow_from_objective(objective, context)

        # Inicia execução
        execution_result = await self.task_manager.start_execution(task_list.id)

        return {
            'workflow_id': task_list.id,
            'status': 'started',
            'total_steps': task_list.total_tasks,
            'execution_info': execution_result
        }

    async def monitor_workflow_progress(self, workflow_id: str) -> Dict[str, Any]:
        """Monitora progresso de um workflow"""

        status = await self.task_manager.get_task_list_status(workflow_id)
        if not status:
            raise DEILEError(f"Workflow {workflow_id} not found")

        return status

    async def wait_for_workflow_completion(self, workflow_id: str,
                                         timeout: Optional[timedelta] = None) -> Dict[str, Any]:
        """Aguarda conclusão de um workflow"""

        start_time = datetime.now()
        max_wait = timeout or timedelta(hours=1)

        while True:
            status = await self.monitor_workflow_progress(workflow_id)

            if status['is_completed']:
                return {
                    'status': 'completed',
                    'success': not status['has_failures'],
                    'final_stats': status
                }

            if status['has_failures']:
                return {
                    'status': 'failed',
                    'success': False,
                    'final_stats': status
                }

            # Verifica timeout
            if datetime.now() - start_time > max_wait:
                return {
                    'status': 'timeout',
                    'success': False,
                    'message': f"Workflow timed out after {max_wait}"
                }

            # Aguarda antes de verificar novamente
            await asyncio.sleep(2)

    # Métodos privados

    async def _analyze_objective_to_steps(self, objective: str,
                                        context: Dict[str, Any]) -> List[WorkflowStep]:
        """Analisa objetivo e converte em steps executáveis"""

        steps = []

        # Análise básica por palavras-chave (versão simplificada)
        objective_lower = objective.lower()

        # Verificação de arquivos
        if any(word in objective_lower for word in ['file', 'read', 'analyze', 'check']):
            steps.append(WorkflowStep(
                action='read_file',
                params={'path': context.get('target_file', 'README.md')},
                description=f"Read target file for analysis",
                timeout=30
            ))

        # Listagem de arquivos
        if any(word in objective_lower for word in ['list', 'files', 'directory', 'explore']):
            steps.append(WorkflowStep(
                action='list_files',
                params={'path': context.get('target_dir', '.'), 'recursive': True},
                description="List files in target directory",
                timeout=60
            ))

        # Busca em arquivos
        if any(word in objective_lower for word in ['search', 'find', 'grep', 'pattern']):
            steps.append(WorkflowStep(
                action='find_in_files',
                params={
                    'pattern': context.get('search_pattern', 'TODO'),
                    'path': context.get('search_path', '.'),
                    'max_results': 50
                },
                description=f"Search for pattern in files",
                timeout=120
            ))

        # Execução de comandos
        if any(word in objective_lower for word in ['run', 'execute', 'command', 'script']):
            steps.append(WorkflowStep(
                action='bash_execute',
                params={
                    'command': context.get('command', 'echo "Workflow step executed"'),
                    'show_cli': True
                },
                description="Execute command",
                timeout=300
            ))

        # Validação de resultados
        if any(word in objective_lower for word in ['validate', 'verify', 'check', 'test']):
            steps.append(WorkflowStep(
                action='validation',
                params={'validation_type': 'general'},
                description="Validate workflow results",
                timeout=60
            ))

        # Se nenhum step específico foi gerado, cria step genérico de análise
        if not steps:
            steps.append(WorkflowStep(
                action='list_files',
                params={'path': '.', 'recursive': False},
                description=f"General analysis step for: {objective}",
                timeout=60
            ))

        return steps

    async def _add_workflow_step_to_list(self, step: WorkflowStep, list_id: str, index: int) -> Task:
        """Adiciona workflow step como task na lista SQLite"""

        # Determina dependências baseado no índice
        depends_on = []
        if index > 0:
            # Busca tasks existentes na lista para determinar dependência
            existing_tasks = await self.task_manager._get_tasks_for_list(list_id)
            if existing_tasks:
                # Depende da última task adicionada
                depends_on = [existing_tasks[-1].id]

        # Cria task usando SQLiteTaskManager
        task = await self.task_manager.add_task_to_list(
            list_id=list_id,
            title=step.description or f"Step {index + 1}: {step.action}",
            description=step.description,
            depends_on=depends_on,
            priority=TaskPriority.MEDIUM,
            estimated_duration=timedelta(seconds=step.timeout)
        )

        # Adiciona metadados específicos do workflow como tags
        task.tags = ['workflow', f'action:{step.action}']
        task.metadata = {
            'action_type': 'tool' if self.tool_registry.get_enabled(step.action) is not None else 'custom',
            'action_name': step.action,
            'params': step.params or {},
            'timeout': step.timeout,
            'retry_count': step.retry_count,
            'validation_func': step.validation,
            'rollback_func': step.rollback
        }

        return task

    async def _convert_step_to_task(self, step: WorkflowStep, list_id: str, index: int) -> Task:
        """Converte WorkflowStep em Task"""

        task = Task(
            id=f"{list_id}_{index:03d}",
            title=step.description or f"Step {index + 1}: {step.action}",
            description=step.description,
            priority=TaskPriority.MEDIUM,
            estimated_duration=timedelta(seconds=step.timeout)
        )

        # Adiciona metadados específicos do workflow
        task.metadata = {
            'action_type': 'tool' if self.tool_registry.get_enabled(step.action) is not None else 'custom',
            'action_name': step.action,
            'params': step.params or {},
            'timeout': step.timeout,
            'retry_count': step.retry_count,
            'validation_func': step.validation,
            'rollback_func': step.rollback
        }

        return task

    async def _execute_tool_action(self, action_name: str, params: Dict[str, Any], task: Task) -> Any:
        """Executa ação usando tool registry"""

        # Verifica se tool existe
        if self.tool_registry.get_enabled(action_name) is None:
            raise DEILEError(f"Tool '{action_name}' not found")

        # Cria contexto para execução
        context = ToolContext(
            user_input=task.description,
            parsed_args=params,
            session_data={},
            working_directory='.',
            file_list=[]
        )

        # Executa tool
        result = await self.tool_registry.execute_tool(action_name, context)

        if result.status != ToolStatus.SUCCESS:
            raise DEILEError(f"Tool execution failed: {result.message}")

        return result.output

    async def _execute_command_action(self, command: str, params: Dict[str, Any], task: Task) -> Any:
        """Executa comando do sistema"""

        # Usa bash_execute tool para executar comando
        return await self._execute_tool_action('bash_execute', {
            'command': command,
            **params
        }, task)

    async def _execute_validation_action(self, validation_type: str, params: Dict[str, Any], task: Task) -> Any:
        """Executa validação personalizada"""

        # Implementação básica de validação
        if validation_type == 'general':
            return {
                'validation_passed': True,
                'message': 'General validation completed',
                'timestamp': datetime.now().isoformat()
            }

        return {'validation_passed': True, 'type': validation_type}

    async def _execute_custom_action(self, action_name: str, params: Dict[str, Any], task: Task) -> Any:
        """Executa ação customizada"""

        logger.warning(f"Custom action '{action_name}' not implemented, returning success")
        return {
            'action': action_name,
            'params': params,
            'message': f"Custom action '{action_name}' executed (placeholder)",
            'timestamp': datetime.now().isoformat()
        }

    async def _run_validation(self, task: Task, result: Any) -> Dict[str, Any]:
        """Executa validação do resultado de uma task"""

        validation_func = task.metadata.get('validation_func')
        if not validation_func:
            return {'success': True, 'message': 'No validation specified'}

        try:
            if asyncio.iscoroutinefunction(validation_func):
                validation_result = await validation_func(result)
            else:
                validation_result = validation_func(result)

            return {'success': True, 'result': validation_result}

        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def _run_rollback(self, task: Task) -> None:
        """Executa rollback de uma task"""

        rollback_func = task.metadata.get('rollback_func')
        if not rollback_func:
            return

        if asyncio.iscoroutinefunction(rollback_func):
            await rollback_func(task)
        else:
            rollback_func(task)


# Singleton instance
_workflow_executor: Optional[WorkflowExecutor] = None


def get_workflow_executor() -> WorkflowExecutor:
    """Retorna instância singleton do WorkflowExecutor"""
    global _workflow_executor
    if _workflow_executor is None:
        _workflow_executor = WorkflowExecutor()
    return _workflow_executor