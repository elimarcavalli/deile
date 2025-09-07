"""Git Tool - Integração completa com Git através do GitPython"""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

try:
    import git
    from git import Repo, InvalidGitRepositoryError, NoSuchPathError
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False
    git = None
    Repo = None
    InvalidGitRepositoryError = Exception
    NoSuchPathError = Exception

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..security.secrets_scanner import SecretsScanner


class GitTool(SyncTool):
    """Tool para operações Git completas com GitPython"""
    
    def __init__(self):
        super().__init__()
        self.secrets_scanner = SecretsScanner()
    
    @property
    def name(self) -> str:
        return "git"
    
    @property
    def description(self) -> str:
        return "Execute Git operations including status, diff, commit, push, pull, and branch management"
    
    @property
    def category(self) -> str:
        return "version_control"
    
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "status", "diff", "log", "add", "commit", "push", "pull",
                            "branch", "checkout", "stash", "reset", "remote", "tag",
                            "show", "blame", "merge", "rebase"
                        ],
                        "description": "Git operation to perform"
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files to operate on (for add, reset, etc.)"
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message (for commit action)"
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name (for branch, checkout actions)"
                    },
                    "remote": {
                        "type": "string",
                        "default": "origin",
                        "description": "Remote name (for push, pull actions)"
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force operation (use with caution)"
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Show what would be done without executing"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Limit number of entries (for log action)"
                    }
                },
                "required": ["action"]
            },
            "returns": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {"type": "object"},
                    "output": {"type": "string"},
                    "error": {"type": "string"}
                }
            },
            "side_effects": "May modify git repository state",
            "risk_level": "medium",
            "display_policy": "both"
        }
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute git operations"""
        if not GIT_AVAILABLE:
            return ToolResult(
                status=ToolStatus.ERROR,
                data={"error": "GitPython not available"},
                message="Git tool requires GitPython package. Install with: pip install GitPython",
                display_policy=DisplayPolicy.BOTH
            )
        
        try:
            # Extract parameters
            args = context.parsed_args
            action = args.get("action", "status")
            working_dir = context.working_directory
            
            # Find git repository
            repo = self._get_repo(working_dir)
            if not repo:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    data={"error": "Not a git repository"},
                    message=f"No git repository found in {working_dir} or parent directories",
                    display_policy=DisplayPolicy.BOTH
                )
            
            # Execute the requested action
            result = self._execute_action(repo, action, args)
            
            return ToolResult(
                status=ToolStatus.SUCCESS if result["success"] else ToolStatus.ERROR,
                data=result["data"],
                message=result.get("output", result.get("error", "Git operation completed")),
                display_policy=DisplayPolicy.BOTH
            )
            
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                data={"error": str(e)},
                message=f"Git operation failed: {str(e)}",
                display_policy=DisplayPolicy.BOTH
            )
    
    def _get_repo(self, working_dir: str) -> Optional[Repo]:
        """Find git repository in working directory or parents"""
        try:
            return Repo(working_dir, search_parent_directories=True)
        except (InvalidGitRepositoryError, NoSuchPathError):
            return None
    
    def _execute_action(self, repo: Repo, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute specific git action"""
        try:
            if action == "status":
                return self._git_status(repo)
            elif action == "diff":
                return self._git_diff(repo, args)
            elif action == "log":
                return self._git_log(repo, args)
            elif action == "add":
                return self._git_add(repo, args)
            elif action == "commit":
                return self._git_commit(repo, args)
            elif action == "push":
                return self._git_push(repo, args)
            elif action == "pull":
                return self._git_pull(repo, args)
            elif action == "branch":
                return self._git_branch(repo, args)
            elif action == "checkout":
                return self._git_checkout(repo, args)
            elif action == "stash":
                return self._git_stash(repo, args)
            elif action == "reset":
                return self._git_reset(repo, args)
            elif action == "remote":
                return self._git_remote(repo)
            elif action == "tag":
                return self._git_tag(repo, args)
            elif action == "show":
                return self._git_show(repo, args)
            elif action == "blame":
                return self._git_blame(repo, args)
            elif action == "merge":
                return self._git_merge(repo, args)
            elif action == "rebase":
                return self._git_rebase(repo, args)
            else:
                return {
                    "success": False,
                    "error": f"Unknown git action: {action}",
                    "data": {}
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "data": {}
            }
    
    def _git_status(self, repo: Repo) -> Dict[str, Any]:
        """Get repository status"""
        status_data = {
            "branch": repo.active_branch.name,
            "commit": repo.head.commit.hexsha[:8],
            "modified": [item.a_path for item in repo.index.diff(None)],
            "staged": [item.a_path for item in repo.index.diff("HEAD")],
            "untracked": repo.untracked_files,
            "remote_behind": 0,
            "remote_ahead": 0
        }
        
        # Check remote status
        try:
            origin = repo.remote("origin")
            origin.fetch()
            status_data["remote_behind"] = len(list(repo.iter_commits(f'{repo.active_branch}..origin/{repo.active_branch}')))
            status_data["remote_ahead"] = len(list(repo.iter_commits(f'origin/{repo.active_branch}..{repo.active_branch}')))
        except:
            pass
        
        # Format output
        output_lines = [
            f"On branch {status_data['branch']}",
            f"Last commit: {status_data['commit']}"
        ]
        
        if status_data["remote_behind"] > 0:
            output_lines.append(f"Your branch is behind by {status_data['remote_behind']} commits")
        if status_data["remote_ahead"] > 0:
            output_lines.append(f"Your branch is ahead by {status_data['remote_ahead']} commits")
        
        if status_data["staged"]:
            output_lines.append("\nChanges staged for commit:")
            for file in status_data["staged"]:
                output_lines.append(f"  modified: {file}")
        
        if status_data["modified"]:
            output_lines.append("\nChanges not staged for commit:")
            for file in status_data["modified"]:
                output_lines.append(f"  modified: {file}")
        
        if status_data["untracked"]:
            output_lines.append("\nUntracked files:")
            for file in status_data["untracked"]:
                output_lines.append(f"  {file}")
        
        if not any([status_data["staged"], status_data["modified"], status_data["untracked"]]):
            output_lines.append("\nNothing to commit, working tree clean")
        
        return {
            "success": True,
            "data": status_data,
            "output": "\n".join(output_lines)
        }
    
    def _git_diff(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get repository diff"""
        files = args.get("files", [])
        
        if files:
            diff_output = ""
            for file in files:
                try:
                    diff = repo.git.diff("HEAD", file)
                    if diff:
                        diff_output += f"\n--- {file} ---\n{diff}\n"
                except:
                    diff_output += f"\n--- {file} ---\nFile not found or no diff\n"
        else:
            # Get all diffs
            staged_diff = repo.git.diff("--cached")
            unstaged_diff = repo.git.diff()
            
            diff_output = ""
            if staged_diff:
                diff_output += "=== Staged Changes ===\n" + staged_diff + "\n\n"
            if unstaged_diff:
                diff_output += "=== Unstaged Changes ===\n" + unstaged_diff + "\n"
            
            if not staged_diff and not unstaged_diff:
                diff_output = "No changes to show"
        
        return {
            "success": True,
            "data": {"diff": diff_output},
            "output": diff_output
        }
    
    def _git_log(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get commit log"""
        limit = args.get("limit", 10)
        commits = []
        output_lines = []
        
        for commit in repo.iter_commits(max_count=limit):
            commit_data = {
                "hash": commit.hexsha[:8],
                "message": commit.message.strip(),
                "author": commit.author.name,
                "date": commit.authored_datetime.isoformat(),
                "files_changed": len(commit.stats.files)
            }
            commits.append(commit_data)
            
            output_lines.append(f"commit {commit_data['hash']}")
            output_lines.append(f"Author: {commit_data['author']}")
            output_lines.append(f"Date: {commit.authored_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
            output_lines.append(f"    {commit_data['message']}")
            output_lines.append(f"    ({commit_data['files_changed']} files changed)")
            output_lines.append("")
        
        return {
            "success": True,
            "data": {"commits": commits},
            "output": "\n".join(output_lines)
        }
    
    def _git_add(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Add files to staging"""
        files = args.get("files", [])
        dry_run = args.get("dry_run", False)
        
        if not files:
            return {
                "success": False,
                "error": "No files specified for git add",
                "data": {}
            }
        
        if dry_run:
            return {
                "success": True,
                "data": {"files": files},
                "output": f"Would add files: {', '.join(files)}"
            }
        
        try:
            repo.index.add(files)
            return {
                "success": True,
                "data": {"files": files},
                "output": f"Added files: {', '.join(files)}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to add files: {str(e)}",
                "data": {}
            }
    
    def _git_commit(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Create a commit"""
        message = args.get("message", "")
        dry_run = args.get("dry_run", False)
        
        if not message:
            return {
                "success": False,
                "error": "Commit message is required",
                "data": {}
            }
        
        # Scan message for secrets
        if self.secrets_scanner.scan_text(message):
            return {
                "success": False,
                "error": "Commit message contains potential secrets",
                "data": {}
            }
        
        if dry_run:
            staged_files = [item.a_path for item in repo.index.diff("HEAD")]
            return {
                "success": True,
                "data": {"message": message, "files": staged_files},
                "output": f"Would commit {len(staged_files)} files with message: {message}"
            }
        
        try:
            # Check if there are staged changes
            staged_files = [item.a_path for item in repo.index.diff("HEAD")]
            if not staged_files:
                return {
                    "success": False,
                    "error": "No staged changes to commit",
                    "data": {}
                }
            
            commit = repo.index.commit(message)
            return {
                "success": True,
                "data": {
                    "hash": commit.hexsha[:8],
                    "message": message,
                    "files": staged_files
                },
                "output": f"Created commit {commit.hexsha[:8]}: {message}"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to create commit: {str(e)}",
                "data": {}
            }
    
    def _git_push(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Push changes to remote"""
        remote_name = args.get("remote", "origin")
        force = args.get("force", False)
        dry_run = args.get("dry_run", False)
        
        try:
            remote = repo.remote(remote_name)
            branch = repo.active_branch.name
            
            if dry_run:
                return {
                    "success": True,
                    "data": {"remote": remote_name, "branch": branch},
                    "output": f"Would push {branch} to {remote_name}"
                }
            
            # Check if push is needed
            try:
                remote.fetch()
                commits_ahead = len(list(repo.iter_commits(f'{remote_name}/{branch}..{branch}')))
                if commits_ahead == 0:
                    return {
                        "success": True,
                        "data": {"remote": remote_name, "branch": branch},
                        "output": "Everything up-to-date"
                    }
            except:
                pass
            
            # Perform push
            if force:
                info = remote.push(branch, force=True)[0]
            else:
                info = remote.push(branch)[0]
            
            return {
                "success": True,
                "data": {
                    "remote": remote_name,
                    "branch": branch,
                    "summary": str(info.summary)
                },
                "output": f"Pushed {branch} to {remote_name}: {info.summary}"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to push: {str(e)}",
                "data": {}
            }
    
    def _git_pull(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Pull changes from remote"""
        remote_name = args.get("remote", "origin")
        dry_run = args.get("dry_run", False)
        
        try:
            remote = repo.remote(remote_name)
            branch = repo.active_branch.name
            
            if dry_run:
                remote.fetch()
                commits_behind = len(list(repo.iter_commits(f'{branch}..{remote_name}/{branch}')))
                return {
                    "success": True,
                    "data": {"remote": remote_name, "branch": branch, "commits_behind": commits_behind},
                    "output": f"Would pull {commits_behind} commits from {remote_name}/{branch}"
                }
            
            # Perform pull
            info = remote.pull()[0]
            
            return {
                "success": True,
                "data": {
                    "remote": remote_name,
                    "branch": branch,
                    "summary": str(info)
                },
                "output": f"Pulled from {remote_name}/{branch}: {str(info)}"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to pull: {str(e)}",
                "data": {}
            }
    
    def _git_branch(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Branch operations"""
        branch_name = args.get("branch")
        
        if not branch_name:
            # List branches
            branches = []
            current_branch = repo.active_branch.name
            
            for branch in repo.branches:
                branches.append({
                    "name": branch.name,
                    "current": branch.name == current_branch,
                    "commit": branch.commit.hexsha[:8]
                })
            
            output_lines = []
            for branch in branches:
                prefix = "* " if branch["current"] else "  "
                output_lines.append(f"{prefix}{branch['name']} ({branch['commit']})")
            
            return {
                "success": True,
                "data": {"branches": branches, "current": current_branch},
                "output": "\n".join(output_lines)
            }
        else:
            # Create new branch
            try:
                new_branch = repo.create_head(branch_name)
                return {
                    "success": True,
                    "data": {"branch": branch_name},
                    "output": f"Created branch '{branch_name}'"
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to create branch: {str(e)}",
                    "data": {}
                }
    
    def _git_checkout(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Checkout branch or commit"""
        target = args.get("branch")
        
        if not target:
            return {
                "success": False,
                "error": "Branch or commit hash required for checkout",
                "data": {}
            }
        
        try:
            # Check if target exists as branch
            if target in [branch.name for branch in repo.branches]:
                repo.git.checkout(target)
                return {
                    "success": True,
                    "data": {"target": target, "type": "branch"},
                    "output": f"Switched to branch '{target}'"
                }
            else:
                # Try as commit hash
                repo.git.checkout(target)
                return {
                    "success": True,
                    "data": {"target": target, "type": "commit"},
                    "output": f"Checked out commit '{target}'"
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to checkout '{target}': {str(e)}",
                "data": {}
            }
    
    def _git_stash(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Stash operations"""
        try:
            stashes = repo.git.stash("list").split("\n") if repo.git.stash("list") else []
            
            return {
                "success": True,
                "data": {"stashes": stashes},
                "output": f"Stash list:\n" + "\n".join(stashes) if stashes else "No stashes found"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Stash operation failed: {str(e)}",
                "data": {}
            }
    
    def _git_reset(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Reset operations"""
        files = args.get("files", [])
        force = args.get("force", False)
        
        if not files:
            return {
                "success": False,
                "error": "Files required for reset operation",
                "data": {}
            }
        
        try:
            if force:
                repo.git.reset("--hard", *files)
                action = "hard reset"
            else:
                repo.git.reset(*files)
                action = "reset"
            
            return {
                "success": True,
                "data": {"files": files, "action": action},
                "output": f"Performed {action} on: {', '.join(files)}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Reset failed: {str(e)}",
                "data": {}
            }
    
    def _git_remote(self, repo: Repo) -> Dict[str, Any]:
        """List remotes"""
        try:
            remotes = []
            for remote in repo.remotes:
                remotes.append({
                    "name": remote.name,
                    "url": list(remote.urls)[0] if remote.urls else "No URL"
                })
            
            output_lines = []
            for remote in remotes:
                output_lines.append(f"{remote['name']}\t{remote['url']}")
            
            return {
                "success": True,
                "data": {"remotes": remotes},
                "output": "\n".join(output_lines) if remotes else "No remotes configured"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to list remotes: {str(e)}",
                "data": {}
            }
    
    def _git_tag(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Tag operations"""
        try:
            tags = [tag.name for tag in repo.tags]
            tags.sort(reverse=True)
            
            return {
                "success": True,
                "data": {"tags": tags},
                "output": "\n".join(tags) if tags else "No tags found"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Tag operation failed: {str(e)}",
                "data": {}
            }
    
    def _git_show(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Show commit details"""
        try:
            commit_hash = args.get("commit", "HEAD")
            commit = repo.commit(commit_hash)
            
            commit_info = {
                "hash": commit.hexsha,
                "short_hash": commit.hexsha[:8],
                "message": commit.message.strip(),
                "author": commit.author.name,
                "date": commit.authored_datetime.isoformat(),
                "files": list(commit.stats.files.keys())
            }
            
            output_lines = [
                f"commit {commit_info['hash']}",
                f"Author: {commit_info['author']}",
                f"Date: {commit.authored_datetime.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                f"    {commit_info['message']}",
                "",
                f"Files changed ({len(commit_info['files'])}):"
            ]
            
            for file in commit_info['files'][:10]:  # Limit to 10 files
                output_lines.append(f"    {file}")
            
            if len(commit_info['files']) > 10:
                output_lines.append(f"    ... and {len(commit_info['files']) - 10} more files")
            
            return {
                "success": True,
                "data": commit_info,
                "output": "\n".join(output_lines)
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Show operation failed: {str(e)}",
                "data": {}
            }
    
    def _git_blame(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Git blame for file"""
        files = args.get("files", [])
        
        if not files:
            return {
                "success": False,
                "error": "File required for blame operation",
                "data": {}
            }
        
        try:
            file_path = files[0]
            blame_output = repo.git.blame(file_path)
            
            return {
                "success": True,
                "data": {"file": file_path, "blame": blame_output},
                "output": f"Blame for {file_path}:\n{blame_output}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Blame operation failed: {str(e)}",
                "data": {}
            }
    
    def _git_merge(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Merge branch"""
        branch = args.get("branch")
        dry_run = args.get("dry_run", False)
        
        if not branch:
            return {
                "success": False,
                "error": "Branch required for merge operation",
                "data": {}
            }
        
        try:
            if dry_run:
                # Check if merge is possible
                return {
                    "success": True,
                    "data": {"branch": branch},
                    "output": f"Would merge branch '{branch}' into current branch"
                }
            
            # Perform merge
            repo.git.merge(branch)
            
            return {
                "success": True,
                "data": {"branch": branch},
                "output": f"Merged branch '{branch}' into current branch"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Merge failed: {str(e)}",
                "data": {}
            }
    
    def _git_rebase(self, repo: Repo, args: Dict[str, Any]) -> Dict[str, Any]:
        """Rebase operations"""
        branch = args.get("branch", "origin/main")
        dry_run = args.get("dry_run", False)
        
        try:
            if dry_run:
                return {
                    "success": True,
                    "data": {"branch": branch},
                    "output": f"Would rebase current branch onto '{branch}'"
                }
            
            # Perform rebase
            repo.git.rebase(branch)
            
            return {
                "success": True,
                "data": {"branch": branch},
                "output": f"Rebased current branch onto '{branch}'"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Rebase failed: {str(e)}",
                "data": {}
            }


if __name__ == "__main__":
    # Test básico
    tool = GitTool()
    print("Git Tool loaded successfully")
    print("Schema:", tool.get_schema())