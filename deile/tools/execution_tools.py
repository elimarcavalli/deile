"""Ferramentas para execução de código e comandos"""

import subprocess
import asyncio
import tempfile
import os
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any

from .base import SyncTool, AsyncTool, ToolContext, ToolResult, ToolStatus
from ..core.exceptions import ToolError, ValidationError


class ExecutionTool(SyncTool):
    """Ferramenta básica para execução de comandos (preparada para sandbox)"""
    
    @property
    def name(self) -> str:
        return "execute_command"
    
    @property
    def description(self) -> str:
        return "Executes system commands (use with caution)"
    
    @property
    def category(self) -> str:
        return "execution"
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Executa comando do sistema"""
        command = context.parsed_args.get("command")
        allow_dangerous = context.parsed_args.get("allow_dangerous", False)
        timeout = context.parsed_args.get("timeout", 30)
        
        if not command:
            return ToolResult(
                status=ToolStatus.ERROR,
                message="No command provided",
                error=ValidationError("command is required")
            )
        
        # Lista de comandos perigosos (pode ser expandida)
        dangerous_commands = [
            "rm", "del", "format", "fdisk", "mkfs", "dd",
            "shutdown", "reboot", "halt", "poweroff",
            "sudo rm", "sudo del", "> /dev/", "chmod 777"
        ]
        
        # Verifica comandos perigosos
        if not allow_dangerous:
            command_lower = command.lower()
            if any(dangerous in command_lower for dangerous in dangerous_commands):
                return ToolResult(
                    status=ToolStatus.ERROR,
                    message=f"Dangerous command blocked: {command}. Use allow_dangerous=True if needed.",
                    error=PermissionError("Dangerous command blocked")
                )
        
        try:
            # Executa comando com timeout
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=context.working_directory
            )
            
            # Prepara resultado
            output = result.stdout.strip() if result.stdout else ""
            error_output = result.stderr.strip() if result.stderr else ""
            
            if result.returncode == 0:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    data=output,
                    message=f"Command executed successfully (exit code: {result.returncode})",
                    metadata={
                        "command": command,
                        "exit_code": result.returncode,
                        "stdout": output,
                        "stderr": error_output,
                        "execution_directory": context.working_directory
                    }
                )
            else:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    data=error_output,
                    message=f"Command failed with exit code {result.returncode}",
                    metadata={
                        "command": command,
                        "exit_code": result.returncode,
                        "stdout": output,
                        "stderr": error_output
                    }
                )
                
        except subprocess.TimeoutExpired:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Command timed out after {timeout} seconds",
                error=TimeoutError(f"Command timeout: {timeout}s")
            )
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Error executing command: {str(e)}",
                error=e
            )
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se pode processar a entrada"""
        input_lower = user_input.lower()
        return any(keyword in input_lower for keyword in [
            "run", "execute", "command", "shell", "bash", "cmd"
        ])


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