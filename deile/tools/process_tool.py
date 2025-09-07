"""
Process Management Tool for DEILE v4.0
======================================

Comprehensive process management tool for listing, monitoring, and managing
system processes with security controls and cross-platform compatibility.

Author: DEILE
Version: 4.0
"""

import json
import logging
import platform
import re
import signal
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Tuple

import psutil

from deile.core.models.base import SyncTool, ToolRegistry
from deile.core.context_manager import ContextManager  
from deile.core.exceptions import ToolError
from deile.infrastructure.security.secrets_scanner import SecretsScanner

logger = logging.getLogger(__name__)


class DisplayPolicy(Enum):
    """Display policy for process information"""
    SYSTEM = "system"      # Show system processes
    AGENT = "agent"        # Show agent processes only
    BOTH = "both"          # Show both system and agent processes
    SILENT = "silent"      # Minimal output


class ProcessStatus(Enum):
    """Process status enumeration"""
    RUNNING = "running"
    SLEEPING = "sleeping"
    STOPPED = "stopped"
    ZOMBIE = "zombie"
    IDLE = "idle"
    UNKNOWN = "unknown"


@dataclass
class ProcessInfo:
    """Process information data structure"""
    pid: int
    name: str
    status: str
    cpu_percent: float
    memory_percent: float
    memory_info: Dict[str, int]
    create_time: float
    ppid: Optional[int] = None
    cmdline: List[str] = None
    cwd: Optional[str] = None
    username: Optional[str] = None
    connections: List[Dict] = None
    open_files: List[str] = None
    num_threads: int = 0
    children: List[int] = None

    def __post_init__(self):
        if self.cmdline is None:
            self.cmdline = []
        if self.connections is None:
            self.connections = []
        if self.open_files is None:
            self.open_files = []
        if self.children is None:
            self.children = []


