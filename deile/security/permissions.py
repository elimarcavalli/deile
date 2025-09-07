"""Permission System for DEILE"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Pattern
from enum import Enum
import re
import yaml
from pathlib import Path
import logging


logger = logging.getLogger(__name__)


class PermissionLevel(Enum):
    """Permission levels"""
    NONE = "none"
    READ = "read" 
    WRITE = "write"
    EXECUTE = "execute"
    ADMIN = "admin"


class ResourceType(Enum):
    """Types of resources that can be protected"""
    FILE = "file"
    DIRECTORY = "directory"
    COMMAND = "command"
    NETWORK = "network"
    SYSTEM = "system"


@dataclass
class PermissionRule:
    """Single permission rule"""
    id: str
    name: str
    description: str
    resource_type: ResourceType
    resource_pattern: str  # Regex pattern
    tool_names: List[str]
    permission_level: PermissionLevel
    conditions: Dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True
    
    def __post_init__(self):
        """Compile regex pattern after initialization"""
        try:
            self.compiled_pattern = re.compile(self.resource_pattern)
        except re.error as e:
            logger.error(f"Invalid regex pattern in rule {self.id}: {e}")
            self.compiled_pattern = None
            
    def matches_resource(self, resource: str) -> bool:
        """Check if this rule matches the given resource"""
        if not self.compiled_pattern:
            return False
        return bool(self.compiled_pattern.match(resource))
        
    def applies_to_tool(self, tool_name: str) -> bool:
        """Check if this rule applies to the given tool"""
        return tool_name in self.tool_names or "*" in self.tool_names


class PermissionManager:
    """Central permission management"""
    
    def __init__(self, config_path: Optional[Path] = None):
        self.rules: List[PermissionRule] = []
        self.default_permission = PermissionLevel.READ
        
        if config_path and config_path.exists():
            self.load_rules_from_config(config_path)
        else:
            self._load_default_rules()
    
    def _load_default_rules(self) -> None:
        """Load default security rules"""
        default_rules = [
            # System protection
            PermissionRule(
                id="protect_system_dirs",
                name="System Directory Protection", 
                description="Protect critical system directories",
                resource_type=ResourceType.DIRECTORY,
                resource_pattern=r"^(/etc|/usr|/boot|/sys|/proc|C:\\Windows|C:\\Program Files).*",
                tool_names=["*"],
                permission_level=PermissionLevel.NONE,
                priority=10
            ),
            
            # Git protection
            PermissionRule(
                id="protect_git_dir",
                name="Git Directory Protection",
                description="Protect .git directories from accidental modification",
                resource_type=ResourceType.DIRECTORY, 
                resource_pattern=r".*\.git(/.*)?$",
                tool_names=["write_file", "delete_file", "bash_execute"],
                permission_level=PermissionLevel.READ,
                priority=20
            ),
            
            # Config file protection
            PermissionRule(
                id="protect_config_files",
                name="Configuration File Protection",
                description="Require elevated permission for config files",
                resource_type=ResourceType.FILE,
                resource_pattern=r".*\.(env|config|conf|yaml|yml|json|ini)$",
                tool_names=["write_file", "delete_file"],
                permission_level=PermissionLevel.WRITE,
                priority=30
            ),
            
            # Python cache protection
            PermissionRule(
                id="allow_python_cache",
                name="Python Cache Access",
                description="Allow access to Python cache directories",
                resource_type=ResourceType.DIRECTORY,
                resource_pattern=r".*(__pycache__|\.pytest_cache)(/.*)?$",
                tool_names=["*"],
                permission_level=PermissionLevel.WRITE,
                priority=40
            ),
            
            # Default workspace access
            PermissionRule(
                id="workspace_access",
                name="Workspace Access",
                description="Allow full access to workspace files",
                resource_type=ResourceType.FILE,
                resource_pattern=r"^\./((?!\.git/).)*$",
                tool_names=["*"],
                permission_level=PermissionLevel.WRITE,
                priority=100
            )
        ]
        
        for rule in default_rules:
            self.add_rule(rule)
            
    def check_permission(self, 
                        tool_name: str,
                        resource: str,
                        action: str,
                        context: Optional[Dict[str, Any]] = None) -> bool:
        """Check if action is permitted"""
        
        context = context or {}
        
        # Find applicable rules, sorted by priority
        applicable_rules = [
            rule for rule in sorted(self.rules, key=lambda r: r.priority)
            if rule.enabled and rule.applies_to_tool(tool_name) and rule.matches_resource(resource)
        ]
        
        if not applicable_rules:
            # No specific rules, use default permission
            return self._check_default_permission(action)
        
        # Use the highest priority rule (lowest priority number)
        rule = applicable_rules[0]
        
        # Check if the requested action is allowed by the rule
        return self._action_allowed_by_permission(action, rule.permission_level, context)
    
    def _check_default_permission(self, action: str) -> bool:
        """Check if action is allowed by default permission"""
        return self._action_allowed_by_permission(action, self.default_permission, {})
    
    def _action_allowed_by_permission(self, 
                                    action: str, 
                                    permission: PermissionLevel,
                                    context: Dict[str, Any]) -> bool:
        """Check if action is allowed by permission level"""
        
        action_requirements = {
            "read": PermissionLevel.READ,
            "write": PermissionLevel.WRITE, 
            "execute": PermissionLevel.EXECUTE,
            "delete": PermissionLevel.WRITE,
            "create": PermissionLevel.WRITE,
            "modify": PermissionLevel.WRITE,
            "admin": PermissionLevel.ADMIN
        }
        
        required_level = action_requirements.get(action.lower(), PermissionLevel.READ)
        
        # Permission level hierarchy
        level_hierarchy = [
            PermissionLevel.NONE,
            PermissionLevel.READ,
            PermissionLevel.WRITE,
            PermissionLevel.EXECUTE,
            PermissionLevel.ADMIN
        ]
        
        try:
            required_index = level_hierarchy.index(required_level)
            current_index = level_hierarchy.index(permission)
            return current_index >= required_index
        except ValueError:
            # Unknown permission level, deny by default
            return False
    
    def add_rule(self, rule: PermissionRule) -> None:
        """Add a permission rule"""
        # Remove existing rule with same ID
        self.rules = [r for r in self.rules if r.id != rule.id]
        self.rules.append(rule)
        logger.debug(f"Added permission rule: {rule.id}")
    
    def remove_rule(self, rule_id: str) -> bool:
        """Remove a permission rule"""
        original_count = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        removed = len(self.rules) < original_count
        
        if removed:
            logger.debug(f"Removed permission rule: {rule_id}")
        return removed
    
    def load_rules_from_config(self, config_path: Path) -> None:
        """Load rules from YAML configuration"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            rules_config = config.get('permission_rules', [])
            
            for rule_data in rules_config:
                rule = PermissionRule(
                    id=rule_data['id'],
                    name=rule_data['name'],
                    description=rule_data['description'],
                    resource_type=ResourceType(rule_data['resource_type']),
                    resource_pattern=rule_data['resource_pattern'],
                    tool_names=rule_data['tool_names'],
                    permission_level=PermissionLevel(rule_data['permission_level']),
                    conditions=rule_data.get('conditions', {}),
                    priority=rule_data.get('priority', 100),
                    enabled=rule_data.get('enabled', True)
                )
                self.add_rule(rule)
                
            logger.info(f"Loaded {len(rules_config)} permission rules from {config_path}")
            
        except Exception as e:
            logger.error(f"Failed to load permission rules from {config_path}: {e}")
            # Load default rules as fallback
            self._load_default_rules()
    
    def save_rules_to_config(self, config_path: Path) -> None:
        """Save current rules to YAML configuration"""
        try:
            rules_data = []
            
            for rule in self.rules:
                rule_data = {
                    'id': rule.id,
                    'name': rule.name,
                    'description': rule.description,
                    'resource_type': rule.resource_type.value,
                    'resource_pattern': rule.resource_pattern,
                    'tool_names': rule.tool_names,
                    'permission_level': rule.permission_level.value,
                    'conditions': rule.conditions,
                    'priority': rule.priority,
                    'enabled': rule.enabled
                }
                rules_data.append(rule_data)
            
            config = {
                'permission_rules': rules_data,
                'default_permission': self.default_permission.value
            }
            
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, indent=2, default_flow_style=False)
                
            logger.info(f"Saved {len(rules_data)} permission rules to {config_path}")
            
        except Exception as e:
            logger.error(f"Failed to save permission rules to {config_path}: {e}")
    
    def get_rule_by_id(self, rule_id: str) -> Optional[PermissionRule]:
        """Get rule by ID"""
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None
    
    def list_rules(self, tool_name: Optional[str] = None) -> List[PermissionRule]:
        """List all rules, optionally filtered by tool name"""
        if tool_name:
            return [r for r in self.rules if r.applies_to_tool(tool_name)]
        return self.rules.copy()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get permission system statistics"""
        return {
            "total_rules": len(self.rules),
            "enabled_rules": len([r for r in self.rules if r.enabled]),
            "rules_by_priority": len(set(r.priority for r in self.rules)),
            "default_permission": self.default_permission.value,
            "resource_types": list(set(r.resource_type.value for r in self.rules))
        }