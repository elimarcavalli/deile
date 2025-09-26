"""Enhanced Execution Tools for DEILE v4.0 - Advanced PTY Support"""

import asyncio
import logging
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable, Union

if platform.system() == "Windows":
    WINDOWS_PTY_AVAILABLE = False
    import msvcrt
    import ctypes
    from ctypes import wintypes
else:
    try:
        import pty
        import select
        UNIX_PTY_AVAILABLE = True
    except ImportError:
        UNIX_PTY_AVAILABLE = False

from .base import SyncTool, AsyncTool, ToolContext, ToolResult, ToolStatus
from ..core.exceptions import ToolError, ValidationError

logger = logging.getLogger(__name__)


class PTYSession:
    """Cross-platform PTY (Pseudo-Terminal) session manager"""
    
    def __init__(self, command: str, working_dir: str = None, env: Dict[str, str] = None):
        self.command = command
        self.working_dir = working_dir or os.getcwd()
        self.env = {**os.environ, **(env or {})}
        self.process = None
        self.master_fd = None
        self.slave_fd = None
        self.thread = None
        self.output_buffer = []
        self.error_buffer = []
        self.is_running = False
        self.exit_code = None
        
        # Windows-specific
        self.pty_process = None
        
    def start(self) -> bool:
        """Start PTY session"""
        try:
            if platform.system() == "Windows":
                return self._start_windows()
            else:
                return self._start_unix()
        except Exception as e:
            logger.error(f"Failed to start PTY session: {e}")
            return False
    
    def _start_windows(self) -> bool:
        """Start PTY session on Windows"""
        # Use standard subprocess for Windows compatibility
        if WINDOWS_PTY_AVAILABLE:
            logger.info("Using standard subprocess on Windows")
        
        # Fallback to ConPTY or basic subprocess
        try:
            # Try using ConPTY if available (Windows 10 1903+)
            if hasattr(subprocess, 'STARTUPINFO'):
                startupinfo = subprocess.STARTUPINFO()
                if hasattr(startupinfo, 'dwFlags'):
                    startupinfo.dwFlags |= getattr(subprocess, 'STARTF_USESTDHANDLES', 0)
                    
            self.process = subprocess.Popen(
                self.command,
                shell=True,
                cwd=self.working_dir,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0,
                startupinfo=startupinfo if 'startupinfo' in locals() else None
            )
            self.is_running = True
            self.thread = threading.Thread(target=self._read_output_subprocess)
            self.thread.daemon = True
            self.thread.start()
            return True
            
        except Exception as e:
            logger.error(f"Failed to start Windows PTY session: {e}")
            return False
    
    def _start_unix(self) -> bool:
        """Start PTY session on Unix-like systems"""
        if not UNIX_PTY_AVAILABLE:
            logger.warning("Unix PTY not available on this system")
            return False
            
        try:
            self.master_fd, self.slave_fd = pty.openpty()
            
            self.process = subprocess.Popen(
                self.command,
                shell=True,
                cwd=self.working_dir,
                env=self.env,
                stdin=self.slave_fd,
                stdout=self.slave_fd,
                stderr=self.slave_fd,
                preexec_fn=os.setsid
            )
            
            os.close(self.slave_fd)  # Close slave fd in parent process
            self.is_running = True
            
            # Start output reading thread
            self.thread = threading.Thread(target=self._read_output_unix)
            self.thread.daemon = True
            self.thread.start()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start Unix PTY session: {e}")
            return False
    
    def _read_output_windows(self):
        """Read output from Windows PTY"""
        try:
            if self.pty_process:
                while self.is_running:
                    try:
                        output = self.pty_process.read(timeout=100)  # 100ms timeout
                        if output:
                            self.output_buffer.append(output)
                    except Exception:
                        break
                    except Exception as e:
                        if self.is_running:
                            logger.error(f"Error reading PTY output: {e}")
                        break
        except Exception as e:
            logger.error(f"PTY output thread error: {e}")
        finally:
            self._cleanup_windows()
    
    def _read_output_subprocess(self):
        """Read output from subprocess (Windows fallback)"""
        try:
            while self.is_running and self.process and self.process.poll() is None:
                try:
                    # Read stdout
                    if self.process.stdout:
                        output = self.process.stdout.readline()
                        if output:
                            self.output_buffer.append(output)
                    
                    # Read stderr
                    if self.process.stderr:
                        error = self.process.stderr.readline()
                        if error:
                            self.error_buffer.append(error)
                            
                except Exception as e:
                    if self.is_running:
                        logger.error(f"Error reading subprocess output: {e}")
                    break
                    
        except Exception as e:
            logger.error(f"Subprocess output thread error: {e}")
        finally:
            if self.process:
                self.exit_code = self.process.returncode
    
    def _read_output_unix(self):
        """Read output from Unix PTY"""
        if not UNIX_PTY_AVAILABLE:
            return
            
        try:
            while self.is_running:
                try:
                    ready, _, _ = select.select([self.master_fd], [], [], 0.1)  # 100ms timeout
                    if ready:
                        try:
                            output = os.read(self.master_fd, 1024).decode('utf-8', errors='ignore')
                            if output:
                                self.output_buffer.append(output)
                        except OSError:
                            break
                except Exception as e:
                    if self.is_running:
                        logger.error(f"Error in select/read: {e}")
                    break
                    
        except Exception as e:
            logger.error(f"PTY output thread error: {e}")
        finally:
            self._cleanup_unix()
    
    def write_input(self, data: str) -> bool:
        """Write input to PTY"""
        try:
            if platform.system() == "Windows":
                if self.pty_process:
                    self.pty_process.write(data)
                    return True
                elif self.process and self.process.stdin:
                    self.process.stdin.write(data)
                    self.process.stdin.flush()
                    return True
            else:
                if self.master_fd:
                    os.write(self.master_fd, data.encode('utf-8'))
                    return True
                    
        except Exception as e:
            logger.error(f"Failed to write input to PTY: {e}")
        
        return False
    
    def read_output(self, timeout: float = 0.1) -> str:
        """Read accumulated output"""
        time.sleep(timeout)  # Allow time for output to accumulate
        
        if self.output_buffer:
            output = ''.join(self.output_buffer)
            self.output_buffer.clear()
            return output
        
        return ""
    
    def read_errors(self) -> str:
        """Read accumulated errors"""
        if self.error_buffer:
            errors = ''.join(self.error_buffer)
            self.error_buffer.clear()
            return errors
        
        return ""
    
    def send_signal(self, sig: int) -> bool:
        """Send signal to process"""
        try:
            if platform.system() == "Windows":
                if self.process:
                    if sig == signal.SIGTERM:
                        self.process.terminate()
                    elif sig == signal.SIGKILL:
                        self.process.kill()
                    return True
            else:
                if self.process:
                    os.killpg(os.getpgid(self.process.pid), sig)
                    return True
                    
        except Exception as e:
            logger.error(f"Failed to send signal {sig}: {e}")
        
        return False
    
    def is_alive(self) -> bool:
        """Check if process is still running"""
        if self.process:
            return self.process.poll() is None
        return False
    
    def get_exit_code(self) -> Optional[int]:
        """Get process exit code"""
        if self.process:
            return self.process.returncode
        return self.exit_code
    
    def terminate(self, timeout: float = 5.0) -> bool:
        """Terminate PTY session gracefully"""
        if not self.is_running:
            return True
            
        self.is_running = False
        
        try:
            # Send SIGTERM first
            if self.send_signal(signal.SIGTERM):
                # Wait for graceful shutdown
                start_time = time.time()
                while self.is_alive() and (time.time() - start_time) < timeout:
                    time.sleep(0.1)
                
                # Force kill if still running
                if self.is_alive():
                    self.send_signal(signal.SIGKILL)
                    time.sleep(0.5)
            
            return not self.is_alive()
            
        except Exception as e:
            logger.error(f"Error terminating PTY session: {e}")
            return False
        finally:
            self._cleanup()
    
    def _cleanup_windows(self):
        """Cleanup Windows PTY resources"""
        try:
            if self.pty_process:
                self.pty_process.close()
                self.pty_process = None
        except Exception as e:
            logger.error(f"Error cleaning up Windows PTY: {e}")
    
    def _cleanup_unix(self):
        """Cleanup Unix PTY resources"""
        try:
            if self.master_fd:
                os.close(self.master_fd)
                self.master_fd = None
        except Exception as e:
            logger.error(f"Error cleaning up Unix PTY: {e}")
    
    def _cleanup(self):
        """General cleanup"""
        if platform.system() == "Windows":
            self._cleanup_windows()
        else:
            self._cleanup_unix()
            
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)