class ProcessTool(SyncTool):
    """
    Advanced process management tool with security controls
    
    Features:
    - List and filter processes
    - Monitor process metrics
    - Secure process termination
    - Process tree analysis
    - Cross-platform compatibility
    - Resource monitoring
    """

    def __init__(self):
        super().__init__()
        self.name = "process_tool"
        self.description = "Process management tool for listing, monitoring, and managing system processes"
        self.secrets_scanner = SecretsScanner()
        self.context_manager = ContextManager()
        
        # Security settings
        self.protected_processes = {
            'windows': {
                'System', 'csrss.exe', 'winlogon.exe', 'services.exe', 
                'lsass.exe', 'spoolsv.exe', 'explorer.exe', 'dwm.exe'
            },
            'linux': {
                'init', 'kthreadd', 'systemd', 'kernel', 'ksoftirqd',
                'migration', 'rcu_', 'watchdog', 'NetworkManager',
                'systemd-', 'dbus', 'sshd'  
            },
            'darwin': {
                'kernel_task', 'launchd', 'UserEventAgent', 'loginwindow',
                'WindowServer', 'SystemUIServer', 'Dock', 'Finder'
            }
        }

    def get_schema(self) -> Dict[str, Any]:
        """Get the JSON schema for this tool"""
        return {
            "type": "object", 
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_processes", "get_process", "kill_process", 
                        "monitor_process", "process_tree", "find_process",
                        "get_connections", "get_open_files", "system_stats"
                    ],
                    "description": "The action to perform"
                },
                "pid": {
                    "type": "integer",
                    "description": "Process ID for operations on specific processes"
                },
                "name": {
                    "type": "string", 
                    "description": "Process name pattern for filtering"
                },
                "user": {
                    "type": "string",
                    "description": "Filter processes by username"
                },
                "signal": {
                    "type": "string",
                    "enum": ["TERM", "KILL", "INT", "HUP", "USR1", "USR2"],
                    "description": "Signal to send when killing process",
                    "default": "TERM"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds for operations",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 300
                },
                "display_policy": {
                    "type": "string", 
                    "enum": ["system", "agent", "both", "silent"],
                    "description": "Control what processes to display",
                    "default": "both"
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["cpu", "memory", "name", "pid", "create_time"],
                    "description": "Sort processes by field",
                    "default": "cpu"
                },
                "limit": {
                    "type": "integer",
                    "description": "Limit number of processes returned",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 1000
                },
                "include_children": {
                    "type": "boolean", 
                    "description": "Include child processes in operations",
                    "default": False
                },
                "monitor_duration": {
                    "type": "integer",
                    "description": "Duration in seconds for monitoring",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 300
                }
            },
            "required": ["action"],
            "additionalProperties": False
        }

    def execute(self, **params) -> Dict[str, Any]:
        """Execute the process management tool"""
        try:
            action = params.get("action")
            
            if action == "list_processes":
                return self._list_processes(**params)
            elif action == "get_process":
                return self._get_process(**params) 
            elif action == "kill_process":
                return self._kill_process(**params)
            elif action == "monitor_process":
                return self._monitor_process(**params)
            elif action == "process_tree":
                return self._process_tree(**params)
            elif action == "find_process":
                return self._find_process(**params)
            elif action == "get_connections":
                return self._get_connections(**params)
            elif action == "get_open_files": 
                return self._get_open_files(**params)
            elif action == "system_stats":
                return self._system_stats(**params)
            else:
                return self._error(f"Unknown action: {action}")
                
        except psutil.Error as e:
            return self._error(f"Process operation failed: {str(e)}")
        except Exception as e:
            logger.error(f"ProcessTool execution error: {str(e)}")
            return self._error(f"Execution failed: {str(e)}")

    def _list_processes(self, **params) -> Dict[str, Any]:
        """List system processes with filtering and sorting"""
        try:
            display_policy = DisplayPolicy(params.get("display_policy", "both"))
            sort_by = params.get("sort_by", "cpu")
            limit = params.get("limit", 50)
            name_filter = params.get("name", "")
            user_filter = params.get("user", "")
            
            processes = []
            
            for proc in psutil.process_iter(['pid', 'name', 'status', 'username', 
                                           'cpu_percent', 'memory_percent', 
                                           'create_time', 'ppid']):
                try:
                    pinfo = proc.info
                    
                    # Apply filters
                    if name_filter and name_filter.lower() not in pinfo['name'].lower():
                        continue
                    if user_filter and pinfo.get('username', '').lower() != user_filter.lower():
                        continue
                        
                    # Apply display policy
                    if display_policy == DisplayPolicy.AGENT:
                        if not self._is_agent_process(pinfo['name'], pinfo['pid']):
                            continue
                    elif display_policy == DisplayPolicy.SYSTEM:
                        if self._is_agent_process(pinfo['name'], pinfo['pid']):
                            continue
                    elif display_policy == DisplayPolicy.SILENT:
                        # Minimal output mode
                        processes.append({
                            'pid': pinfo['pid'],
                            'name': pinfo['name'],
                            'status': pinfo['status']
                        })
                        continue
                    
                    process_data = {
                        'pid': pinfo['pid'],
                        'name': pinfo['name'],
                        'status': pinfo['status'],
                        'username': pinfo.get('username', 'N/A'),
                        'cpu_percent': round(pinfo.get('cpu_percent', 0), 2),
                        'memory_percent': round(pinfo.get('memory_percent', 0), 2),
                        'create_time': pinfo.get('create_time', 0),
                        'ppid': pinfo.get('ppid', 0)
                    }
                    processes.append(process_data)
                    
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # Sort processes
            if sort_by == "cpu":
                processes.sort(key=lambda x: x.get('cpu_percent', 0), reverse=True)
            elif sort_by == "memory":
                processes.sort(key=lambda x: x.get('memory_percent', 0), reverse=True)
            elif sort_by == "name":
                processes.sort(key=lambda x: x.get('name', '').lower())
            elif sort_by == "pid":
                processes.sort(key=lambda x: x.get('pid', 0))
            elif sort_by == "create_time":
                processes.sort(key=lambda x: x.get('create_time', 0), reverse=True)
            
            # Limit results
            if limit > 0:
                processes = processes[:limit]
            
            return self._success({
                'processes': processes,
                'total_count': len(processes),
                'sort_by': sort_by,
                'display_policy': display_policy.value,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to list processes: {str(e)}")

    def _get_process(self, **params) -> Dict[str, Any]:
        """Get detailed information about specific process"""
        try:
            pid = params.get("pid")
            if not pid:
                return self._error("PID is required for get_process action")
            
            try:
                proc = psutil.Process(pid)
                
                # Get comprehensive process information
                with proc.oneshot():
                    pinfo = ProcessInfo(
                        pid=proc.pid,
                        name=proc.name(),
                        status=proc.status(),
                        cpu_percent=proc.cpu_percent(),
                        memory_percent=proc.memory_percent(),
                        memory_info=proc.memory_info()._asdict(),
                        create_time=proc.create_time(),
                        ppid=proc.ppid(),
                        cmdline=proc.cmdline(),
                        cwd=proc.cwd() if self._can_access_cwd(proc) else None,
                        username=proc.username() if self._can_access_username(proc) else None,
                        num_threads=proc.num_threads()
                    )
                
                # Get connections (if accessible)
                try:
                    connections = []
                    for conn in proc.connections():
                        connections.append({
                            'fd': getattr(conn, 'fd', None),
                            'family': conn.family.name if hasattr(conn.family, 'name') else str(conn.family),
                            'type': conn.type.name if hasattr(conn.type, 'name') else str(conn.type),
                            'local_address': f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
                            'remote_address': f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
                            'status': conn.status
                        })
                    pinfo.connections = connections
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pinfo.connections = []
                
                # Get open files (if accessible)
                try:
                    open_files = []
                    for file_info in proc.open_files():
                        open_files.append({
                            'path': file_info.path,
                            'fd': file_info.fd if hasattr(file_info, 'fd') else None,
                            'position': file_info.position if hasattr(file_info, 'position') else None,
                            'mode': file_info.mode if hasattr(file_info, 'mode') else None
                        })
                    pinfo.open_files = open_files
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pinfo.open_files = []
                
                # Get child processes
                try:
                    children = [child.pid for child in proc.children()]
                    pinfo.children = children
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pinfo.children = []
                
                # Scan for secrets in command line
                secrets_found = []
                if pinfo.cmdline:
                    cmdline_str = ' '.join(pinfo.cmdline)
                    secrets_found = self.secrets_scanner.scan_text(cmdline_str)
                
                return self._success({
                    'process': asdict(pinfo),
                    'secrets_detected': len(secrets_found),
                    'secrets_summary': [s.get('type', 'unknown') for s in secrets_found],
                    'is_protected': self._is_protected_process(pinfo.name),
                    'is_agent_process': self._is_agent_process(pinfo.name, pinfo.pid),
                    'timestamp': datetime.now().isoformat()
                })
                
            except psutil.NoSuchProcess:
                return self._error(f"Process with PID {pid} not found")
            except psutil.AccessDenied:
                return self._error(f"Access denied to process {pid}")
                
        except Exception as e:
            return self._error(f"Failed to get process information: {str(e)}")

    def _kill_process(self, **params) -> Dict[str, Any]:
        """Terminate a process with security checks"""
        try:
            pid = params.get("pid")
            signal_name = params.get("signal", "TERM")
            timeout = params.get("timeout", 30)
            include_children = params.get("include_children", False)
            
            if not pid:
                return self._error("PID is required for kill_process action")
            
            try:
                proc = psutil.Process(pid)
                process_name = proc.name()
                
                # Security check - protect critical system processes
                if self._is_protected_process(process_name):
                    return self._error(f"Cannot kill protected system process: {process_name}")
                
                # Get children before termination if requested
                children_pids = []
                if include_children:
                    try:
                        children_pids = [child.pid for child in proc.children(recursive=True)]
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
                
                # Map signal names to signal values
                signal_map = {
                    "TERM": signal.SIGTERM,
                    "KILL": signal.SIGKILL, 
                    "INT": signal.SIGINT,
                    "HUP": signal.SIGHUP
                }
                
                if platform.system() != "Windows":
                    signal_map.update({
                        "USR1": signal.SIGUSR1,
                        "USR2": signal.SIGUSR2
                    })
                
                sig = signal_map.get(signal_name, signal.SIGTERM)
                
                killed_processes = []
                
                # Kill children first if requested
                if include_children and children_pids:
                    for child_pid in children_pids:
                        try:
                            child_proc = psutil.Process(child_pid)
                            child_proc.send_signal(sig)
                            killed_processes.append({
                                'pid': child_pid,
                                'name': child_proc.name(),
                                'signal': signal_name,
                                'type': 'child'
                            })
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                
                # Kill main process
                proc.send_signal(sig)
                killed_processes.append({
                    'pid': pid,
                    'name': process_name,
                    'signal': signal_name,
                    'type': 'main'
                })
                
                # Wait for termination if using graceful signals
                if signal_name in ["TERM", "INT", "HUP"]:
                    try:
                        proc.wait(timeout=timeout)
                        termination_status = "graceful"
                    except psutil.TimeoutExpired:
                        # Force kill if graceful termination failed
                        proc.kill()
                        termination_status = "forced"
                else:
                    termination_status = "immediate"
                
                return self._success({
                    'killed_processes': killed_processes,
                    'termination_status': termination_status,
                    'signal_sent': signal_name,
                    'children_included': include_children,
                    'total_killed': len(killed_processes),
                    'timestamp': datetime.now().isoformat()
                })
                
            except psutil.NoSuchProcess:
                return self._error(f"Process with PID {pid} not found")
            except psutil.AccessDenied:
                return self._error(f"Access denied to kill process {pid}")
                
        except Exception as e:
            return self._error(f"Failed to kill process: {str(e)}")

    def _monitor_process(self, **params) -> Dict[str, Any]:
        """Monitor a process over time"""
        try:
            pid = params.get("pid")
            duration = params.get("monitor_duration", 10)
            
            if not pid:
                return self._error("PID is required for monitor_process action")
            
            try:
                proc = psutil.Process(pid)
                samples = []
                
                start_time = time.time()
                sample_interval = max(1, duration // 10)  # 10 samples max
                
                while time.time() - start_time < duration:
                    try:
                        with proc.oneshot():
                            sample = {
                                'timestamp': time.time(),
                                'cpu_percent': proc.cpu_percent(),
                                'memory_percent': proc.memory_percent(),
                                'memory_rss': proc.memory_info().rss,
                                'memory_vms': proc.memory_info().vms,
                                'num_threads': proc.num_threads(),
                                'status': proc.status()
                            }
                            
                            # Add I/O stats if available
                            try:
                                io_stats = proc.io_counters()
                                sample.update({
                                    'read_bytes': io_stats.read_bytes,
                                    'write_bytes': io_stats.write_bytes,
                                    'read_count': io_stats.read_count,
                                    'write_count': io_stats.write_count
                                })
                            except (psutil.AccessDenied, AttributeError):
                                pass
                            
                            samples.append(sample)
                            
                    except psutil.NoSuchProcess:
                        samples.append({
                            'timestamp': time.time(),
                            'status': 'terminated',
                            'cpu_percent': 0,
                            'memory_percent': 0
                        })
                        break
                    
                    time.sleep(sample_interval)
                
                # Calculate statistics
                cpu_values = [s.get('cpu_percent', 0) for s in samples if 'cpu_percent' in s]
                memory_values = [s.get('memory_percent', 0) for s in samples if 'memory_percent' in s]
                
                stats = {
                    'avg_cpu': round(sum(cpu_values) / len(cpu_values), 2) if cpu_values else 0,
                    'max_cpu': round(max(cpu_values), 2) if cpu_values else 0,
                    'avg_memory': round(sum(memory_values) / len(memory_values), 2) if memory_values else 0,
                    'max_memory': round(max(memory_values), 2) if memory_values else 0,
                    'sample_count': len(samples),
                    'monitoring_duration': time.time() - start_time
                }
                
                return self._success({
                    'pid': pid,
                    'process_name': proc.name() if proc.is_running() else 'Unknown',
                    'samples': samples,
                    'statistics': stats,
                    'timestamp': datetime.now().isoformat()
                })
                
            except psutil.NoSuchProcess:
                return self._error(f"Process with PID {pid} not found")
                
        except Exception as e:
            return self._error(f"Failed to monitor process: {str(e)}")

    def _process_tree(self, **params) -> Dict[str, Any]:
        """Get process tree starting from specified PID or root"""
        try:
            pid = params.get("pid")
            
            def build_tree(process, depth=0, max_depth=5):
                if depth > max_depth:
                    return None
                    
                try:
                    tree_node = {
                        'pid': process.pid,
                        'name': process.name(),
                        'status': process.status(),
                        'cpu_percent': round(process.cpu_percent(), 2),
                        'memory_percent': round(process.memory_percent(), 2),
                        'create_time': process.create_time(),
                        'depth': depth,
                        'children': []
                    }
                    
                    # Add children
                    for child in process.children():
                        child_node = build_tree(child, depth + 1, max_depth)
                        if child_node:
                            tree_node['children'].append(child_node)
                    
                    return tree_node
                    
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return None
            
            if pid:
                # Build tree from specific process
                try:
                    root_proc = psutil.Process(pid)
                    tree = build_tree(root_proc)
                    
                    if not tree:
                        return self._error(f"Could not build tree for PID {pid}")
                        
                    return self._success({
                        'tree': tree,
                        'root_pid': pid,
                        'timestamp': datetime.now().isoformat()
                    })
                    
                except psutil.NoSuchProcess:
                    return self._error(f"Process with PID {pid} not found")
            else:
                # Build tree from all root processes
                trees = []
                for proc in psutil.process_iter():
                    try:
                        if proc.ppid() == 0 or proc.ppid() is None:  # Root processes
                            tree = build_tree(proc, max_depth=3)  # Limit depth for root
                            if tree:
                                trees.append(tree)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                
                # Sort by CPU usage
                trees.sort(key=lambda x: x.get('cpu_percent', 0), reverse=True)
                
                return self._success({
                    'trees': trees[:20],  # Limit to top 20 trees
                    'total_trees': len(trees),
                    'timestamp': datetime.now().isoformat()
                })
                
        except Exception as e:
            return self._error(f"Failed to build process tree: {str(e)}")

    def _find_process(self, **params) -> Dict[str, Any]:
        """Find processes by name pattern"""
        try:
            name_pattern = params.get("name", "")
            user_filter = params.get("user", "")
            limit = params.get("limit", 50)
            
            if not name_pattern:
                return self._error("Name pattern is required for find_process action")
            
            found_processes = []
            pattern = re.compile(name_pattern, re.IGNORECASE)
            
            for proc in psutil.process_iter(['pid', 'name', 'status', 'username', 
                                           'cpu_percent', 'memory_percent', 'cmdline']):
                try:
                    pinfo = proc.info
                    
                    # Check name match
                    if not pattern.search(pinfo['name']):
                        # Also check command line
                        cmdline = ' '.join(pinfo.get('cmdline', []))
                        if not pattern.search(cmdline):
                            continue
                    
                    # Apply user filter
                    if user_filter and pinfo.get('username', '').lower() != user_filter.lower():
                        continue
                    
                    process_data = {
                        'pid': pinfo['pid'],
                        'name': pinfo['name'],
                        'status': pinfo['status'],
                        'username': pinfo.get('username', 'N/A'),
                        'cpu_percent': round(pinfo.get('cpu_percent', 0), 2),
                        'memory_percent': round(pinfo.get('memory_percent', 0), 2),
                        'cmdline': ' '.join(pinfo.get('cmdline', []))[:100] + '...' if len(' '.join(pinfo.get('cmdline', []))) > 100 else ' '.join(pinfo.get('cmdline', []))
                    }
                    found_processes.append(process_data)
                    
                    if len(found_processes) >= limit:
                        break
                        
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            return self._success({
                'found_processes': found_processes,
                'count': len(found_processes),
                'search_pattern': name_pattern,
                'user_filter': user_filter,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to find processes: {str(e)}")

    def _get_connections(self, **params) -> Dict[str, Any]:
        """Get network connections for a process or system-wide"""
        try:
            pid = params.get("pid")
            
            connections = []
            
            if pid:
                # Get connections for specific process
                try:
                    proc = psutil.Process(pid)
                    for conn in proc.connections():
                        connections.append(self._format_connection(conn, pid, proc.name()))
                except psutil.NoSuchProcess:
                    return self._error(f"Process with PID {pid} not found")
                except psutil.AccessDenied:
                    return self._error(f"Access denied to connections for PID {pid}")
            else:
                # Get all system connections
                try:
                    for conn in psutil.net_connections(kind='all'):
                        proc_name = "Unknown"
                        if conn.pid:
                            try:
                                proc = psutil.Process(conn.pid)
                                proc_name = proc.name()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        connections.append(self._format_connection(conn, conn.pid, proc_name))
                except psutil.AccessDenied:
                    return self._error("Access denied to system connections")
            
            # Group by status
            connection_stats = {}
            for conn in connections:
                status = conn.get('status', 'UNKNOWN')
                connection_stats[status] = connection_stats.get(status, 0) + 1
            
            return self._success({
                'connections': connections,
                'total_connections': len(connections),
                'connection_stats': connection_stats,
                'pid_filter': pid,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to get connections: {str(e)}")

    def _get_open_files(self, **params) -> Dict[str, Any]:
        """Get open files for a process"""
        try:
            pid = params.get("pid")
            
            if not pid:
                return self._error("PID is required for get_open_files action")
            
            try:
                proc = psutil.Process(pid)
                open_files = []
                
                for file_info in proc.open_files():
                    file_data = {
                        'path': file_info.path,
                        'fd': file_info.fd if hasattr(file_info, 'fd') else None,
                        'position': file_info.position if hasattr(file_info, 'position') else None,
                        'mode': file_info.mode if hasattr(file_info, 'mode') else None,
                        'size': None,
                        'modified': None
                    }
                    
                    # Get file stats if accessible
                    try:
                        file_path = Path(file_info.path)
                        if file_path.exists():
                            stat_info = file_path.stat()
                            file_data.update({
                                'size': stat_info.st_size,
                                'modified': stat_info.st_mtime
                            })
                    except (OSError, PermissionError):
                        pass
                    
                    open_files.append(file_data)
                
                return self._success({
                    'pid': pid,
                    'process_name': proc.name(),
                    'open_files': open_files,
                    'file_count': len(open_files),
                    'timestamp': datetime.now().isoformat()
                })
                
            except psutil.NoSuchProcess:
                return self._error(f"Process with PID {pid} not found")
            except psutil.AccessDenied:
                return self._error(f"Access denied to open files for PID {pid}")
                
        except Exception as e:
            return self._error(f"Failed to get open files: {str(e)}")

    def _system_stats(self, **params) -> Dict[str, Any]:
        """Get system-wide process statistics"""
        try:
            # CPU stats
            cpu_stats = {
                'cpu_count_logical': psutil.cpu_count(logical=True),
                'cpu_count_physical': psutil.cpu_count(logical=False),
                'cpu_percent': psutil.cpu_percent(interval=1),
                'cpu_times': psutil.cpu_times()._asdict(),
                'load_average': psutil.getloadavg() if hasattr(psutil, 'getloadavg') else None
            }
            
            # Memory stats
            memory_stats = {
                'virtual_memory': psutil.virtual_memory()._asdict(),
                'swap_memory': psutil.swap_memory()._asdict()
            }
            
            # Process stats
            process_stats = {
                'total_processes': len(psutil.pids()),
                'running_processes': 0,
                'sleeping_processes': 0,
                'zombie_processes': 0,
                'stopped_processes': 0
            }
            
            # Count processes by status
            for proc in psutil.process_iter(['status']):
                try:
                    status = proc.info['status']
                    if status == psutil.STATUS_RUNNING:
                        process_stats['running_processes'] += 1
                    elif status == psutil.STATUS_SLEEPING:
                        process_stats['sleeping_processes'] += 1
                    elif status == psutil.STATUS_ZOMBIE:
                        process_stats['zombie_processes'] += 1
                    elif status == psutil.STATUS_STOPPED:
                        process_stats['stopped_processes'] += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # Network stats
            network_stats = {}
            try:
                net_io = psutil.net_io_counters()
                network_stats = {
                    'bytes_sent': net_io.bytes_sent,
                    'bytes_recv': net_io.bytes_recv,
                    'packets_sent': net_io.packets_sent,
                    'packets_recv': net_io.packets_recv,
                    'errin': net_io.errin,
                    'errout': net_io.errout,
                    'dropin': net_io.dropin,
                    'dropout': net_io.dropout
                }
            except AttributeError:
                network_stats = {'error': 'Network stats not available on this platform'}
            
            # Disk stats
            disk_stats = {}
            try:
                disk_io = psutil.disk_io_counters()
                if disk_io:
                    disk_stats = {
                        'read_bytes': disk_io.read_bytes,
                        'write_bytes': disk_io.write_bytes,
                        'read_count': disk_io.read_count,
                        'write_count': disk_io.write_count,
                        'read_time': disk_io.read_time,
                        'write_time': disk_io.write_time
                    }
            except AttributeError:
                disk_stats = {'error': 'Disk stats not available on this platform'}
            
            # System info
            system_info = {
                'platform': platform.system(),
                'platform_release': platform.release(),
                'platform_version': platform.version(),
                'architecture': platform.architecture(),
                'hostname': platform.node(),
                'boot_time': psutil.boot_time()
            }
            
            return self._success({
                'cpu_stats': cpu_stats,
                'memory_stats': memory_stats,
                'process_stats': process_stats,
                'network_stats': network_stats,
                'disk_stats': disk_stats,
                'system_info': system_info,
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            return self._error(f"Failed to get system stats: {str(e)}")

    def _format_connection(self, conn, pid, proc_name) -> Dict[str, Any]:
        """Format connection information"""
        return {
            'pid': pid,
            'process_name': proc_name,
            'fd': getattr(conn, 'fd', None),
            'family': conn.family.name if hasattr(conn.family, 'name') else str(conn.family),
            'type': conn.type.name if hasattr(conn.type, 'name') else str(conn.type),
            'local_address': f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
            'remote_address': f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
            'status': conn.status
        }

    def _is_protected_process(self, process_name: str) -> bool:
        """Check if process is protected from termination"""
        system = platform.system().lower()
        protected_set = self.protected_processes.get(system, set())
        
        process_name_lower = process_name.lower()
        
        for protected in protected_set:
            if protected.lower() in process_name_lower or process_name_lower.startswith(protected.lower()):
                return True
        
        return False

    def _is_agent_process(self, process_name: str, pid: int) -> bool:
        """Check if process is related to DEILE agent"""
        agent_indicators = [
            'python', 'deile', 'agent', 'claude', 'ai', 'llm',
            'jupyter', 'notebook', 'ipython', 'conda', 'pip'
        ]
        
        process_name_lower = process_name.lower()
        
        # Check process name
        for indicator in agent_indicators:
            if indicator in process_name_lower:
                return True
        
        # Check if it's current Python process
        try:
            import os
            if pid == os.getpid():
                return True
        except:
            pass
        
        return False

    def _can_access_cwd(self, proc) -> bool:
        """Check if current working directory is accessible"""
        try:
            proc.cwd()
            return True
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return False

    def _can_access_username(self, proc) -> bool:
        """Check if username is accessible"""
        try:
            proc.username()
            return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return False

    def _success(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return success response"""
        return {
            "success": True,
            "tool": self.name,
            "data": data,
            "timestamp": datetime.now().isoformat()
        }

    def _error(self, message: str) -> Dict[str, Any]:
        """Return error response"""
        logger.error(f"ProcessTool error: {message}")
        return {
            "success": False,
            "tool": self.name,
            "error": message,
            "timestamp": datetime.now().isoformat()
        }


# Register the tool
ToolRegistry.register("process_tool", ProcessTool)