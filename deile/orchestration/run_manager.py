"""
Run Manager for DEILE v4.0
==========================

Manages execution runs with real-time monitoring, artifact generation
and comprehensive logging for autonomous plan execution.

Author: DEILE
Version: 4.0
"""

import logging
import json
import uuid
import time
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional, AsyncIterator, Union
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

class RunStatus(Enum):
    """Status of execution run"""
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"
    TIMEOUT = "timeout"

@dataclass
class StepExecutionResult:
    """Result of individual step execution"""
    step_id: str
    status: str
    started_at: float
    completed_at: float
    duration: float
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    artifact_path: Optional[str] = None
    exit_code: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class RunManifest:
    """Execution run manifest with complete execution details"""
    run_id: str
    plan_id: str
    started_at: float
    status: RunStatus = RunStatus.CREATED
    current_step: int = 0
    completed_at: Optional[float] = None
    
    # Progress tracking
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    
    # Execution details
    step_results: List[StepExecutionResult] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    cost_estimate: float = 0.0
    actual_cost: float = 0.0
    
    # Configuration
    dry_run: bool = False
    auto_approve: bool = False
    step_range: Optional[str] = None
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def duration(self) -> Optional[float]:
        """Calculate total execution duration"""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage"""
        if self.total_steps == 0:
            return 0.0
        return (self.completed_steps / self.total_steps) * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        data = asdict(self)
        data['status'] = self.status.value
        data['step_results'] = [result.to_dict() for result in self.step_results]
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RunManifest':
        """Create from dictionary"""
        data['status'] = RunStatus(data['status'])
        
        step_results = data.get('step_results', [])
        data['step_results'] = [StepExecutionResult(**result) for result in step_results]
        
        return cls(**data)

class RunManager:
    """Execution run management with real-time monitoring"""
    
    def __init__(self, runs_dir: Path = None, artifacts_dir: Path = None):
        """Initialize run manager"""
        self.runs_dir = runs_dir or Path("RUNS")
        self.artifacts_dir = artifacts_dir or Path("ARTIFACTS")
        self.runs_dir.mkdir(exist_ok=True)
        self.artifacts_dir.mkdir(exist_ok=True)
        
        # Active runs tracking
        self.active_runs: Dict[str, RunManifest] = {}
        self.run_locks: Dict[str, asyncio.Lock] = {}
        self.stop_flags: Dict[str, bool] = {}
        
        # Event handlers for monitoring
        self.event_handlers: Dict[str, List[callable]] = {
            'run_started': [],
            'step_started': [],
            'step_completed': [],
            'step_failed': [],
            'run_completed': [],
            'run_failed': []
        }
    
    def add_event_handler(self, event: str, handler: callable) -> None:
        """Add event handler for run monitoring"""
        if event in self.event_handlers:
            self.event_handlers[event].append(handler)
    
    async def _emit_event(self, event: str, **kwargs):
        """Emit event to all registered handlers"""
        handlers = self.event_handlers.get(event, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(**kwargs)
                else:
                    handler(**kwargs)
            except Exception as e:
                logger.warning(f"Event handler error for {event}: {e}")
    
    async def execute_plan(self, 
                          plan_id: str,
                          plan_steps: List[Dict[str, Any]],
                          dry_run: bool = False,
                          auto_approve: bool = False,
                          step_range: Optional[str] = None) -> AsyncIterator[RunManifest]:
        """Execute plan with real-time status updates"""
        
        run_id = f"RUN_{int(time.time())}_{str(uuid.uuid4())[:8]}"
        
        # Create run manifest
        manifest = RunManifest(
            run_id=run_id,
            plan_id=plan_id,
            started_at=time.time(),
            total_steps=len(plan_steps),
            dry_run=dry_run,
            auto_approve=auto_approve,
            step_range=step_range
        )
        
        # Setup run tracking
        self.active_runs[run_id] = manifest
        self.run_locks[run_id] = asyncio.Lock()
        self.stop_flags[run_id] = False
        
        try:
            logger.info(f"Starting execution run {run_id} for plan {plan_id}")
            
            # Emit run started event
            await self._emit_event('run_started', run_id=run_id, manifest=manifest)
            
            manifest.status = RunStatus.RUNNING
            yield manifest
            
            # Filter steps by range if specified
            filtered_steps = self._filter_steps_by_range(plan_steps, step_range)
            manifest.total_steps = len(filtered_steps)
            
            # Execute steps
            for i, step_data in enumerate(filtered_steps):
                # Check stop flag
                if self.stop_flags.get(run_id, False):
                    manifest.status = RunStatus.ABORTED
                    break
                
                manifest.current_step = i + 1
                
                try:
                    # Execute step
                    step_result = await self.execute_step(
                        step_data, 
                        manifest, 
                        dry_run=dry_run,
                        auto_approve=auto_approve
                    )
                    
                    # Update manifest
                    manifest.step_results.append(step_result)
                    
                    if step_result.success:
                        manifest.completed_steps += 1
                    else:
                        manifest.failed_steps += 1
                        
                        # Stop on failure if not configured otherwise
                        if not manifest.metadata.get('continue_on_failure', False):
                            break
                    
                    # Add artifacts
                    if step_result.artifact_path:
                        manifest.artifacts.append(step_result.artifact_path)
                    
                    # Save progress
                    await self._save_manifest(manifest)
                    
                    # Yield updated manifest
                    yield manifest
                    
                except Exception as e:
                    logger.error(f"Error executing step {i+1}: {e}")
                    
                    # Create error result
                    error_result = StepExecutionResult(
                        step_id=step_data.get('id', f'step_{i+1}'),
                        status="failed",
                        started_at=time.time(),
                        completed_at=time.time(),
                        duration=0.0,
                        success=False,
                        error=str(e)
                    )
                    
                    manifest.step_results.append(error_result)
                    manifest.failed_steps += 1
                    
                    yield manifest
                    
                    # Stop on error if not configured otherwise
                    if not manifest.metadata.get('continue_on_failure', False):
                        break
            
            # Finalize run
            manifest.completed_at = time.time()
            
            if manifest.failed_steps == 0 and not self.stop_flags.get(run_id, False):
                manifest.status = RunStatus.SUCCESS
                await self._emit_event('run_completed', run_id=run_id, manifest=manifest)
            else:
                manifest.status = RunStatus.FAILED if manifest.failed_steps > 0 else RunStatus.ABORTED
                await self._emit_event('run_failed', run_id=run_id, manifest=manifest)
            
            # Final save
            await self._save_manifest(manifest)
            
            logger.info(f"Completed execution run {run_id}: {manifest.status.value}")
            yield manifest
            
        except Exception as e:
            manifest.status = RunStatus.FAILED
            manifest.completed_at = time.time()
            await self._save_manifest(manifest)
            
            logger.error(f"Run {run_id} failed: {e}")
            raise
            
        finally:
            # Cleanup
            self.active_runs.pop(run_id, None)
            self.run_locks.pop(run_id, None)
            self.stop_flags.pop(run_id, None)
    
    async def execute_step(self,
                          step_data: Dict[str, Any],
                          manifest: RunManifest,
                          dry_run: bool = False,
                          auto_approve: bool = False) -> StepExecutionResult:
        """Execute single plan step"""
        
        step_id = step_data.get('id', 'unknown_step')
        tool_name = step_data.get('tool_name', 'unknown_tool')
        params = step_data.get('parameters', {})
        
        logger.info(f"Executing step {step_id}: {tool_name}")
        
        start_time = time.time()
        
        # Emit step started event
        await self._emit_event('step_started', 
                              run_id=manifest.run_id, 
                              step_id=step_id, 
                              tool_name=tool_name)
        
        try:
            # Check if step requires approval
            requires_approval = step_data.get('requires_approval', False)
            risk_level = step_data.get('risk_level', 'low')
            
            if requires_approval and not auto_approve and risk_level not in ['low']:
                # Handle approval requirement
                approval_result = await self._handle_approval_requirement(
                    step_data, manifest
                )
                
                if not approval_result:
                    # Approval denied or timed out
                    end_time = time.time()
                    result = StepExecutionResult(
                        step_id=step_id,
                        status="skipped",
                        started_at=start_time,
                        completed_at=end_time,
                        duration=end_time - start_time,
                        success=False,
                        error="Approval required but not granted"
                    )
                    manifest.skipped_steps += 1
                    return result
            
            # Execute step based on dry run setting
            if dry_run:
                result = await self._simulate_step_execution(step_data)
            else:
                result = await self._execute_step_real(step_data, manifest)
            
            # Emit completion event
            await self._emit_event('step_completed' if result.success else 'step_failed',
                                  run_id=manifest.run_id, 
                                  step_id=step_id, 
                                  result=result)
            
            return result
            
        except Exception as e:
            end_time = time.time()
            
            result = StepExecutionResult(
                step_id=step_id,
                status="failed",
                started_at=start_time,
                completed_at=end_time,
                duration=end_time - start_time,
                success=False,
                error=str(e)
            )
            
            await self._emit_event('step_failed',
                                  run_id=manifest.run_id,
                                  step_id=step_id,
                                  result=result)
            
            return result
    
    def _filter_steps_by_range(self, 
                              steps: List[Dict[str, Any]], 
                              step_range: Optional[str]) -> List[Dict[str, Any]]:
        """Filter steps by range specification"""
        if not step_range:
            return steps
        
        try:
            if '-' in step_range:
                # Range format: "1-5"
                start, end = map(int, step_range.split('-'))
                return steps[start-1:end]
            else:
                # Single step: "3"
                index = int(step_range) - 1
                return [steps[index]] if 0 <= index < len(steps) else []
        except (ValueError, IndexError):
            logger.warning(f"Invalid step range: {step_range}, using all steps")
            return steps
    
    async def _handle_approval_requirement(self, 
                                         step_data: Dict[str, Any],
                                         manifest: RunManifest) -> bool:
        """Handle step that requires approval"""
        
        step_id = step_data.get('id', 'unknown_step')
        description = step_data.get('description', 'No description')
        
        logger.info(f"Step {step_id} requires approval: {description}")
        
        # In real implementation, this would integrate with approval system
        # For now, we'll simulate based on risk level
        risk_level = step_data.get('risk_level', 'low')
        
        if risk_level in ['low']:
            # Auto-approve low risk operations
            return True
        else:
            # For high risk operations, would wait for user approval
            # This is a placeholder - real implementation would use ApprovalSystem
            logger.warning(f"High risk step {step_id} requires manual approval")
            return False
    
    async def _simulate_step_execution(self, 
                                     step_data: Dict[str, Any]) -> StepExecutionResult:
        """Simulate step execution for dry run"""
        
        step_id = step_data.get('id', 'unknown_step')
        tool_name = step_data.get('tool_name', 'unknown_tool')
        
        # Simulate execution time
        await asyncio.sleep(0.1)
        
        start_time = time.time()
        end_time = start_time + 0.1
        
        return StepExecutionResult(
            step_id=step_id,
            status="simulated",
            started_at=start_time,
            completed_at=end_time,
            duration=0.1,
            success=True,
            output=f"[DRY RUN] Would execute {tool_name} with parameters: {step_data.get('parameters', {})}"
        )
    
    async def _execute_step_real(self, 
                               step_data: Dict[str, Any],
                               manifest: RunManifest) -> StepExecutionResult:
        """Execute step with real tool execution"""
        
        step_id = step_data.get('id', 'unknown_step')
        tool_name = step_data.get('tool_name', 'unknown_tool')
        params = step_data.get('parameters', {})
        timeout = step_data.get('timeout', 300)
        
        start_time = time.time()
        
        try:
            # Import here to avoid circular imports
            from ..tools.registry import get_tool_registry
            
            tool_registry = get_tool_registry()
            
            # Execute tool with timeout
            result = await asyncio.wait_for(
                tool_registry.execute_function_call(tool_name, params),
                timeout=timeout
            )
            
            end_time = time.time()
            
            # Generate artifact if tool produced output
            artifact_path = None
            if result.output and len(str(result.output)) > 100:
                artifact_path = await self._generate_step_artifact(
                    step_id, tool_name, result, manifest.run_id
                )
            
            return StepExecutionResult(
                step_id=step_id,
                status="completed" if result.success else "failed",
                started_at=start_time,
                completed_at=end_time,
                duration=end_time - start_time,
                success=result.success,
                output=str(result.output) if result.output else None,
                error=result.error_message if hasattr(result, 'error_message') else None,
                artifact_path=artifact_path,
                exit_code=getattr(result, 'exit_code', None)
            )
            
        except asyncio.TimeoutError:
            end_time = time.time()
            return StepExecutionResult(
                step_id=step_id,
                status="timeout",
                started_at=start_time,
                completed_at=end_time,
                duration=end_time - start_time,
                success=False,
                error=f"Step timed out after {timeout} seconds"
            )
        
        except Exception as e:
            end_time = time.time()
            return StepExecutionResult(
                step_id=step_id,
                status="failed",
                started_at=start_time,
                completed_at=end_time,
                duration=end_time - start_time,
                success=False,
                error=str(e)
            )
    
    async def _generate_step_artifact(self,
                                    step_id: str,
                                    tool_name: str,
                                    result: Any,
                                    run_id: str) -> str:
        """Generate artifact file for step execution"""
        
        # Create run-specific artifact directory
        run_artifacts_dir = self.artifacts_dir / run_id
        run_artifacts_dir.mkdir(exist_ok=True)
        
        # Generate artifact filename
        timestamp = int(time.time())
        artifact_filename = f"{step_id}_{tool_name}_{timestamp}.json"
        artifact_path = run_artifacts_dir / artifact_filename
        
        # Prepare artifact data
        artifact_data = {
            "step_id": step_id,
            "tool_name": tool_name,
            "timestamp": timestamp,
            "success": result.success,
            "output": str(result.output) if result.output else None,
            "metadata": {
                "run_id": run_id,
                "execution_time": time.time(),
                "output_size": len(str(result.output)) if result.output else 0
            }
        }
        
        # Add tool-specific data
        if hasattr(result, 'exit_code'):
            artifact_data["exit_code"] = result.exit_code
        if hasattr(result, 'error_message'):
            artifact_data["error_message"] = result.error_message
        
        try:
            with open(artifact_path, 'w', encoding='utf-8') as f:
                json.dump(artifact_data, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Generated artifact: {artifact_path}")
            return str(artifact_path)
            
        except Exception as e:
            logger.error(f"Failed to generate artifact for step {step_id}: {e}")
            return None
    
    async def pause_execution(self, run_id: str) -> bool:
        """Pause plan execution"""
        if run_id not in self.active_runs:
            return False
        
        # Set stop flag - execution will pause at next step
        self.stop_flags[run_id] = True
        
        manifest = self.active_runs[run_id]
        manifest.status = RunStatus.PAUSED
        await self._save_manifest(manifest)
        
        logger.info(f"Paused execution of run {run_id}")
        return True
    
    async def resume_execution(self, run_id: str) -> bool:
        """Resume paused execution"""
        if run_id not in self.active_runs:
            return False
        
        # Clear stop flag
        self.stop_flags[run_id] = False
        
        manifest = self.active_runs[run_id]
        manifest.status = RunStatus.RUNNING
        await self._save_manifest(manifest)
        
        logger.info(f"Resumed execution of run {run_id}")
        return True
    
    async def stop_execution(self, run_id: str) -> bool:
        """Stop plan execution completely"""
        if run_id not in self.active_runs:
            return False
        
        # Set stop flag
        self.stop_flags[run_id] = True
        
        manifest = self.active_runs[run_id]
        manifest.status = RunStatus.ABORTED
        manifest.completed_at = time.time()
        await self._save_manifest(manifest)
        
        logger.info(f"Stopped execution of run {run_id}")
        return True
    
    async def get_run_status(self, run_id: str) -> Optional[RunManifest]:
        """Get current status of execution run"""
        # Check active runs first
        if run_id in self.active_runs:
            return self.active_runs[run_id]
        
        # Load from storage
        return await self.load_manifest(run_id)
    
    async def list_runs(self, 
                       plan_id: Optional[str] = None,
                       status_filter: Optional[RunStatus] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        """List execution runs with optional filtering"""
        
        runs = []
        
        # Get from active runs
        for manifest in self.active_runs.values():
            if plan_id and manifest.plan_id != plan_id:
                continue
            if status_filter and manifest.status != status_filter:
                continue
            
            runs.append({
                "run_id": manifest.run_id,
                "plan_id": manifest.plan_id,
                "status": manifest.status.value,
                "started_at": manifest.started_at,
                "completed_at": manifest.completed_at,
                "progress": f"{manifest.completed_steps}/{manifest.total_steps}",
                "success_rate": manifest.success_rate,
                "duration": manifest.duration
            })
        
        # Get from storage
        for manifest_file in self.runs_dir.glob("*.json"):
            if len(runs) >= limit:
                break
                
            try:
                with open(manifest_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if plan_id and data.get('plan_id') != plan_id:
                    continue
                if status_filter and data.get('status') != status_filter.value:
                    continue
                
                # Skip if already in active runs
                if data.get('run_id') in self.active_runs:
                    continue
                
                runs.append({
                    "run_id": data.get("run_id"),
                    "plan_id": data.get("plan_id"),
                    "status": data.get("status"),
                    "started_at": data.get("started_at"),
                    "completed_at": data.get("completed_at"),
                    "progress": f"{data.get('completed_steps', 0)}/{data.get('total_steps', 0)}",
                    "success_rate": data.get('completed_steps', 0) / max(data.get('total_steps', 1), 1) * 100,
                    "duration": (data.get("completed_at", 0) - data.get("started_at", 0)) if data.get("completed_at") else None
                })
                
            except Exception as e:
                logger.warning(f"Failed to read run manifest {manifest_file}: {e}")
        
        # Sort by start time (most recent first)
        runs.sort(key=lambda x: x["started_at"], reverse=True)
        
        return runs[:limit]
    
    async def _save_manifest(self, manifest: RunManifest) -> None:
        """Save run manifest to storage"""
        
        manifest_file = self.runs_dir / f"{manifest.run_id}.json"
        
        try:
            with open(manifest_file, 'w', encoding='utf-8') as f:
                json.dump(manifest.to_dict(), f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.error(f"Failed to save run manifest {manifest.run_id}: {e}")
            raise
    
    async def load_manifest(self, run_id: str) -> Optional[RunManifest]:
        """Load run manifest from storage"""
        
        manifest_file = self.runs_dir / f"{run_id}.json"
        if not manifest_file.exists():
            return None
        
        try:
            with open(manifest_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return RunManifest.from_dict(data)
            
        except Exception as e:
            logger.error(f"Failed to load run manifest {run_id}: {e}")
            return None
    
    async def delete_run(self, run_id: str) -> bool:
        """Delete run manifest and artifacts"""
        
        try:
            # Remove manifest
            manifest_file = self.runs_dir / f"{run_id}.json"
            if manifest_file.exists():
                manifest_file.unlink()
            
            # Remove artifacts directory
            artifacts_dir = self.artifacts_dir / run_id
            if artifacts_dir.exists():
                import shutil
                shutil.rmtree(artifacts_dir)
            
            # Remove from active runs if present
            self.active_runs.pop(run_id, None)
            self.run_locks.pop(run_id, None)
            self.stop_flags.pop(run_id, None)
            
            logger.info(f"Deleted run {run_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete run {run_id}: {e}")
            return False


# Global instance
_run_manager: Optional[RunManager] = None

def get_run_manager() -> RunManager:
    """Get singleton instance of RunManager"""
    global _run_manager
    if _run_manager is None:
        _run_manager = RunManager()
    return _run_manager