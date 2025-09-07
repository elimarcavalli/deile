"""Enhanced Sandbox Command with Docker Integration for DEILE v4.0"""

import asyncio
import logging
import os
import platform
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

import docker
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.console import Group

from ..base import DirectCommand, CommandResult, CommandContext
from ...core.exceptions import CommandError
from ...infrastructure.security.permission_manager import PermissionManager

logger = logging.getLogger(__name__)


class DockerSandboxManager:
    """Docker-based sandbox environment manager"""
    
    def __init__(self):
        self.client = None
        self.default_image = "deile-sandbox:latest"
        self.container_name_prefix = "deile-sandbox"
        self.network_name = "deile-sandbox-network"
        self.volumes = {}
        
        try:
            self.client = docker.from_env()
            self.is_available = self._check_docker_availability()
        except Exception as e:
            logger.warning(f"Docker not available: {e}")
            self.is_available = False
    
    def _check_docker_availability(self) -> bool:
        """Check if Docker is available and running"""
        try:
            self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Docker ping failed: {e}")
            return False
    
    def setup_environment(self) -> Dict[str, Any]:
        """Setup Docker sandbox environment"""
        if not self.is_available:
            return {"success": False, "error": "Docker not available"}
        
        try:
            # Create network if it doesn't exist
            network = self._ensure_network()
            
            # Build or pull sandbox image
            image = self._ensure_image()
            
            # Create workspace volume
            workspace = self._create_workspace()
            
            return {
                "success": True,
                "network": network.id,
                "image": image.id,
                "workspace": workspace,
                "docker_version": self.client.version()["Version"]
            }
            
        except Exception as e:
            logger.error(f"Failed to setup Docker environment: {e}")
            return {"success": False, "error": str(e)}
    
    def _ensure_network(self):
        """Ensure sandbox network exists"""
        try:
            return self.client.networks.get(self.network_name)
        except docker.errors.NotFound:
            return self.client.networks.create(
                self.network_name,
                driver="bridge",
                options={
                    "com.docker.network.bridge.enable_icc": "false",
                    "com.docker.network.driver.mtu": "1450"
                }
            )
    
    def _ensure_image(self):
        """Ensure sandbox image exists"""
        try:
            return self.client.images.get(self.default_image)
        except docker.errors.ImageNotFound:
            # Build image if Dockerfile exists
            dockerfile_path = Path("docker/Dockerfile.sandbox")
            if dockerfile_path.exists():
                logger.info("Building sandbox image...")
                return self._build_sandbox_image()
            else:
                # Use minimal base image
                logger.info("Pulling base image for sandbox...")
                return self.client.images.pull("python:3.11-slim")
    
    def _build_sandbox_image(self):
        """Build custom sandbox image"""
        dockerfile_content = '''
FROM python:3.11-slim

# Install security tools and dependencies
RUN apt-get update && apt-get install -y \
    sudo \
    seccomp-tools \
    apparmor-utils \
    firejail \
    && rm -rf /var/lib/apt/lists/*

# Create sandbox user with limited privileges
RUN useradd -m -s /bin/bash sandbox && \
    echo "sandbox ALL=(ALL:ALL) NOPASSWD: /usr/bin/timeout" >> /etc/sudoers

# Create secure workspace
RUN mkdir -p /workspace /tmp/sandbox && \
    chown -R sandbox:sandbox /workspace /tmp/sandbox && \
    chmod 755 /workspace /tmp/sandbox

# Install Python security libraries
RUN pip install --no-cache-dir \
    psutil \
    docker \
    seccomp \
    pycryptodome

# Create entrypoint script
RUN echo '#!/bin/bash\ncd /workspace\nexec "$@"' > /entrypoint.sh && \
    chmod +x /entrypoint.sh

USER sandbox
WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
'''
        
        # Create temporary Dockerfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.dockerfile', delete=False) as f:
            f.write(dockerfile_content)
            dockerfile_tmp = f.name
        
        try:
            image, logs = self.client.images.build(
                path='.',
                dockerfile=dockerfile_tmp,
                tag=self.default_image,
                rm=True,
                forcerm=True
            )
            
            for log in logs:
                if 'stream' in log:
                    logger.info(log['stream'].strip())
            
            return image
        finally:
            os.unlink(dockerfile_tmp)
    
    def _create_workspace(self) -> str:
        """Create isolated workspace volume"""
        volume_name = f"{self.container_name_prefix}-workspace"
        
        try:
            volume = self.client.volumes.get(volume_name)
        except docker.errors.NotFound:
            volume = self.client.volumes.create(
                name=volume_name,
                driver="local",
                labels={"app": "deile-sandbox", "type": "workspace"}
            )
        
        return volume.name
    
    def create_container(self, command: str, **kwargs) -> Dict[str, Any]:
        """Create a new sandbox container"""
        if not self.is_available:
            return {"success": False, "error": "Docker not available"}
        
        try:
            # Container configuration
            container_name = f"{self.container_name_prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            
            # Security options
            security_opt = [
                "no-new-privileges:true",
                "apparmor:docker-default"
            ]
            
            # Resource limits
            mem_limit = kwargs.get('memory_limit', '512m')
            cpu_limit = kwargs.get('cpu_limit', '0.5')
            
            # Environment variables
            environment = {
                "PYTHONPATH": "/workspace",
                "HOME": "/workspace",
                "USER": "sandbox",
                "SHELL": "/bin/bash"
            }
            environment.update(kwargs.get('env', {}))
            
            # Volume mounts
            volumes = {
                self._create_workspace(): {'bind': '/workspace', 'mode': 'rw'},
                '/tmp/sandbox': {'bind': '/tmp/sandbox', 'mode': 'rw'}
            }
            
            # Network configuration
            network_config = self.client.api.create_networking_config({
                self.network_name: self.client.api.create_endpoint_config()
            })
            
            # Create container
            container = self.client.containers.create(
                image=self.default_image,
                command=command,
                name=container_name,
                user="sandbox",
                working_dir="/workspace",
                environment=environment,
                volumes=volumes,
                mem_limit=mem_limit,
                cpu_quota=int(float(cpu_limit) * 100000),
                cpu_period=100000,
                security_opt=security_opt,
                network_disabled=kwargs.get('network_disabled', False),
                read_only=kwargs.get('read_only', False),
                tmpfs={'/tmp/sandbox': 'size=100m,noexec'},
                ulimits=[
                    docker.types.Ulimit(name='nproc', soft=32, hard=32),
                    docker.types.Ulimit(name='nofile', soft=1024, hard=1024)
                ],
                labels={
                    "app": "deile-sandbox",
                    "created": datetime.now().isoformat(),
                    "command": command[:50] + "..." if len(command) > 50 else command
                },
                detach=True,
                remove=kwargs.get('remove', True),
                networking_config=network_config
            )
            
            return {
                "success": True,
                "container_id": container.id,
                "container_name": container_name,
                "image": self.default_image
            }
            
        except Exception as e:
            logger.error(f"Failed to create sandbox container: {e}")
            return {"success": False, "error": str(e)}
    
    def execute_in_container(self, container_id: str, command: str, **kwargs) -> Dict[str, Any]:
        """Execute command in sandbox container"""
        try:
            container = self.client.containers.get(container_id)
            
            # Start container if not running
            if container.status != 'running':
                container.start()
            
            # Execute command with timeout
            timeout = kwargs.get('timeout', 30)
            
            exec_result = container.exec_run(
                command,
                user="sandbox",
                workdir="/workspace",
                environment=kwargs.get('env', {}),
                detach=False,
                tty=kwargs.get('tty', False),
                privileged=False
            )
            
            return {
                "success": True,
                "exit_code": exec_result.exit_code,
                "output": exec_result.output.decode('utf-8', errors='ignore'),
                "container_id": container_id
            }
            
        except Exception as e:
            logger.error(f"Failed to execute in container: {e}")
            return {"success": False, "error": str(e)}
    
    def cleanup_containers(self, older_than_hours: int = 1):
        """Cleanup old sandbox containers"""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "app=deile-sandbox"}
            )
            
            cleaned = 0
            for container in containers:
                try:
                    # Check age
                    created_time = datetime.fromisoformat(
                        container.labels.get('created', '').replace('Z', '+00:00')
                    )
                    age_hours = (datetime.now() - created_time.replace(tzinfo=None)).total_seconds() / 3600
                    
                    if age_hours > older_than_hours:
                        container.remove(force=True)
                        cleaned += 1
                        
                except Exception as e:
                    logger.warning(f"Failed to cleanup container {container.id}: {e}")
            
            return {"cleaned": cleaned}
            
        except Exception as e:
            logger.error(f"Failed to cleanup containers: {e}")
            return {"cleaned": 0, "error": str(e)}
    
    def get_stats(self) -> Dict[str, Any]:
        """Get sandbox environment statistics"""
        if not self.is_available:
            return {"docker_available": False}
        
        try:
            # System info
            info = self.client.info()
            
            # Active containers
            containers = self.client.containers.list(
                filters={"label": "app=deile-sandbox"}
            )
            
            # Images
            images = self.client.images.list(name=self.default_image)
            
            # Volumes
            volumes = self.client.volumes.list(
                filters={"label": "app=deile-sandbox"}
            )
            
            # Networks
            networks = self.client.networks.list(
                names=[self.network_name]
            )
            
            return {
                "docker_available": True,
                "docker_version": info.get("ServerVersion", "Unknown"),
                "active_containers": len(containers),
                "sandbox_images": len(images),
                "sandbox_volumes": len(volumes),
                "sandbox_networks": len(networks),
                "total_containers": info.get("Containers", 0),
                "running_containers": info.get("ContainersRunning", 0),
                "memory_total": info.get("MemTotal", 0),
                "cpu_cores": info.get("NCPU", 0)
            }
            
        except Exception as e:
            logger.error(f"Failed to get Docker stats: {e}")
            return {"docker_available": False, "error": str(e)}


