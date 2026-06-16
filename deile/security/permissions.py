"""Permission System for DEILE"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

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

    # Prefixo das regras carregadas da camada ORG (issue #741). É o discriminador
    # que torna as regras de org estritamente subtrativas em ``check_permission``
    # (monotonicidade) — ver ``load_org_rules`` e ``check_permission``.
    _ORG_RULE_PREFIX = "org__"

    def __init__(self, config_path: Optional[Path] = None):
        self.rules: List[PermissionRule] = []
        self.default_permission = PermissionLevel.READ
        self.sandbox_enabled: bool = False
        self.config_path: Optional[Path] = config_path

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
            ),

            # Settings writes (issue #125) — DEFAULT IS FAIL-CLOSED.
            # The default rule is ``READ`` (i.e. "no writes"), matching the
            # security-first principle in 03-PRINCIPIOS-ARQUITETURAIS.md
            # §5: a missing operator policy must not silently grant write
            # access to security-relevant configuration.
            #
            # Operators wiring up an interactive workflow (e.g. /settings,
            # /skills add, --set logging.level=INFO from the CLI) MUST opt
            # in by overriding this rule via ``config/permissions.yaml``:
            #
            #   permission_rules:
            #     - id: settings_write_interactive
            #       name: Settings Write (Interactive)
            #       description: Allow settings writes from the operator
            #       resource_type: file
            #       resource_pattern: '^settings:(global|project):.*$'
            #       tool_names: [settings_manager]
            #       permission_level: write
            #       priority: 40
            #
            # See docs/system_design/08-SEGURANCA.md and 09-CONFIGURACAO.md
            # for the full rationale and snippet.
            PermissionRule(
                id="settings_write_default",
                name="Settings Write Default",
                description="Deny writes to ~/.deile/settings.json and "
                            "<project>/.deile/settings.json unless "
                            "explicitly enabled in config/permissions.yaml "
                            "(fail-closed; issue #125)",
                resource_type=ResourceType.FILE,
                resource_pattern=r"^settings:(global|project):.*$",
                tool_names=["settings_manager"],
                permission_level=PermissionLevel.READ,
                priority=50,
            ),
        ]
        
        for rule in default_rules:
            self.add_rule(rule)
            
    def check_permission(self, 
                        tool_name: str,
                        resource: str,
                        action: str,
                        context: Optional[Dict[str, Any]] = None) -> bool:
        """Check if action is permitted.

        Regras da camada ORG (id com prefixo ``org__``, issue #741) são
        estritamente **subtrativas**: o veredito efetivo é ``baseline AND org``.
        Uma regra de org só pode **negar** ou exigir aprovação — nunca conceder
        uma capacidade que o baseline (regras default/non-org) negaria. Vale
        **independente da prioridade** da regra de org, fechando o vetor de
        escalada em que uma regra ``admin`` de org com prioridade < 10
        sobreporia um ``none`` do baseline (monotonicidade — não-negociável).
        """

        context = context or {}

        # Find applicable rules, sorted by priority (lowest number = highest).
        applicable_rules = [
            rule for rule in sorted(self.rules, key=lambda r: r.priority)
            if rule.enabled and rule.applies_to_tool(tool_name) and rule.matches_resource(resource)
        ]

        org_rules = [r for r in applicable_rules if r.id.startswith(self._ORG_RULE_PREFIX)]
        base_rules = [r for r in applicable_rules if not r.id.startswith(self._ORG_RULE_PREFIX)]

        # Veredito baseline — semântica original (regras de org excluídas):
        # regra de maior prioridade vence, ou o default quando nenhuma casa.
        if base_rules:
            base_allowed = self._action_allowed_by_permission(
                action, base_rules[0].permission_level, context
            )
        else:
            base_allowed = self._check_default_permission(action)

        # Sem regras de org → comportamento byte-idêntico ao baseline.
        if not org_rules:
            return base_allowed

        # Monotonicidade (issue #741): org só APERTA. Efetivo = baseline AND org;
        # se o baseline já nega, nenhuma concessão de org o reverte.
        org_allowed = self._action_allowed_by_permission(
            action, org_rules[0].permission_level, context
        )
        return base_allowed and org_allowed
    
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
    
    def load_org_rules(self, org_config_root: Path) -> None:
        """Carrega regras de permissão da camada ORG a partir de ``org_config_root/permissions.yaml``.

        As regras recebem o prefixo ``org__`` no id; esse prefixo é o que
        ``check_permission`` usa para aplicar a **monotonicidade** (issue #741):
        o veredito efetivo é ``baseline AND org``, então uma regra de org só pode
        **apertar** — negar ou exigir aprovação — nunca conceder além do que o
        baseline permite. A garantia é estrutural (vale qualquer prioridade); a
        prioridade default ``5`` serve apenas para ordenar regras de org entre si.
        """
        permissions_file = org_config_root / "permissions.yaml"
        if not permissions_file.exists():
            return
        try:
            with open(permissions_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            if not isinstance(config, dict):
                logger.warning(
                    "permissions: org permissions.yaml em %s não é um mapping; ignorado",
                    permissions_file,
                )
                return
            rules_config = config.get("permission_rules", [])
            loaded = 0
            for rule_data in rules_config:
                org_priority = rule_data.get("priority", 5)
                rule = PermissionRule(
                    id=f"{self._ORG_RULE_PREFIX}{rule_data['id']}",
                    name=rule_data["name"],
                    description=rule_data.get("description", ""),
                    resource_type=ResourceType(rule_data["resource_type"]),
                    resource_pattern=rule_data["resource_pattern"],
                    tool_names=rule_data["tool_names"],
                    permission_level=PermissionLevel(rule_data["permission_level"]),
                    conditions=rule_data.get("conditions", {}),
                    priority=org_priority,
                    enabled=rule_data.get("enabled", True),
                )
                self.add_rule(rule)
                loaded += 1
            logger.info(
                "permissions: %d regras de org carregadas de %s",
                loaded, permissions_file,
            )
        except Exception as exc:
            logger.error(
                "permissions: falha ao carregar regras de org de %s: %s",
                permissions_file, exc,
            )

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


# Singleton instance
_permission_manager: Optional[PermissionManager] = None


_DEFAULT_PERMISSIONS_CONFIG = Path(__file__).parent.parent.parent / "config" / "permissions.yaml"


def get_permission_manager() -> PermissionManager:
    """Returns singleton instance of PermissionManager."""
    global _permission_manager
    if _permission_manager is None:
        _permission_manager = PermissionManager(config_path=_DEFAULT_PERMISSIONS_CONFIG)
    return _permission_manager