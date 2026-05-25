"""Enhanced Bash Tool - PTY support, tee, security and artifacts - SITUAÇÃO 4"""

import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# PTY imports for Unix-like systems
try:
    import pty  # noqa: F401
    import select  # noqa: F401
    import tty  # noqa: F401
    PTY_AVAILABLE = True
except ImportError as e:
    logging.warning(f"PTY modules not available: {e}")
    PTY_AVAILABLE = False

from ..config.settings import get_settings
from ..core.exceptions import ToolError
from ..orchestration.artifact_manager import ArtifactManager
from ..security.permissions import PermissionManager
from ._shell_security import assess_risk
from .base import DisplayPolicy, SyncTool, ToolContext, ToolResult, ToolStatus

logger = logging.getLogger(__name__)


class BashExecuteTool(SyncTool):
    """Enhanced bash execution with PTY, tee, security and artifacts"""

    @property
    def name(self) -> str:
        return "bash_execute"

    @property
    def description(self) -> str:
        return (
            "Execute bash commands with PTY support, output tee, security "
            "controls and artifact generation"
        )

    @property
    def category(self) -> str:
        return "execution"

    def __init__(self,
                 permission_manager: Optional[PermissionManager] = None,
                 artifact_manager: Optional[ArtifactManager] = None):
        super().__init__()

        self.permission_manager = permission_manager
        self.artifact_manager = artifact_manager
        self.platform = platform.system()

    def _should_use_pty(self, command: str, force_pty: Optional[bool] = None) -> bool:
        """Determine if PTY should be used"""
        
        if force_pty is not None:
            return force_pty
        
        # Interactive commands that benefit from PTY
        interactive_commands = [
            'vim', 'nano', 'emacs', 'less', 'more', 'top', 'htop', 
            'tmux', 'screen', 'ssh', 'telnet', 'ftp', 'sftp',
            'python -i', 'node', 'irb', 'scala', 'mysql', 'psql'
        ]
        
        command_lower = command.lower()
        return any(cmd in command_lower for cmd in interactive_commands)
    
    def _execute_with_pty_unix(self,
                              command: str,
                              working_dir: Path,
                              env: Dict[str, str],
                              timeout: float) -> Tuple[str, str, int, bool]:
        """Execute command with PTY on Unix systems"""

        try:
            import pty
            import select

            # Create master and slave PTY
            master_fd, slave_fd = pty.openpty()
            process = None
            try:
                # Start process with PTY
                process = subprocess.Popen(
                    command,
                    shell=True,  # nosec B602 — intentional: BashExecuteTool is a shell-execution primitive; command passes security assessment before reaching here
                    cwd=working_dir,
                    env=env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    preexec_fn=os.setsid
                )

                # Close slave fd in parent — child holds the only ref now.
                os.close(slave_fd)
                slave_fd = -1  # marker: don't double-close in finally

                # Read from master with timeout
                output_buffer = []
                start_time = time.time()

                while True:
                    # Check if process is still running
                    poll_result = process.poll()
                    if poll_result is not None:
                        break

                    # Check timeout
                    if time.time() - start_time > timeout:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise TimeoutError(f"Command timed out after {timeout} seconds")

                    # Check for data to read
                    ready, _, _ = select.select([master_fd], [], [], 1.0)
                    if ready:
                        try:
                            data = os.read(master_fd, 1024).decode('utf-8', errors='replace')
                            if data:
                                output_buffer.append(data)
                                # Real-time output (tee functionality)
                                if self._should_show_output():
                                    print(data, end='', flush=True)
                        except OSError:
                            break

                # Wait for process to complete
                exit_code = process.wait()
                output = ''.join(output_buffer)

                return output, "", exit_code, True  # PTY used
            finally:
                # Always close PTY fds and reap the subprocess so we don't
                # leak file descriptors on TimeoutError / unexpected errors.
                # Before this fix, a TimeoutError raised inside the loop
                # escaped the function without closing ``master_fd`` —
                # repeated timeouts exhausted the FD table.
                if slave_fd != -1:
                    try:
                        os.close(slave_fd)
                    except OSError:
                        pass
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                if process is not None and process.poll() is None:
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except Exception:  # noqa: BLE001 - best effort cleanup
                        pass

        except Exception as e:
            logger.error(f"PTY execution failed: {e}")
            # Fallback to regular subprocess
            return self._execute_with_subprocess(command, working_dir, env, timeout)
    
    def _execute_with_subprocess(self,
                               command: str,
                               working_dir: Path,
                               env: Dict[str, str],
                               timeout: float) -> Tuple[str, str, int, bool]:
        """Execute command with regular subprocess (fallback)"""

        if self.platform == 'Windows':
            shell_cmd = ['cmd.exe', '/c', command]
        else:
            shell_cmd = ['/bin/bash', '-c', command]

        try:
            result = subprocess.run(
                shell_cmd,
                cwd=working_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"Command timed out after {timeout} seconds")
        except Exception as e:
            logger.error(f"Subprocess execution failed: {e}")
            raise ToolError(f"Command execution failed: {str(e)}")

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if self._should_show_output():
            if stdout:
                print(stdout, end='', flush=True)
            if stderr:
                print(stderr, end='', file=sys.stderr, flush=True)

        return stdout, stderr, result.returncode, False
    
    def _should_show_output(self) -> bool:
        """Determine if output should be shown in real-time"""
        # This will be controlled by the tool context
        return True  # For now, always show output
    
    def _prepare_environment(self, 
                           base_env: Optional[Dict[str, str]],
                           working_dir: Path) -> Dict[str, str]:
        """Prepare environment variables for execution"""
        
        env = os.environ.copy()
        
        # Add custom environment variables
        if base_env:
            env.update(base_env)
        
        # Ensure PATH includes common directories
        if self.platform == 'Windows':
            common_paths = [
                r'C:\Windows\System32',
                r'C:\Windows',
                r'C:\Program Files\Git\bin',
                r'C:\Program Files\Git\cmd'
            ]
        else:
            common_paths = [
                '/usr/local/bin',
                '/usr/bin', 
                '/bin',
                '/usr/local/sbin',
                '/usr/sbin',
                '/sbin'
            ]
        
        current_path = env.get('PATH', '')
        for path in common_paths:
            if path not in current_path and os.path.exists(path):
                env['PATH'] = f"{path}{os.pathsep}{env['PATH']}"
        
        # Set working directory in environment
        env['PWD'] = str(working_dir)
        
        return env
    
    def _check_permissions(self, command: str, working_dir: Path) -> None:
        """Check if command execution is permitted"""
        
        if not self.permission_manager:
            return  # No permission manager, allow all
        
        # Check command execution permission
        if not self.permission_manager.check_permission(
            tool_name=self.name,
            resource=command,
            action="execute",
            context={"working_directory": str(working_dir)}
        ):
            raise ToolError("Permission denied: Command execution not allowed")
        
        # Check working directory permission
        if not self.permission_manager.check_permission(
            tool_name=self.name,
            resource=str(working_dir),
            action="read",
            context={"resource_type": "directory"}
        ):
            raise ToolError(f"Permission denied: Access to directory {working_dir} not allowed")
    
    def _store_artifact(self,
                       run_id: str,
                       command: str,
                       result: Dict[str, Any],
                       execution_time: float) -> Optional[str]:
        """Store execution artifact"""
        
        if not self.artifact_manager:
            return None
        
        try:
            input_data = {
                "command": command,
                "working_directory": result.get("working_directory"),
                "platform": self.platform,
                "timestamp": time.time()
            }
            
            output_data = {
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "exit_code": result.get("exit_code", -1),
                "pty_used": result.get("pty_used", False),
                "security_warnings": result.get("security_warnings", []),
                "execution_time": execution_time
            }
            
            artifact_path = self.artifact_manager.store_artifact(
                run_id=run_id,
                tool_name=self.name,
                input_data=input_data,
                output_data=output_data,
                execution_time=execution_time,
                status="success" if result.get("exit_code") == 0 else "error"
            )
            
            return artifact_path
            
        except Exception as e:
            logger.error(f"Failed to store bash artifact: {e}")
            return None
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute bash command with PTY support and security"""
        
        start_time = time.time()
        
        try:
            # Extract parameters
            args = context.parsed_args
            command = args.get("command")
            working_directory = args.get("working_directory") or context.working_directory or "."
            timeout = args.get("timeout", 60.0)
            use_pty = args.get("use_pty")
            sandbox = args.get("sandbox", False)
            show_cli = args.get("show_cli", True)
            capture_output = args.get("capture_output", True)
            environment = args.get("environment") or {}
            security_level = args.get("security_level", "moderate")
            
            if not command or not command.strip():
                raise ToolError("Command cannot be empty")

            # Enforce sandbox_code_execution profile setting (issue #138)
            if get_settings().sandbox_code_execution:
                sandbox = True

            # Prepare working directory
            working_dir = Path(working_directory).resolve()
            if not working_dir.exists():
                raise ToolError(f"Working directory does not exist: {working_directory}")
            
            # Security assessment
            risk_level, security_warnings = assess_risk(command)
            
            # Check if risk level is acceptable
            risk_hierarchy = ["safe", "moderate", "dangerous"]
            requested_level_idx = risk_hierarchy.index(security_level)
            actual_level_idx = risk_hierarchy.index(risk_level)
            
            if actual_level_idx > requested_level_idx:
                raise ToolError(
                    f"Command risk level ({risk_level}) exceeds requested level ({security_level}). "
                    f"Security warnings: {security_warnings}"
                )
            
            # Check permissions
            self._check_permissions(command, working_dir)
            
            # Prepare environment
            env = self._prepare_environment(environment, working_dir)
            
            # Determine PTY usage
            should_use_pty = self._should_use_pty(command, use_pty)
            
            # Execute command
            if should_use_pty and not sandbox and self.platform != 'Windows':
                stdout, stderr, exit_code, pty_used = self._execute_with_pty_unix(
                    command, working_dir, env, timeout
                )
            else:
                stdout, stderr, exit_code, pty_used = self._execute_with_subprocess(
                    command, working_dir, env, timeout
                )
            
            # Prepare result data
            execution_time = time.time() - start_time
            
            result_data = {
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "execution_time": execution_time,
                "pty_used": pty_used,
                "sandbox_used": sandbox,
                "security_warnings": security_warnings,
                "truncated": False,
                "working_directory": str(working_dir),
                "command": command
            }
            
            # Store artifact
            run_id = (context.metadata or {}).get("run_id", f"bash_{int(time.time())}")
            artifact_path = None
            
            if capture_output:
                artifact_path = self._store_artifact(run_id, command, result_data, execution_time)
                result_data["artifact_path"] = artifact_path
            
            # Prepare display data
            display_data = {
                "command": command,
                "exit_code": exit_code,
                "execution_time": round(execution_time, 2),
                "pty_used": pty_used,
                "security_level": risk_level,
                "warnings": security_warnings
            }
            
            # Determine status
            if exit_code == 0:
                status = ToolStatus.SUCCESS
                message = "Command executed successfully (exit code: 0)"
            else:
                status = ToolStatus.ERROR
                message = f"Command failed with exit code: {exit_code}"
            
            if security_warnings:
                message += f" | Security warnings: {len(security_warnings)}"
            
            return ToolResult(
                status=status,
                data=result_data,
                message=message,
                display_policy=DisplayPolicy.SYSTEM,
                show_cli=show_cli,
                artifact_path=artifact_path,
                display_data=display_data,
                execution_time=execution_time
            )
            
        except Exception as e:
            logger.error(f"Bash execution error: {e}")
            
            execution_time = time.time() - start_time
            
            return ToolResult.error_result(
                message=f"Bash execution failed: {str(e)}",
                error=e,
                display_policy=DisplayPolicy.SYSTEM,
                execution_time=execution_time
            )