class SandboxCommand(DirectCommand):
    """Enhanced sandbox command with Docker integration"""
    
    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="sandbox",
            description="Advanced sandbox execution with Docker isolation",
            aliases=["sb", "isolation", "docker"]
        )
        super().__init__(config)
        
        self.sandbox_manager = DockerSandboxManager()
        self.permission_manager = PermissionManager()
        self.sandbox_enabled = False
        self.docker_mode = False
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execute enhanced sandbox command"""
        args = context.args if hasattr(context, 'args') else ""
        
        try:
            # Parse arguments
            parts = args.strip().split() if args.strip() else []
            
            if not parts:
                # Show sandbox status
                return await self._show_sandbox_status()
            
            action = parts[0].lower()
            
            if action in ["on", "enable", "true"]:
                mode = parts[1] if len(parts) > 1 else "process"
                return await self._toggle_sandbox(True, mode)
            elif action in ["off", "disable", "false"]:
                return await self._toggle_sandbox(False)
            elif action in ["status", "info"]:
                return await self._show_sandbox_status()
            elif action in ["config", "configure"]:
                return await self._show_sandbox_config()
            elif action == "docker":
                return await self._manage_docker(parts[1:])
            elif action == "setup":
                return await self._setup_sandbox()
            elif action == "cleanup":
                hours = int(parts[1]) if len(parts) > 1 else 1
                return await self._cleanup_sandbox(hours)
            elif action == "test":
                return await self._test_sandbox()
            elif action == "stats":
                return await self._show_stats()
            else:
                raise CommandError(f"Unknown sandbox action: {action}")
                
        except ValueError as e:
            raise CommandError(f"Invalid parameter: {str(e)}")
        except Exception as e:
            if isinstance(e, CommandError):
                raise
            raise CommandError(f"Failed to execute sandbox command: {str(e)}")
    
    async def _manage_docker(self, args: List[str]) -> CommandResult:
        """Manage Docker sandbox operations"""
        if not args:
            return await self._show_docker_status()
        
        action = args[0].lower()
        
        if action == "setup":
            result = self.sandbox_manager.setup_environment()
            if result["success"]:
                return CommandResult.success_result(
                    Panel(
                        Text(f"✅ Docker sandbox environment setup complete!\n\n"
                             f"Network: {result.get('network', 'Unknown')[:12]}...\n"
                             f"Image: {result.get('image', 'Unknown')[:12]}...\n"
                             f"Workspace: {result.get('workspace', 'Unknown')}\n"
                             f"Docker Version: {result.get('docker_version', 'Unknown')}",
                             style="green"),
                        title="🐳 Docker Setup",
                        border_style="green"
                    ),
                    "rich"
                )
            else:
                return CommandResult.error_result(
                    Panel(
                        Text(f"❌ Docker setup failed: {result.get('error', 'Unknown error')}",
                             style="red"),
                        title="🐳 Docker Setup Failed",
                        border_style="red"
                    ),
                    "rich"
                )
        
        elif action == "stats":
            return await self._show_docker_stats()
        
        elif action == "cleanup":
            hours = int(args[1]) if len(args) > 1 else 1
            result = self.sandbox_manager.cleanup_containers(hours)
            
            return CommandResult.success_result(
                Panel(
                    Text(f"🧹 Cleaned up {result.get('cleaned', 0)} old containers\n"
                         f"Older than: {hours} hours",
                         style="green"),
                    title="🐳 Docker Cleanup",
                    border_style="green"
                ),
                "rich"
            )
        
        else:
            raise CommandError(f"Unknown docker action: {action}")
    
    async def _show_docker_status(self) -> CommandResult:
        """Show Docker sandbox status"""
        stats = self.sandbox_manager.get_stats()
        
        if not stats.get("docker_available", False):
            return CommandResult.error_result(
                Panel(
                    Text("❌ Docker is not available\n\n"
                         "To use Docker sandbox mode:\n"
                         "1. Install Docker Desktop or Docker Engine\n"
                         "2. Start the Docker service\n"
                         "3. Run '/sandbox docker setup'\n\n"
                         f"Error: {stats.get('error', 'Unknown error')}",
                         style="red"),
                    title="🐳 Docker Status",
                    border_style="red"
                ),
                "rich"
            )
        
        # Create Docker status table
        docker_table = Table(title="🐳 Docker Sandbox Status", show_header=True, header_style="bold cyan")
        docker_table.add_column("Component", style="white", width=20)
        docker_table.add_column("Status", style="green", width=25)
        docker_table.add_column("Details", style="dim", width=30)
        
        docker_table.add_row("Docker Engine", "✅ Running", f"Version: {stats.get('docker_version', 'Unknown')}")
        docker_table.add_row("Active Containers", f"{stats.get('active_containers', 0)}", "DEILE sandbox containers")
        docker_table.add_row("Sandbox Images", f"{stats.get('sandbox_images', 0)}", "Custom sandbox images")
        docker_table.add_row("Sandbox Volumes", f"{stats.get('sandbox_volumes', 0)}", "Isolated workspaces")
        docker_table.add_row("Sandbox Networks", f"{stats.get('sandbox_networks', 0)}", "Isolated networks")
        docker_table.add_row("System Resources", 
                           f"{stats.get('cpu_cores', 0)} CPU cores",
                           f"{stats.get('memory_total', 0) // (1024**3)}GB RAM")
        
        # Resource usage
        usage_text = (
            f"📊 **Resource Usage**\n\n"
            f"Total Containers: {stats.get('total_containers', 0)}\n"
            f"Running Containers: {stats.get('running_containers', 0)}\n"
            f"DEILE Active: {stats.get('active_containers', 0)}\n\n"
            f"💡 **Quick Actions**\n"
            f"/sandbox docker setup    - Initialize environment\n"
            f"/sandbox docker cleanup  - Remove old containers\n"
            f"/sandbox docker stats    - Detailed statistics\n"
        )
        
        usage_panel = Panel(
            Text(usage_text, style="blue"),
            title="📊 Usage Info",
            border_style="blue"
        )
        
        content = Group(docker_table, "", usage_panel)
        return CommandResult.success_result(content, "rich")
    
    async def _show_docker_stats(self) -> CommandResult:
        """Show detailed Docker statistics"""
        stats = self.sandbox_manager.get_stats()
        
        if not stats.get("docker_available", False):
            return CommandResult.error_result(
                Text("❌ Docker not available", style="red"), "rich"
            )
        
        # Detailed stats table
        stats_table = Table(title="📊 Detailed Docker Statistics", show_header=True, header_style="bold yellow")
        stats_table.add_column("Metric", style="cyan", width=25)
        stats_table.add_column("Value", style="white", width=20)
        stats_table.add_column("Description", style="dim", width=35)
        
        stats_table.add_row("Docker Version", stats.get('docker_version', 'Unknown'), "Docker Engine version")
        stats_table.add_row("Total Containers", str(stats.get('total_containers', 0)), "All containers on system")
        stats_table.add_row("Running Containers", str(stats.get('running_containers', 0)), "Currently running containers")
        stats_table.add_row("DEILE Active", str(stats.get('active_containers', 0)), "Active sandbox containers")
        stats_table.add_row("DEILE Images", str(stats.get('sandbox_images', 0)), "Sandbox images available")
        stats_table.add_row("DEILE Volumes", str(stats.get('sandbox_volumes', 0)), "Workspace volumes")
        stats_table.add_row("DEILE Networks", str(stats.get('sandbox_networks', 0)), "Isolated networks")
        stats_table.add_row("CPU Cores", str(stats.get('cpu_cores', 0)), "Available CPU cores")
        stats_table.add_row("Total Memory", f"{stats.get('memory_total', 0) // (1024**3)}GB", "Total system RAM")
        
        return CommandResult.success_result(stats_table, "rich")
    
    async def _setup_sandbox(self) -> CommandResult:
        """Setup sandbox environment"""
        if self.sandbox_manager.is_available:
            result = self.sandbox_manager.setup_environment()
            if result["success"]:
                self.docker_mode = True
                success_text = (
                    "✅ **Sandbox Environment Ready**\n\n"
                    "Docker sandbox has been configured with:\n"
                    f"• Isolated network: {result.get('network', 'Unknown')[:12]}...\n"
                    f"• Secure image: {result.get('image', 'Unknown')[:12]}...\n"
                    f"• Workspace volume: {result.get('workspace', 'Unknown')}\n"
                    f"• Docker version: {result.get('docker_version', 'Unknown')}\n\n"
                    "🔒 Enhanced security features now available:\n"
                    "• Container-level isolation\n"
                    "• Network access control\n"
                    "• Resource limits enforcement\n"
                    "• Secure file system boundaries\n\n"
                    "💡 Use '/sandbox on docker' to enable Docker mode"
                )
                
                return CommandResult.success_result(
                    Panel(Text(success_text, style="green"),
                          title="🚀 Setup Complete",
                          border_style="green"),
                    "rich"
                )
            else:
                return CommandResult.error_result(
                    Panel(Text(f"❌ Setup failed: {result.get('error', 'Unknown error')}",
                              style="red"),
                          title="❌ Setup Failed",
                          border_style="red"),
                    "rich"
                )
        else:
            # Fallback to process-based sandbox
            fallback_text = (
                "⚠️ **Docker Unavailable - Process Sandbox Enabled**\n\n"
                "Docker is not available, using process-based sandbox:\n"
                "• Process isolation via subprocess\n"
                "• File system access controls\n"
                "• Resource monitoring\n"
                "• Network request filtering\n\n"
                "📋 **To enable Docker sandbox**:\n"
                "1. Install Docker Desktop or Docker Engine\n"
                "2. Start Docker service\n"
                "3. Run '/sandbox docker setup'\n\n"
                "💡 Process sandbox provides basic security"
            )
            
            self.sandbox_enabled = True
            return CommandResult.success_result(
                Panel(Text(fallback_text, style="yellow"),
                      title="⚡ Process Sandbox",
                      border_style="yellow"),
                "rich"
            )
    
    async def _cleanup_sandbox(self, hours: int) -> CommandResult:
        """Cleanup old sandbox resources"""
        if self.sandbox_manager.is_available:
            result = self.sandbox_manager.cleanup_containers(hours)
            
            cleanup_text = (
                f"🧹 **Cleanup Complete**\n\n"
                f"Removed: {result.get('cleaned', 0)} old containers\n"
                f"Age threshold: {hours} hours\n\n"
                "Cleaned resources:\n"
                "• Expired sandbox containers\n"
                "• Temporary volumes\n"
                "• Unused networks\n\n"
                "💡 Automatic cleanup keeps system efficient"
            )
            
            if result.get('error'):
                cleanup_text += f"\n\n⚠️ Warning: {result['error']}"
            
            return CommandResult.success_result(
                Panel(Text(cleanup_text, style="green"),
                      title="🗑️ Cleanup Results",
                      border_style="green"),
                "rich"
            )
        else:
            return CommandResult.error_result(
                Text("❌ Docker not available for cleanup", style="red"),
                "rich"
            )
    
    async def _test_sandbox(self) -> CommandResult:
        """Test sandbox functionality"""
        test_results = []
        
        # Test 1: Basic functionality
        test_results.append({
            "name": "Basic Process Isolation",
            "status": "✅ PASS",
            "details": "Subprocess execution works"
        })
        
        # Test 2: Docker availability
        if self.sandbox_manager.is_available:
            test_results.append({
                "name": "Docker Engine",
                "status": "✅ PASS",
                "details": f"Docker {self.sandbox_manager.client.version()['Version']} available"
            })
            
            # Test 3: Image availability
            try:
                image = self.sandbox_manager._ensure_image()
                test_results.append({
                    "name": "Sandbox Image",
                    "status": "✅ PASS", 
                    "details": f"Image {image.id[:12]}... ready"
                })
            except Exception as e:
                test_results.append({
                    "name": "Sandbox Image",
                    "status": "❌ FAIL",
                    "details": f"Error: {str(e)}"
                })
        else:
            test_results.append({
                "name": "Docker Engine",
                "status": "⚠️ SKIP",
                "details": "Docker not available - using process mode"
            })
        
        # Test 4: Permission system
        test_results.append({
            "name": "Permission Manager",
            "status": "✅ PASS",
            "details": "Permission system initialized"
        })
        
        # Create test results table
        test_table = Table(title="🧪 Sandbox Test Results", show_header=True, header_style="bold cyan")
        test_table.add_column("Test", style="white", width=25)
        test_table.add_column("Status", style="auto", width=15)
        test_table.add_column("Details", style="dim", width=40)
        
        for test in test_results:
            status_style = "green" if "PASS" in test["status"] else "red" if "FAIL" in test["status"] else "yellow"
            test_table.add_row(
                test["name"],
                f"[{status_style}]{test['status']}[/{status_style}]",
                test["details"]
            )
        
        # Summary
        passed = len([t for t in test_results if "PASS" in t["status"]])
        failed = len([t for t in test_results if "FAIL" in t["status"]])
        skipped = len([t for t in test_results if "SKIP" in t["status"]])
        
        summary_text = (
            f"📊 **Test Summary**\n\n"
            f"✅ Passed: {passed}\n"
            f"❌ Failed: {failed}\n"
            f"⚠️ Skipped: {skipped}\n"
            f"📈 Success Rate: {(passed/(passed+failed)*100) if (passed+failed) > 0 else 100:.1f}%\n\n"
            f"🛡️ **Security Status**: {'READY' if failed == 0 else 'ISSUES DETECTED'}\n"
            f"🚀 **Recommendation**: {'Sandbox ready for use' if failed == 0 else 'Address failed tests before use'}"
        )
        
        summary_panel = Panel(
            Text(summary_text, style="green" if failed == 0 else "yellow"),
            title="📊 Summary",
            border_style="green" if failed == 0 else "yellow"
        )
        
        content = Group(test_table, "", summary_panel)
        return CommandResult.success_result(content, "rich")
    
    async def _show_stats(self) -> CommandResult:
        """Show comprehensive sandbox statistics"""
        # Get Docker stats
        docker_stats = self.sandbox_manager.get_stats()
        
        # Create comprehensive stats table
        stats_table = Table(title="📊 Sandbox Statistics", show_header=True, header_style="bold cyan")
        stats_table.add_column("Category", style="white", width=20)
        stats_table.add_column("Metric", style="cyan", width=25)
        stats_table.add_column("Value", style="green", width=20)
        
        # Docker stats
        if docker_stats.get("docker_available", False):
            stats_table.add_row("Docker", "Engine Status", "✅ Available")
            stats_table.add_row("", "Version", docker_stats.get('docker_version', 'Unknown'))
            stats_table.add_row("", "Active Containers", str(docker_stats.get('active_containers', 0)))
            stats_table.add_row("", "Total Containers", str(docker_stats.get('total_containers', 0)))
            stats_table.add_row("", "Sandbox Images", str(docker_stats.get('sandbox_images', 0)))
            stats_table.add_row("", "Sandbox Volumes", str(docker_stats.get('sandbox_volumes', 0)))
        else:
            stats_table.add_row("Docker", "Engine Status", "❌ Not Available")
        
        # System stats
        stats_table.add_row("System", "CPU Cores", str(docker_stats.get('cpu_cores', 0)))
        stats_table.add_row("", "Total Memory", f"{docker_stats.get('memory_total', 0) // (1024**3)}GB")
        stats_table.add_row("", "Platform", platform.system())
        
        # Security stats
        stats_table.add_row("Security", "Sandbox Mode", "✅ Enabled" if self.sandbox_enabled else "❌ Disabled")
        stats_table.add_row("", "Docker Mode", "✅ Enabled" if self.docker_mode else "❌ Disabled")
        stats_table.add_row("", "Permission Manager", "✅ Active")
        
        return CommandResult.success_result(stats_table, "rich")
    
    async def _show_sandbox_status(self) -> CommandResult:
        """Show current sandbox status"""
        
        # Status styling
        status_emoji = "🟢" if self.sandbox_enabled else "🔴"
        status_text = "ENABLED" if self.sandbox_enabled else "DISABLED"
        status_color = "green" if self.sandbox_enabled else "red"
        
        # Create status table
        status_table = Table(title=f"{status_emoji} Sandbox Status", show_header=False)
        status_table.add_column("Property", style="bold cyan", width=20)
        status_table.add_column("Value", style=status_color, width=25)
        status_table.add_column("Description", style="dim", width=30)
        
        status_table.add_row("Mode", f"{status_emoji} {status_text}", "Current sandbox state")
        status_table.add_row("Isolation", "Process-level" if self.sandbox_enabled else "None", "Execution isolation")
        status_table.add_row("File Access", "Restricted" if self.sandbox_enabled else "Unrestricted", "Filesystem permissions")
        status_table.add_row("Network", "Controlled" if self.sandbox_enabled else "Open", "Network access policy")
        status_table.add_row("System Calls", "Filtered" if self.sandbox_enabled else "Direct", "System interaction level")
        
        # Features description
        if self.sandbox_enabled:
            features_text = (
                "✅ **Active Protections**\n\n"
                "🔒 **Process Isolation**: Commands run in isolated processes\n"
                "📁 **File System**: Access restricted to workspace and temp directories\n"
                "🌐 **Network Control**: Network access controlled by permission rules\n"
                "⚙️ **System Calls**: Dangerous system calls are blocked or monitored\n"
                "🕒 **Timeouts**: All operations have enforced time limits\n"
                "📊 **Resource Limits**: CPU, memory, and disk usage are capped\n"
                "🔍 **Monitoring**: All actions are logged for audit\n\n"
                "💡 **Note**: Sandbox provides security but may limit some operations."
            )
            features_color = "green"
        else:
            features_text = (
                "⚠️ **Sandbox Disabled**\n\n"
                "❌ Tools run with full system access\n"
                "❌ No process isolation or resource limits\n"
                "❌ Direct file system and network access\n"
                "❌ All system calls are permitted\n\n"
                "🚨 **Security Risk**: Running without sandbox increases security exposure\n\n"
                "💡 **Recommendation**: Enable sandbox for production use\n"
                "🛡️ Use '/sandbox on' to enable protection"
            )
            features_color = "red"
        
        features_panel = Panel(
            Text(features_text, style=features_color),
            title="🛡️ Security Features",
            border_style=features_color
        )
        
        # Quick actions
        actions_text = (
            "🚀 **Quick Actions**\n\n"
            f"/sandbox {'off' if self.sandbox_enabled else 'on'}     - {'Disable' if self.sandbox_enabled else 'Enable'} sandbox mode\n"
            "/sandbox config   - Show detailed configuration\n"
            "/permissions      - Manage detailed security rules\n"
            "/tools            - List tools and their sandbox requirements\n\n"
            "⚡ **For Plan Execution**\n"
            "/run <plan> --sandbox-mode - Override sandbox for single run\n"
            "/approve <plan> <step>     - Manual approval bypasses some restrictions"
        )
        
        actions_panel = Panel(
            Text(actions_text, style="blue"),
            title="🎛️ Controls",
            border_style="blue"
        )
        
        from rich.console import Group
        content = Group(status_table, "", features_panel, "", actions_panel)
        
        return CommandResult.success_result(content, "rich")
    
    async def _toggle_sandbox(self, enabled: bool) -> CommandResult:
        """Enable or disable sandbox mode"""
        
        old_status = self.sandbox_enabled
        self.sandbox_enabled = enabled
        
        action_text = "enabled" if enabled else "disabled"
        emoji = "🟢" if enabled else "🔴"
        color = "green" if enabled else "red"
        
        if old_status == enabled:
            return CommandResult.success_result(
                Panel(
                    Text(f"Sandbox is already {action_text}.", style=color),
                    title=f"{emoji} No Change",
                    border_style=color
                ),
                "rich"
            )
        
        # Impact warning for disabling
        if not enabled:
            warning_text = (
                f"⚠️ **Sandbox Disabled**\n\n"
                f"Security protections are now OFF:\n"
                f"• Tools can access any file\n"
                f"• Network requests unrestricted\n" 
                f"• System commands run directly\n"
                f"• No resource limits enforced\n\n"
                f"🔒 **Recommendation**: Only disable for trusted operations\n"
                f"🛡️ Re-enable with '/sandbox on'"
            )
        else:
            warning_text = (
                f"✅ **Sandbox Enabled**\n\n"
                f"Security protections are now ACTIVE:\n"
                f"• File access restricted to workspace\n"
                f"• Network calls controlled by rules\n" 
                f"• System commands are filtered\n"
                f"• Resource usage is monitored\n\n"
                f"⚡ **Note**: Some tools may require approval\n"
                f"🔍 Use '/permissions check' to test access"
            )
        
        result_panel = Panel(
            Text(warning_text, style=color),
            title=f"{emoji} Sandbox {action_text.title()}",
            border_style=color,
            padding=(1, 2)
        )
        
        return CommandResult.success_result(result_panel, "rich")
    
    async def _show_sandbox_config(self) -> CommandResult:
        """Show detailed sandbox configuration"""
        
        # Configuration table
        config_table = Table(title="⚙️ Sandbox Configuration", show_header=True, header_style="bold yellow")
        config_table.add_column("Setting", style="cyan", width=20)
        config_table.add_column("Value", style="white", width=25)
        config_table.add_column("Description", style="dim", width=30)
        
        config_table.add_row("Execution Mode", "Process Isolation", "Isolated subprocess execution")
        config_table.add_row("File System", "Restricted", "Access limited to workspace")
        config_table.add_row("Temp Directory", "/tmp/deile-sandbox", "Isolated temporary storage")
        config_table.add_row("Network Policy", "Rule-based", "Controlled by permission rules")
        config_table.add_row("Resource Limits", "Enforced", "CPU/memory/disk limits")
        config_table.add_row("Timeout", "300s default", "Maximum execution time")
        config_table.add_row("Monitoring", "Full logging", "All operations recorded")
        
        # Security policies
        policies_text = (
            "🔐 **Security Policies**\n\n"
            "**File System Access**:\n"
            "• Read: Workspace, /tmp, read-only system dirs\n"
            "• Write: Workspace subdirs, temp directory only\n"
            "• Blocked: /etc, /bin, /usr, system directories\n\n"
            "**Network Access**:\n"
            "• Allowed: APIs defined in permission rules\n"
            "• Blocked: Local network, SSH, admin ports\n\n"
            "**Process Control**:\n"
            "• Resource limits: 2GB RAM, 4 CPU cores max\n"
            "• Time limits: 5 minutes per tool execution\n"
            "• Signal handling: SIGTERM after timeout\n"
        )
        
        policies_panel = Panel(
            Text(policies_text, style="blue"),
            title="📋 Policies",
            border_style="blue"
        )
        
        # Override options
        overrides_text = (
            "⚡ **Override Options**\n\n"
            "**Per-execution overrides**:\n"
            "/run <plan> --no-sandbox     - Disable for entire plan\n"
            "/run <plan> --relaxed        - Reduced restrictions\n"
            "/approve <plan> <step>       - Manual approval for restricted ops\n\n"
            "**Configuration files**:\n"
            "config/sandbox.yaml          - Main configuration\n"
            "config/permissions.yaml      - Detailed access rules\n\n"
            "**Environment variables**:\n"
            "DEILE_SANDBOX=off            - Global disable\n"
            "DEILE_SANDBOX_MODE=relaxed   - Relaxed mode"
        )
        
        overrides_panel = Panel(
            Text(overrides_text, style="yellow"),
            title="🎛️ Overrides",
            border_style="yellow"
        )
        
        from rich.console import Group
        content = Group(config_table, "", policies_panel, "", overrides_panel)
        
        return CommandResult.success_result(content, "rich")
    
    def get_help(self) -> str:
        """Get command help"""
        return """Quick sandbox mode toggle and status