class EnhancedExecutionTool(SyncTool):
    """Enhanced execution tool with advanced PTY support and interactive capabilities"""
    
    @property
    def name(self) -> str:
        return "execute_command_enhanced"
    
    @property
    def description(self) -> str:
        return "Enhanced command execution with PTY support, interactive capabilities, and advanced process management"
    
    @property
    def category(self) -> str:
        return "execution"
    
    def __init__(self):
        super().__init__()
        self.active_sessions = {}  # Track active PTY sessions
        self.session_counter = 0
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute command with enhanced PTY support"""
        command = context.parsed_args.get("command")
        interactive = context.parsed_args.get("interactive", False)
        pty_mode = context.parsed_args.get("pty", False)
        timeout = context.parsed_args.get("timeout", 30)
        env_vars = context.parsed_args.get("env", {})
        input_data = context.parsed_args.get("input", "")
        
        if not command:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="No command provided",
                error=ValidationError("command is required")
            )
        
        # Security checks
        if not self._is_command_safe(command):
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Potentially dangerous command blocked: {command}",
                error=PermissionError("Command blocked by security policy")
            )
        
        try:
            if interactive or pty_mode:
                return self._execute_interactive(command, context, timeout, env_vars, input_data)
            else:
                return self._execute_standard(command, context, timeout, env_vars)
                
        except Exception as e:
            logger.error(f"Enhanced execution error: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Execution failed: {str(e)}",
                error=e
            )
    
    def _execute_interactive(self, command: str, context: ToolContext, 
                           timeout: int, env_vars: Dict[str, str], input_data: str) -> ToolResult:
        """Execute command in interactive PTY mode"""
        session_id = f"session_{self.session_counter}"
        self.session_counter += 1
        
        try:
            # Create PTY session
            pty_session = PTYSession(
                command=command,
                working_dir=context.working_directory,
                env=env_vars
            )
            
            if not pty_session.start():
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message="Failed to start PTY session",
                    error=RuntimeError("PTY initialization failed")
                )
            
            self.active_sessions[session_id] = pty_session
            
            # Send initial input if provided
            if input_data:
                pty_session.write_input(input_data)
            
            # Wait for initial output or timeout
            start_time = time.time()
            output_parts = []
            
            while (time.time() - start_time) < timeout:
                output = pty_session.read_output(0.1)
                if output:
                    output_parts.append(output)
                
                if not pty_session.is_alive():
                    break
                    
                time.sleep(0.1)
            
            # Get final state
            final_output = ''.join(output_parts)
            error_output = pty_session.read_errors()
            exit_code = pty_session.get_exit_code()
            is_still_running = pty_session.is_alive()
            
            # If process is still running, keep session active
            if is_still_running:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    data=final_output,
                    message=f"Interactive session started (ID: {session_id}). Process is still running.",
                    metadata={
                        "session_id": session_id,
                        "command": command,
                        "running": True,
                        "output": final_output,
                        "stderr": error_output,
                        "pty_mode": True,
                        "platform": platform.system()
                    }
                )
            else:
                # Process completed, cleanup session
                pty_session.terminate()
                self.active_sessions.pop(session_id, None)
                
                status = ToolStatus.SUCCESS if (exit_code == 0) else ToolStatus.ERROR
                return ToolResult(
                    status=status,
                    data=final_output,
                    message=f"Command completed with exit code {exit_code}",
                    metadata={
                        "command": command,
                        "exit_code": exit_code,
                        "stdout": final_output,
                        "stderr": error_output,
                        "pty_mode": True,
                        "execution_time": time.time() - start_time
                    }
                )
                
        except Exception as e:
            # Cleanup on error
            if session_id in self.active_sessions:
                try:
                    self.active_sessions[session_id].terminate()
                    del self.active_sessions[session_id]
                except:
                    pass
            
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Interactive execution failed: {str(e)}",
                error=e
            )
    
    def _execute_standard(self, command: str, context: ToolContext, 
                         timeout: int, env_vars: Dict[str, str]) -> ToolResult:
        """Execute command in standard subprocess mode"""
        try:
            # Prepare environment
            full_env = {**os.environ, **env_vars}
            
            # Execute with timeout
            result = subprocess.run(
                command,
                shell=True,
                cwd=context.working_directory,
                env=full_env,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            output = result.stdout.strip() if result.stdout else ""
            error_output = result.stderr.strip() if result.stderr else ""
            
            status = ToolStatus.SUCCESS if result.returncode == 0 else ToolStatus.ERROR
            message = f"Command executed with exit code {result.returncode}"
            
            return ToolResult(
                status=status,
                data=output,
                message=message,
                metadata={
                    "command": command,
                    "exit_code": result.returncode,
                    "stdout": output,
                    "stderr": error_output,
                    "pty_mode": False,
                    "working_directory": context.working_directory
                }
            )
            
        except subprocess.TimeoutExpired:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Command timed out after {timeout} seconds",
                error=TimeoutError(f"Command timeout: {timeout}s")
            )
    
    def interact_with_session(self, session_id: str, input_data: str, 
                            timeout: float = 1.0) -> Dict[str, Any]:
        """Interact with an active PTY session"""
        if session_id not in self.active_sessions:
            return {
                "success": False,
                "error": f"Session {session_id} not found"
            }
        
        session = self.active_sessions[session_id]
        
        try:
            # Send input
            if input_data:
                session.write_input(input_data)
            
            # Read output
            time.sleep(timeout)
            output = session.read_output()
            errors = session.read_errors()
            
            return {
                "success": True,
                "output": output,
                "errors": errors,
                "running": session.is_alive(),
                "exit_code": session.get_exit_code()
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def terminate_session(self, session_id: str) -> bool:
        """Terminate an active PTY session"""
        if session_id not in self.active_sessions:
            return False
        
        try:
            session = self.active_sessions[session_id]
            success = session.terminate()
            if success:
                del self.active_sessions[session_id]
            return success
            
        except Exception as e:
            logger.error(f"Error terminating session {session_id}: {e}")
            return False
    
    def list_active_sessions(self) -> Dict[str, Dict[str, Any]]:
        """List all active PTY sessions"""
        sessions = {}
        
        for session_id, session in self.active_sessions.items():
            sessions[session_id] = {
                "running": session.is_alive(),
                "command": session.command,
                "working_dir": session.working_dir,
                "exit_code": session.get_exit_code()
            }
        
        # Clean up dead sessions
        dead_sessions = [sid for sid, info in sessions.items() if not info["running"]]
        for sid in dead_sessions:
            self.terminate_session(sid)
        
        return sessions
    
    def _is_command_safe(self, command: str) -> bool:
        """Enhanced security check for commands"""
        dangerous_patterns = [
            r'rm\s+-rf\s+/',
            r'del\s+/[fqs]\s+c:\\',
            r'format\s+c:',
            r'dd\s+if=.*of=/dev/',
            r':\(\)\{\s*:\s*\|\s*:\s*&\s*\};:',  # Fork bomb pattern
            r'sudo\s+rm\s+-rf',
            r'chmod\s+777\s+/',
            r'>\s*/dev/.*',
            r'mkfs\.',
            r'fdisk.*--delete',
        ]
        
        import re
        command_lower = command.lower()
        
        for pattern in dangerous_patterns:
            if re.search(pattern, command_lower):
                return False
        
        return True
    
    async def can_handle(self, user_input: str) -> bool:
        """Check if this tool can handle the user input"""
        input_lower = user_input.lower()
        keywords = [
            "run", "execute", "command", "shell", "bash", "cmd",
            "interactive", "pty", "terminal", "process"
        ]
        return any(keyword in input_lower for keyword in keywords)


class PythonExecutionTool(SyncTool):
    """Ferramenta para execução segura de código Python"""
    
    @property
    def name(self) -> str:
        return "python_execute"
    
    @property
    def description(self) -> str:
        return "Executes Python code in a controlled environment"
    
    @property
    def category(self) -> str:
        return "execution"
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa código Python"""
        code = context.parsed_args.get("code")
        timeout = context.parsed_args.get("timeout", 30)
        
        if not code:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="No Python code provided",
                error=ValidationError("code is required")
            )
        
        # Lista de imports/módulos perigosos
        dangerous_imports = [
            "os.system", "subprocess", "shutil.rmtree", "open(",
            "__import__", "exec", "eval", "compile",
            "file", "input", "raw_input"
        ]
        
        # Verifica código perigoso
        code_lines = code.lower()
        dangerous_found = [d for d in dangerous_imports if d in code_lines]
        if dangerous_found:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Dangerous code patterns detected: {', '.join(dangerous_found)}",
                error=PermissionError("Unsafe code blocked")
            )
        
        try:
            # Cria arquivo temporário
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                temp_file = f.name
            
            try:
                # Executa Python com timeout
                result = subprocess.run([
                    sys.executable, temp_file
                ], 
                capture_output=True, 
                text=True, 
                timeout=timeout,
                cwd=context.working_directory
                )
                
                output = result.stdout.strip() if result.stdout else ""
                error_output = result.stderr.strip() if result.stderr else ""
                
                if result.returncode == 0:
                    return ToolResult(
                        status=ToolStatus.SUCCESS,
                        data=output,
                        message="Python code executed successfully",
                        metadata={
                            "exit_code": result.returncode,
                            "stdout": output,
                            "stderr": error_output
                        }
                    )
                else:
                    return ToolResult(
                        status=ToolStatus.ERROR,
                        data=error_output,
                        message=f"Python code failed with exit code {result.returncode}",
                        metadata={
                            "exit_code": result.returncode,
                            "stdout": output,
                            "stderr": error_output
                        }
                    )
                    
            finally:
                # Limpa arquivo temporário
                try:
                    os.unlink(temp_file)
                except:
                    pass
                    
        except subprocess.TimeoutExpired:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Python code timed out after {timeout} seconds",
                error=TimeoutError(f"Execution timeout: {timeout}s")
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error executing Python code: {str(e)}",
                error=e
            )
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "python", "run code", "execute code", "script"
        ])


