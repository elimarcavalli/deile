"""Testes para o comando /loc."""

from unittest.mock import MagicMock, patch

import pytest

from deile.commands.base import CommandContext, CommandStatus
from deile.commands.builtin.loc_command import LocCommand


@pytest.fixture
def loc_command():
    return LocCommand()


@pytest.mark.asyncio
async def test_loc_command_execute(loc_command, tmp_path):
    # Setup a fake repository
    repo_dir = tmp_path / "fake_repo"
    repo_dir.mkdir()

    # Create some fake files
    (repo_dir / "file1.py").write_text("print('hello')\nprint('world')\n")
    (repo_dir / "file2.md").write_text("# Title\n\nSome text\n")
    (repo_dir / "file3.yaml").write_text("key: value\n")

    # Create a fake test file
    test_dir = repo_dir / "deile" / "tests"
    test_dir.mkdir(parents=True)
    (test_dir / "test_fake.py").write_text(
        "def test_something():\n    pass\nasync def test_async_something():\n    pass\n"
    )

    # Mock git ls-files
    def mock_run(*args, **kwargs):
        mock_result = MagicMock()
        mock_result.stdout = (
            "file1.py\nfile2.md\nfile3.yaml\ndeile/tests/test_fake.py\n"
        )
        return mock_result

    with patch("subprocess.run", side_effect=mock_run):
        ctx = CommandContext(user_input="/loc", working_directory=str(repo_dir))
        result = await loc_command.execute(ctx)

        assert result.status == CommandStatus.SUCCESS
        assert result.content_type == "rich"

        # Check metadata
        assert result.metadata["total_files"] == 4
        assert result.metadata["total_lines"] == 10
        assert result.metadata["total_tests"] == 2

        # Check lang_stats
        lang_stats = result.metadata["lang_stats"]
        assert lang_stats["Python"]["files"] == 2
        assert lang_stats["Python"]["lines"] == 6
        assert lang_stats["Markdown"]["files"] == 1
        assert lang_stats["Markdown"]["lines"] == 3
        assert lang_stats["YAML"]["files"] == 1
        assert lang_stats["YAML"]["lines"] == 1

        # Check top_files
        top_files = result.metadata["top_files"]
        assert len(top_files) == 4
        assert top_files[0] == ("deile/tests/test_fake.py", 4)
        assert top_files[1] == ("file2.md", 3)
        assert top_files[2] == ("file1.py", 2)
        assert top_files[3] == ("file3.yaml", 1)


@pytest.mark.asyncio
async def test_loc_command_git_error(loc_command, tmp_path):
    import subprocess

    def mock_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, "git")

    with patch("subprocess.run", side_effect=mock_run):
        ctx = CommandContext(user_input="/loc", working_directory=str(tmp_path))
        result = await loc_command.execute(ctx)

        assert result.status == CommandStatus.SUCCESS
        assert result.metadata["total_files"] == 0
        assert result.metadata["total_lines"] == 0
        assert result.metadata["total_tests"] == 0


def test_get_language(loc_command):
    assert loc_command._get_language("test.py") == "Python"
    assert loc_command._get_language("README.md") == "Markdown"
    assert loc_command._get_language("config.yaml") == "YAML"
    assert loc_command._get_language("config.yml") == "YAML"
    assert loc_command._get_language("data.json") == "JSON"
    assert loc_command._get_language("script.sh") == "Shell"
    assert loc_command._get_language("unknown.txt") == "Other"
