"""Workflow Executor - Integração entre TaskManager e execução real no DEILE"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ..core.exceptions import DEILEError
from ..tools.base import ToolContext, ToolStatus
from ..tools.registry import get_tool_registry
from .sqlite_task_manager import (SQLiteTaskManager, Task, TaskList,
                                  TaskPriority, TaskStatus,
                                  get_sqlite_task_manager)

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    """Representa um step de workflow que será convertido em Task"""
    action: str
    params: Dict[str, Any] = None
    description: str = ""
    validation: Optional[Callable] = None
    rollback: Optional[Callable] = None
    timeout: int = 300
    retry_count: int = 0


class WorkflowExecutor:
    """Executor que conecta SQLiteTaskManager com sistema real do DEILE"""

    def __init__(self, task_manager: Optional[SQLiteTaskManager] = None):
        self.task_manager = task_manager or get_sqlite_task_manager()
        self.tool_registry = get_tool_registry()

        self._action_handlers: Dict[str, Callable] = {
            'tool': self._execute_tool_action,
            'command': self._execute_command_action,
            'validation': self._execute_validation_action,
        }
        # Keeps background loop tasks alive (prevents GC cancellation)
        self._running_loops: set = set()
        # In-memory store for Callables that cannot be JSON-serialized to SQLite
        self._step_funcs: Dict[str, Dict[str, Optional[Callable]]] = {}

    async def create_workflow_from_objective(self, objective: str,
                                           context: Optional[Dict[str, Any]] = None) -> TaskList:
        """Cria workflow baseado em objetivo do usuário."""
        steps = await self._analyze_objective_to_steps(objective, context or {})

        task_list = await self.task_manager.create_task_list(
            title=f"Workflow: {objective[:50]}...",
            description=f"Auto-generated workflow for: {objective}",
            sequential=True,
            auto_start=True,
        )

        for i, step in enumerate(steps):
            await self._add_workflow_step_to_list(step, task_list.id, i)

        logger.info("Created workflow with %d steps for objective: %s", len(steps), objective)
        return task_list

    async def execute_task(self, task: Task) -> Dict[str, Any]:
        """Executa uma task específica."""
        logger.info("Executing task: %s", task.title)

        try:
            action_type = task.metadata.get('action_type', 'tool')
            action_name = task.metadata.get('action_name', '')
            params = task.metadata.get('params', {})

            handler = self._action_handlers.get(action_type)
            if handler is None:
                raise DEILEError(f"Unknown action type: '{action_type}'")

            result = await handler(action_name, params, task)

            step_funcs = self._step_funcs.get(task.id, {})
            if step_funcs.get('validation_func'):
                validation_result = await self._run_validation(task, result, step_funcs['validation_func'])
                if not validation_result['success']:
                    raise DEILEError(f"Validation failed: {validation_result['error']}")

            return {
                'success': True,
                'data': result,
                'message': f"Task '{task.title}' completed successfully",
            }

        except Exception as e:
            logger.error("Task execution failed: %s", e)

            step_funcs = self._step_funcs.get(task.id, {})
            if step_funcs.get('rollback_func'):
                try:
                    await self._run_rollback(task, step_funcs['rollback_func'])
                    logger.info("Rollback completed for task %s", task.id)
                except Exception as rollback_error:
                    logger.error("Rollback failed: %s", rollback_error)

            return {
                'success': False,
                'error': str(e),
                'message': f"Task '{task.title}' failed: {str(e)}",
            }

    async def start_workflow_execution(self, objective: str,
                                      context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Inicia execução completa de workflow baseado em objetivo."""
        task_list = await self.create_workflow_from_objective(objective, context)
        await self.task_manager.activate_task_list(task_list.id)
        loop_task = asyncio.create_task(self._execute_task_list_loop(task_list.id))
        self._running_loops.add(loop_task)
        loop_task.add_done_callback(self._running_loops.discard)
        return {
            'workflow_id': task_list.id,
            'status': 'started',
            'total_steps': task_list.total_tasks,
            'execution_info': {
                'list_id': task_list.id,
                'status': 'started',
                'total_tasks': task_list.total_tasks,
            },
        }

    async def _execute_task_list_loop(self, list_id: str) -> None:
        """Executa tasks da lista sequencialmente respeitando dependências."""
        try:
            while True:
                ready_tasks = await self.task_manager.get_next_tasks(list_id)
                if not ready_tasks:
                    break

                task = ready_tasks[0]
                result = await self.execute_task(task)

                await self.task_manager.mark_task_completed(
                    list_id=list_id,
                    task_id=task.id,
                    success=result['success'],
                    result_data=result.get('data') if isinstance(result.get('data'), dict) else None,
                    error_message=result.get('error'),
                )

                if not result['success']:
                    task_list = await self.task_manager.load_task_list(list_id)
                    if task_list and task_list.stop_on_failure:
                        logger.info(
                            "Stopping workflow %s on step failure (stop_on_failure=True)", list_id
                        )
                        break
        except Exception as exc:
            logger.error("Workflow loop %s aborted due to infrastructure error: %s", list_id, exc)

    async def monitor_workflow_progress(self, workflow_id: str) -> Dict[str, Any]:
        """Monitora progresso de um workflow."""
        status = await self.task_manager.get_task_list_status(workflow_id)
        if not status:
            raise DEILEError(f"Workflow {workflow_id} not found")
        return status

    async def wait_for_workflow_completion(self, workflow_id: str,
                                          timeout: Optional[timedelta] = None) -> Dict[str, Any]:
        """Aguarda conclusão de um workflow."""
        start_time = datetime.now()
        max_wait = timeout or timedelta(hours=1)

        while True:
            status = await self.monitor_workflow_progress(workflow_id)

            if status['is_completed']:
                return {
                    'status': 'completed',
                    'success': not status['has_failures'],
                    'final_stats': status,
                }

            if status['has_failures']:
                return {
                    'status': 'failed',
                    'success': False,
                    'final_stats': status,
                }

            if datetime.now() - start_time > max_wait:
                return {
                    'status': 'timeout',
                    'success': False,
                    'message': f"Workflow timed out after {max_wait}",
                }

            await asyncio.sleep(2)

    # Métodos privados

    async def _analyze_objective_to_steps(self, objective: str,
                                         context: Dict[str, Any]) -> List[WorkflowStep]:
        """Analisa objetivo e converte em steps executáveis."""
        steps = []
        objective_lower = objective.lower()

        if any(w in objective_lower for w in ['file', 'read', 'analyze', 'check']):
            steps.append(WorkflowStep(
                action='read_file',
                params={'path': context.get('target_file', 'README.md')},
                description="Read target file for analysis",
                timeout=30,
            ))

        if any(w in objective_lower for w in ['list', 'files', 'directory', 'explore']):
            steps.append(WorkflowStep(
                action='list_files',
                params={'path': context.get('target_dir', '.'), 'recursive': True},
                description="List files in target directory",
                timeout=60,
            ))

        if any(w in objective_lower for w in ['search', 'find', 'grep', 'pattern']):
            steps.append(WorkflowStep(
                action='find_in_files',
                params={
                    'pattern': context.get('search_pattern', 'TODO'),
                    'path': context.get('search_path', '.'),
                    'max_results': 50,
                },
                description="Search for pattern in files",
                timeout=120,
            ))

        if any(w in objective_lower for w in ['run', 'execute', 'command', 'script']):
            steps.append(WorkflowStep(
                action='bash_execute',
                params={
                    'command': context.get('command', 'echo "Workflow step executed"'),
                    'show_cli': True,
                },
                description="Execute command",
                timeout=300,
            ))

        if any(w in objective_lower for w in ['validate', 'verify', 'check', 'test']):
            steps.append(WorkflowStep(
                action='validation',
                params={'validation_type': 'general'},
                description="Validate workflow results",
                timeout=60,
            ))

        if not steps:
            steps.append(WorkflowStep(
                action='list_files',
                params={'path': '.', 'recursive': False},
                description=f"General analysis step for: {objective}",
                timeout=60,
            ))

        return steps

    async def _add_workflow_step_to_list(self, step: WorkflowStep, list_id: str, index: int) -> Task:
        """Adiciona workflow step como task na lista SQLite, persistindo metadata."""
        depends_on = []
        if index > 0:
            existing_tasks = await self.task_manager._get_tasks_for_list(list_id)
            if existing_tasks:
                depends_on = [existing_tasks[-1].id]

        is_registered_tool = self.tool_registry.get_enabled(step.action) is not None
        action_type = 'tool' if is_registered_tool else step.action

        task_metadata = {
            'list_id': list_id,
            'action_type': action_type,
            'action_name': step.action,
            'params': step.params or {},
            'timeout': step.timeout,
            'retry_count': step.retry_count,
        }

        task = await self.task_manager.add_task_to_list(
            list_id=list_id,
            title=step.description or f"Step {index + 1}: {step.action}",
            description=step.description,
            depends_on=depends_on,
            priority=TaskPriority.MEDIUM,
            estimated_duration=timedelta(seconds=step.timeout),
            metadata=task_metadata,
            tags=['workflow', f'action:{step.action}'],
        )

        # Callables cannot be JSON-serialized to SQLite — store in memory keyed by task.id
        if step.validation is not None or step.rollback is not None:
            self._step_funcs[task.id] = {
                'validation_func': step.validation,
                'rollback_func': step.rollback,
            }

        return task

    async def _execute_tool_action(self, action_name: str, params: Dict[str, Any], task: Task) -> Any:
        """Executa ação usando tool registry."""
        if self.tool_registry.get_enabled(action_name) is None:
            raise DEILEError(f"Tool '{action_name}' not found in registry")

        context = ToolContext(
            user_input=task.description,
            parsed_args=params,
            session_data={},
            working_directory='.',
            file_list=[],
        )

        result = await self.tool_registry.execute_tool(action_name, context)

        if result.status != ToolStatus.SUCCESS:
            raise DEILEError(f"Tool execution failed: {result.message}")

        return result.output

    async def _execute_command_action(self, command: str, params: Dict[str, Any], task: Task) -> Any:
        """Executa comando do sistema via bash_execute tool."""
        return await self._execute_tool_action('bash_execute', {'command': command, **params}, task)

    async def _execute_validation_action(self, validation_type: str, params: Dict[str, Any], task: Task) -> Any:
        """Valida resultado dos steps anteriores do workflow.

        'general': falha se qualquer step anterior do mesmo task_list falhou.
        """
        if validation_type != 'general':
            raise DEILEError(
                f"Unknown validation type: '{validation_type}'. Supported: 'general'"
            )

        list_id = task.metadata.get('list_id')
        if not list_id:
            raise DEILEError("Cannot run general validation: list_id missing from task metadata")

        tasks = await self.task_manager._get_tasks_for_list(list_id)
        failed = [t for t in tasks if t.id != task.id and t.status == TaskStatus.FAILED]
        if failed:
            failed_titles = ', '.join(t.title for t in failed)
            raise DEILEError(
                f"General validation failed: {len(failed)} step(s) failed — {failed_titles}"
            )

        return {'validation_passed': True, 'message': 'All previous steps completed successfully'}

    async def _run_validation(self, task: Task, result: Any, validation_func: Callable) -> Dict[str, Any]:
        """Executa validação do resultado de uma task."""
        try:
            if asyncio.iscoroutinefunction(validation_func):
                validation_result = await validation_func(result)
            else:
                validation_result = validation_func(result)
            return {'success': True, 'result': validation_result}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def _run_rollback(self, task: Task, rollback_func: Callable) -> None:
        """Executa rollback de uma task."""
        if asyncio.iscoroutinefunction(rollback_func):
            await rollback_func(task)
        else:
            rollback_func(task)


# Singleton instance
_workflow_executor: Optional[WorkflowExecutor] = None


def get_workflow_executor() -> WorkflowExecutor:
    """Retorna instância singleton do WorkflowExecutor."""
    global _workflow_executor
    if _workflow_executor is None:
        _workflow_executor = WorkflowExecutor()
    return _workflow_executor