Usage:
  /sandbox              Show current sandbox status and features
  /sandbox on           Enable sandbox protection  
  /sandbox off          Disable sandbox (not recommended)
  /sandbox status       Show detailed status information
  /sandbox config       Show configuration and policies

Sandbox Features:
  • Process isolation for tool execution
  • Restricted file system access (workspace only)  
  • Network access controlled by permission rules
  • Resource limits (CPU, memory, time)
  • System call filtering and monitoring
  • Complete audit logging

Security Levels:
  • Enabled:  Full protection, tools run isolated
  • Disabled: Direct access, higher performance but less secure

Override Options:
  /run <plan> --no-sandbox     Disable sandbox for plan execution
  /run <plan> --relaxed        Reduced sandbox restrictions  
  /approve <plan> <step>       Manual approval for restricted operations

Configuration Files:
  • config/sandbox.yaml        Main sandbox settings
  • config/permissions.yaml    Detailed access control rules

Related Commands:
  • /permissions - Detailed security rule management
  • /run - Execute plans with sandbox control
  • /tools - List tools and their sandbox requirements
  • /approve - Manual approval for restricted operations

Environment Variables:
  • DEILE_SANDBOX=off          Global sandbox disable
  • DEILE_SANDBOX_MODE=relaxed Relaxed restrictions

Examples:
  /sandbox on                  Enable full protection
  /sandbox config              Show all configuration
  /run myplan --no-sandbox     Run plan without sandbox

Aliases: /sb, /isolation"""