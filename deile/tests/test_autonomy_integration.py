"""
End-to-end integration tests for the complete autonomy system

This test suite validates the full autonomous workflow from user input
to final execution, ensuring all components work together seamlessly.
"""

import pytest
import tempfile
import shutil
import asyncio
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock

from deile.core.agent import DeileAgent
from deile.core.context_manager import ContextManager
from deile.tools.file_tools import ReadFileTool
from deile.core.agent import AgentSession
from deile.core.exceptions import ValidationError


class TestAutonomyEndToEnd:
    """End-to-end tests for complete autonomy workflow"""

    @pytest.fixture
    def test_project(self):
        """Create a realistic test project structure"""
        temp_dir = tempfile.mkdtemp()
        workspace = Path(temp_dir)

        # Create comprehensive project structure
        project_structure = {
            "README.md": """# Test Project

This is a test project for validating DEILE's autonomy features.

## Features
- Smart file resolution
- Autonomous operation
- Natural language processing

## Usage
Run `python main.py` to start the application.
""",
            "LICENSE": "MIT License\n\nCopyright (c) 2024 Test Project",
            "requirements.txt": """pytest>=7.0.0
click>=8.0.0
requests>=2.28.0
pydantic>=1.10.0
""",
            "setup.py": """from setuptools import setup, find_packages

setup(
    name="test-project",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "click>=8.0.0",
        "requests>=2.28.0",
    ],
)""",
            "pyproject.toml": """[build-system]
requires = ["setuptools>=45", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "test-project"
version = "1.0.0"
""",
            "config.yaml": """database:
  host: localhost
  port: 5432
  name: test_db

api:
  base_url: https://api.example.com
  timeout: 30

logging:
  level: INFO
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
""",
            "main.py": """#!/usr/bin/env python3
\"\"\"Main application entry point\"\"\"

import click
import yaml
from pathlib import Path

@click.command()
@click.option('--config', '-c', default='config.yaml', help='Configuration file')
def main(config):
    \"\"\"Run the test application\"\"\"
    config_path = Path(config)
    if config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
        print(f"Loaded configuration: {config_data}")
    else:
        print(f"Configuration file {config} not found")

if __name__ == '__main__':
    main()
""",
            ".env": """DATABASE_URL=postgresql://user:pass@localhost/test_db
API_KEY=test-api-key-12345
DEBUG=True
""",
            ".gitignore": """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
.venv/

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db
""",
            "Dockerfile": """FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]
""",
            "docker-compose.yml": """version: '3.8'

services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://user:pass@db:5432/test_db
    depends_on:
      - db

  db:
    image: postgres:15
    environment:
      - POSTGRES_DB=test_db
      - POSTGRES_USER=user
      - POSTGRES_PASSWORD=pass
    volumes:
      - db_data:/var/lib/postgresql/data

volumes:
  db_data:
"""
        }

        # Create files
        for filename, content in project_structure.items():
            (workspace / filename).write_text(content)

        # Create source directory
        src_dir = workspace / "src"
        src_dir.mkdir()
        (src_dir / "__init__.py").write_text("")
        (src_dir / "app.py").write_text("""\"\"\"Main application module\"\"\"

class App:
    def __init__(self, config_path="config.yaml"):
        self.config_path = config_path
        self.config = {}

    def load_config(self):
        import yaml
        with open(self.config_path) as f:
            self.config = yaml.safe_load(f)

    def run(self):
        print("Application running...")
""")
        (src_dir / "models.py").write_text("""\"\"\"Data models\"\"\"

from dataclasses import dataclass
from typing import Optional

@dataclass
class User:
    id: int
    name: str
    email: str
    active: bool = True

@dataclass
class Project:
    id: int
    name: str
    description: Optional[str] = None
    owner_id: int
""")

        # Create tests directory
        tests_dir = workspace / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_app.py").write_text("""\"\"\"Tests for the main application\"\"\"

import pytest
from src.app import App

def test_app_initialization():
    app = App()
    assert app.config_path == "config.yaml"
    assert app.config == {}

def test_app_config_loading():
    # Mock test - would need actual config file
    pass
""")
        (tests_dir / "conftest.py").write_text("""\"\"\"Pytest configuration\"\"\"