class TestRunnerTool(SyncTool):
    """Ferramenta para execução de testes (implementação futura)"""
    
    @property
    def name(self) -> str:
        return "run_tests"
    
    @property
    def description(self) -> str:
        return "Runs project tests and returns results"
    
    @property
    def category(self) -> str:
        return "testing"
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa testes do projeto"""
        test_path = context.parsed_args.get("test_path", "tests/")
        test_type = context.parsed_args.get("test_type", "pytest")  # pytest, unittest, etc.
        verbose = context.parsed_args.get("verbose", False)
        
        # Comandos de teste baseado no tipo
        test_commands = {
            "pytest": ["python", "-m", "pytest"],
            "unittest": ["python", "-m", "unittest"],
            "nose": ["nosetests"]
        }
        
        if test_type not in test_commands:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Unsupported test type: {test_type}",
                error=ValidationError(f"test_type must be one of: {list(test_commands.keys())}")
            )
        
        try:
            # Constrói comando
            cmd = test_commands[test_type].copy()
            
            if test_path and test_path != ".":
                cmd.append(test_path)
            
            if verbose and test_type == "pytest":
                cmd.append("-v")
            
            # Executa testes
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutos de timeout para testes
                cwd=context.working_directory
            )
            
            output = result.stdout.strip() if result.stdout else ""
            error_output = result.stderr.strip() if result.stderr else ""
            
            # Analisa resultado básico
            if test_type == "pytest":
                if "failed" in output.lower() or result.returncode != 0:
                    status = ToolStatus.ERROR
                    message = "Tests failed"
                else:
                    status = ToolStatus.SUCCESS
                    message = "All tests passed"
            else:
                status = ToolStatus.SUCCESS if result.returncode == 0 else ToolStatus.ERROR
                message = "Tests completed" if result.returncode == 0 else "Tests failed"
            
            return ToolResult(
                status=status,
                data=output,
                message=message,
                metadata={
                    "test_type": test_type,
                    "test_path": test_path,
                    "exit_code": result.returncode,
                    "stdout": output,
                    "stderr": error_output
                }
            )
            
        except subprocess.TimeoutExpired:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="Tests timed out after 5 minutes",
                error=TimeoutError("Test execution timeout")
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error running tests: {str(e)}",
                error=e
            )
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "test", "pytest", "unittest", "run tests"
        ])