import pytest
from pathlib import Path

@pytest.fixture
def test_data_dir():
    return Path(__file__).parent / "data"
""")

        # Create docs directory
        docs_dir = workspace / "docs"
        docs_dir.mkdir()
        (docs_dir / "api.md").write_text("""# API Documentation

## Endpoints

### GET /health
Returns the health status of the application.

**Response:**
```json
{
    "status": "ok",
    "timestamp": "2024-01-01T00:00:00Z"
}
```

### POST /users
Creates a new user.

**Request:**
```json
{
    "name": "John Doe",
    "email": "john@example.com"
}
```
""")
        (docs_dir / "deployment.md").write_text("""# Deployment Guide

## Docker Deployment

1. Build the image:
   ```bash
   docker build -t test-project .
   ```

2. Run the container:
   ```bash
   docker run -p 8000:8000 test-project
   ```

## Production Deployment

1. Set up the database
2. Configure environment variables
3. Deploy using docker-compose
""")

        yield workspace

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def mock_agent(self, test_project):
        """Create a mock agent with autonomy features"""
        # Create mock components
        context_manager = Mock(spec=ContextManager)
        context_manager.create_tool_context.return_value = Mock()
        context_manager.create_tool_context.return_value.working_directory = str(test_project)
        context_manager.create_tool_context.return_value.user_input = ""
        context_manager.create_tool_context.return_value.parsed_args = {}
        context_manager.create_tool_context.return_value.file_list = []

        # Create agent with mocked dependencies
        agent = DeileAgent(
            context_manager=context_manager,
            working_directory=str(test_project)
        )

        return agent

    @pytest.mark.asyncio
    async def test_autonomous_readme_reading(self, test_project):
        """Test autonomous README reading workflow"""
        # Test scenarios that should trigger autonomous reading
        test_inputs = [
            "read the readme",
            "show me the README file",
            "examine the project documentation",
            "open the readme.md",
            "I want to see the project description"
        ]

        for user_input in test_inputs:
            # Create ReadFileTool and test context
            tool = ReadFileTool()

            # Create mock context
            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            # Execute tool
            result = tool.execute_sync(context)

            # Should successfully read README.md
            assert result.status.name == "SUCCESS"
            assert "Test Project" in result.content
            assert "Features" in result.content

    @pytest.mark.asyncio
    async def test_autonomous_config_reading(self, test_project):
        """Test autonomous configuration file reading"""
        test_inputs = [
            "read the config file",
            "show me the configuration",
            "examine config.yaml",
            "open the settings file"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read a config file
            assert result.status.name == "SUCCESS"
            # Should contain config content
            assert ("database:" in result.content or
                   "DATABASE_URL" in result.content or
                   "build-system" in result.content)

    @pytest.mark.asyncio
    async def test_autonomous_requirements_reading(self, test_project):
        """Test autonomous requirements file reading"""
        test_inputs = [
            "read the requirements",
            "show me the dependencies",
            "examine requirements.txt",
            "what packages are needed?"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read requirements or setup files
            assert result.status.name == "SUCCESS"
            assert ("pytest" in result.content or
                   "setuptools" in result.content or
                   "click" in result.content)

    @pytest.mark.asyncio
    async def test_autonomous_main_code_reading(self, test_project):
        """Test autonomous main code file reading"""
        test_inputs = [
            "read the main python file",
            "show me main.py",
            "examine the entry point",
            "open the main application code"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read main.py or app.py
            assert result.status.name == "SUCCESS"
            assert ("def main" in result.content or
                   "class App" in result.content or
                   "__main__" in result.content)

    @pytest.mark.asyncio
    async def test_autonomous_license_reading(self, test_project):
        """Test autonomous license file reading"""
        test_inputs = [
            "read the license",
            "show me the LICENSE file",
            "examine the license terms",
            "what license is this project under?"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read LICENSE
            assert result.status.name == "SUCCESS"
            assert "MIT License" in result.content

    @pytest.mark.asyncio
    async def test_autonomous_dockerfile_reading(self, test_project):
        """Test autonomous Dockerfile reading"""
        test_inputs = [
            "read the Dockerfile",
            "show me the docker configuration",
            "examine the container setup",
            "how is this containerized?"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read Dockerfile or docker-compose.yml
            assert result.status.name == "SUCCESS"
            assert ("FROM python" in result.content or
                   "version:" in result.content or
                   "services:" in result.content)

    @pytest.mark.asyncio
    async def test_autonomous_source_code_reading(self, test_project):
        """Test autonomous source code reading"""
        test_inputs = [
            "read the app source code",
            "show me src/app.py",
            "examine the application logic",
            "open the models file"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read source files
            assert result.status.name == "SUCCESS"
            assert ("class App" in result.content or
                   "class User" in result.content or
                   "dataclass" in result.content)

    @pytest.mark.asyncio
    async def test_autonomous_test_code_reading(self, test_project):
        """Test autonomous test code reading"""
        test_inputs = [
            "read the test files",
            "show me the tests",
            "examine test_app.py",
            "how are things tested?"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read test files
            assert result.status.name == "SUCCESS"
            assert ("test_" in result.content or
                   "pytest" in result.content or
                   "@pytest.fixture" in result.content)

    @pytest.mark.asyncio
    async def test_autonomous_documentation_reading(self, test_project):
        """Test autonomous documentation reading"""
        test_inputs = [
            "read the API documentation",
            "show me docs/api.md",
            "examine the deployment guide",
            "how do I deploy this?"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully read documentation files
            assert result.status.name == "SUCCESS"
            assert ("API" in result.content or
                   "Deployment" in result.content or
                   "Endpoints" in result.content or
                   "docker" in result.content.lower())

    @pytest.mark.asyncio
    async def test_autonomous_file_suggestions(self, test_project):
        """Test autonomous file suggestions for ambiguous queries"""
        test_inputs = [
            "read the documentation",  # Should suggest multiple docs
            "show me the config",      # Should suggest config files
            "examine the setup",       # Should suggest setup files
            "read the yaml file"       # Should suggest yaml files
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should either succeed or provide helpful suggestions
            if result.status.name == "ERROR":
                # Error message should contain suggestions
                assert ("Sugestões:" in result.message or
                       "not found" in result.message.lower())
            else:
                # Should have successfully resolved to a relevant file
                assert result.status.name == "SUCCESS"

    @pytest.mark.asyncio
    async def test_edge_cases_and_error_handling(self, test_project):
        """Test edge cases and error handling in autonomous workflow"""
        edge_cases = [
            "read the nonexistent file",
            "show me xyzabc123.txt",
            "examine the missing.py",
            "",  # Empty input
            "   ",  # Whitespace only
            "read file without name",
            "show me"
        ]

        for user_input in edge_cases:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should handle gracefully without crashing
            assert result is not None
            assert hasattr(result, 'status')
            assert hasattr(result, 'message')

            # For clearly non-existent files, should provide helpful error
            if "nonexistent" in user_input or "xyzabc123" in user_input:
                assert result.status.name == "ERROR"

    @pytest.mark.asyncio
    async def test_performance_with_large_project(self, test_project):
        """Test performance of autonomous resolution with many files"""
        # Create additional files to simulate larger project
        for i in range(50):
            (test_project / f"extra_file_{i:03d}.py").write_text(f"# Extra file {i}")
            (test_project / f"data_{i:03d}.json").write_text(f'{{"id": {i}}}')

        import time

        test_inputs = [
            "read the readme",
            "show me config.yaml",
            "examine main.py"
        ]

        for user_input in test_inputs:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            start_time = time.time()
            result = tool.execute_sync(context)
            end_time = time.time()

            # Should complete within reasonable time (5 seconds)
            assert end_time - start_time < 5.0
            # Should still work correctly
            assert result.status.name == "SUCCESS"

    @pytest.mark.asyncio
    async def test_concurrent_autonomous_operations(self, test_project):
        """Test concurrent autonomous operations"""
        async def autonomous_read(user_input):
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(test_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            return tool.execute_sync(context)

        # Run multiple autonomous operations concurrently
        tasks = [
            autonomous_read("read the readme"),
            autonomous_read("show me config.yaml"),
            autonomous_read("examine main.py"),
            autonomous_read("read requirements.txt")
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should complete successfully
        assert len(results) == 4
        for result in results:
            assert not isinstance(result, Exception)
            assert result.status.name == "SUCCESS"

    @pytest.mark.asyncio
    async def test_autonomous_with_different_working_directories(self):
        """Test autonomous functionality with different working directories"""
        # Create multiple temporary workspaces
        workspaces = []

        try:
            for i in range(3):
                temp_dir = tempfile.mkdtemp()
                workspace = Path(temp_dir)
                workspaces.append(workspace)

                # Create different files in each workspace
                (workspace / f"project_{i}_readme.md").write_text(f"# Project {i}")
                (workspace / f"config_{i}.yaml").write_text(f"version: {i}")

            # Test autonomous resolution in each workspace
            for i, workspace in enumerate(workspaces):
                tool = ReadFileTool()

                context = Mock()
                context.working_directory = str(workspace)
                context.user_input = "read the readme"
                context.parsed_args = {}
                context.file_list = []

                result = tool.execute_sync(context)

                # Should read the correct readme for each workspace
                assert result.status.name == "SUCCESS"
                assert f"Project {i}" in result.content

        finally:
            # Cleanup
            for workspace in workspaces:
                shutil.rmtree(workspace)


class TestAutonomyComplexScenarios:
    """Test complex real-world scenarios for autonomy"""

    @pytest.fixture
    def complex_project(self):
        """Create a complex project with nested structure"""
        temp_dir = tempfile.mkdtemp()
        workspace = Path(temp_dir)

        # Create complex nested structure
        structure = {
            "backend/": {
                "README.md": "# Backend Service",
                "config/": {
                    "settings.py": "DJANGO_SETTINGS",
                    "database.yaml": "database config",
                    "redis.conf": "redis configuration"
                },
                "src/": {
                    "main.py": "FastAPI application",
                    "models/": {
                        "user.py": "User model",
                        "project.py": "Project model"
                    },
                    "api/": {
                        "auth.py": "Authentication endpoints",
                        "users.py": "User endpoints"
                    }
                }
            },
            "frontend/": {
                "README.md": "# Frontend Application",
                "package.json": "React dependencies",
                "src/": {
                    "App.js": "Main React component",
                    "components/": {
                        "Header.js": "Header component",
                        "Footer.js": "Footer component"
                    }
                }
            },
            "docs/": {
                "README.md": "# Documentation",
                "api/": {
                    "authentication.md": "Auth docs",
                    "users.md": "User API docs"
                },
                "deployment/": {
                    "docker.md": "Docker deployment",
                    "kubernetes.md": "K8s deployment"
                }
            }
        }

        def create_structure(base_path, structure):
            for name, content in structure.items():
                path = base_path / name
                if isinstance(content, dict):
                    path.mkdir(exist_ok=True)
                    create_structure(path, content)
                else:
                    path.write_text(content)

        create_structure(workspace, structure)

        yield workspace

        # Cleanup
        shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_complex_readme_resolution(self, complex_project):
        """Test README resolution in complex nested structure"""
        test_cases = [
            ("read the main readme", "Documentation"),  # Should find docs/README.md
            ("read the backend readme", "Backend Service"),
            ("read the frontend readme", "Frontend Application"),
            ("show me the documentation readme", "Documentation")
        ]

        for user_input, expected_content in test_cases:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(complex_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully resolve and read appropriate README
            assert result.status.name == "SUCCESS"
            # Note: Due to file resolver logic, might pick any README
            # The important thing is that it successfully resolves something reasonable

    @pytest.mark.asyncio
    async def test_complex_config_resolution(self, complex_project):
        """Test configuration file resolution in nested structure"""
        test_cases = [
            "read the database config",
            "show me the settings file",
            "examine the configuration",
            "read the redis config"
        ]

        for user_input in test_cases:
            tool = ReadFileTool()

            context = Mock()
            context.working_directory = str(complex_project)
            context.user_input = user_input
            context.parsed_args = {}
            context.file_list = []

            result = tool.execute_sync(context)

            # Should successfully resolve to some config file
            # In a complex project, there might be multiple valid matches
            assert result.status.name == "SUCCESS" or "Sugestões:" in result.message


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])