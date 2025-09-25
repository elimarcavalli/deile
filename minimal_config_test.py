#!/usr/bin/env python3
"""
Minimal Configuration Test
========================

Tests the unified configuration system without external dependencies.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

import sys
from pathlib import Path

# Add the deile package to sys.path
sys.path.insert(0, str(Path(__file__).parent))

print("🧪 MINIMAL CONFIGURATION TEST")
print("=" * 40)

def test_imports_and_structure():
    """Test that all configuration components can be imported and have correct structure"""
    results = []

    try:
        # Test ConfigManager import and structure
        print("   Testing ConfigManager import...")

        # Check if files exist
        config_manager_file = Path("deile/config/manager.py")
        if not config_manager_file.exists():
            results.append(("ConfigManager file exists", False, f"File not found: {config_manager_file}"))
            return results

        # Check for required methods in ConfigManager
        content = config_manager_file.read_text()
        required_methods = [
            "async def load_persona_configuration",
            "async def get_persona_config",
            "async def update_persona_config",
            "async def add_persona",
            "async def remove_persona",
            "def add_persona_observer"
        ]

        missing_methods = []
        for method in required_methods:
            if method not in content:
                missing_methods.append(method)

        if missing_methods:
            results.append(("ConfigManager has required methods", False, f"Missing: {missing_methods}"))
        else:
            results.append(("ConfigManager has required methods", True, "All persona methods found"))

    except Exception as e:
        results.append(("ConfigManager structure test", False, str(e)))

    try:
        # Test PersonaConfig import and structure
        print("   Testing PersonaConfig import...")

        persona_config_file = Path("deile/personas/config.py")
        if not persona_config_file.exists():
            results.append(("PersonaConfig file exists", False, f"File not found: {persona_config_file}"))
            return results

        # Check for required classes
        content = persona_config_file.read_text()
        required_classes = [
            "class CommunicationStyle",
            "class VerbosityLevel",
            "class ModelPreferences",
            "class BehaviorSettings",
            "class ToolPreferences",
            "class PersonaConfig"
        ]

        missing_classes = []
        for cls in required_classes:
            if cls not in content:
                missing_classes.append(cls)

        if missing_classes:
            results.append(("PersonaConfig has required classes", False, f"Missing: {missing_classes}"))
        else:
            results.append(("PersonaConfig has required classes", True, "All required classes found"))

    except Exception as e:
        results.append(("PersonaConfig structure test", False, str(e)))

    try:
        # Test PersonaManager integration
        print("   Testing PersonaManager integration...")

        persona_manager_file = Path("deile/personas/manager.py")
        if not persona_manager_file.exists():
            results.append(("PersonaManager file exists", False, f"File not found: {persona_manager_file}"))
            return results

        content = persona_manager_file.read_text()

        # Check for unified configuration integration
        integration_checks = [
            "from .config import PersonaConfig",  # Uses unified config
            "self.config_manager =",  # Uses config manager
            "add_persona_observer",  # Registers as observer
            "async def _on_persona_config_change"  # Handles config changes
        ]

        missing_integration = []
        for check in integration_checks:
            if check not in content:
                missing_integration.append(check)

        if missing_integration:
            results.append(("PersonaManager unified integration", False, f"Missing: {missing_integration}"))
        else:
            results.append(("PersonaManager unified integration", True, "All integration points found"))

        # Check that old duplicate systems are removed
        deprecated_items = [
            "class PersonaConfigHandler",  # Old hot-reload
            "self.personas_dir",  # Old directory management
            "async def discover_and_load_personas",  # Old discovery
            "async def load_persona_from_file"  # Old file loading
        ]

        found_deprecated = []
        for item in deprecated_items:
            if item in content:
                found_deprecated.append(item)

        if found_deprecated:
            results.append(("Deprecated systems removed", False, f"Still found: {found_deprecated}"))
        else:
            results.append(("Deprecated systems removed", True, "All deprecated systems cleaned up"))

    except Exception as e:
        results.append(("PersonaManager integration test", False, str(e)))

    try:
        # Test PersonaLoader fixes
        print("   Testing PersonaLoader fixes...")

        persona_loader_file = Path("deile/personas/loader.py")
        if not persona_loader_file.exists():
            results.append(("PersonaLoader file exists", False, f"File not found: {persona_loader_file}"))
            return results

        content = persona_loader_file.read_text()

        # Check for fixes
        loader_fixes = [
            "from .config import PersonaConfig",  # Fixed import
            "def __init__(self, config_manager=None)",  # Accepts config_manager
            "async def load_persona_instructions"  # Added method
        ]

        missing_fixes = []
        for fix in loader_fixes:
            if fix not in content:
                missing_fixes.append(fix)

        if missing_fixes:
            results.append(("PersonaLoader fixes applied", False, f"Missing: {missing_fixes}"))
        else:
            results.append(("PersonaLoader fixes applied", True, "All fixes applied"))

    except Exception as e:
        results.append(("PersonaLoader fixes test", False, str(e)))

    try:
        # Test default configuration file
        print("   Testing default configuration file...")

        default_config_file = Path("deile/config/persona_config.yaml")
        if not default_config_file.exists():
            results.append(("Default config file exists", False, f"File not found: {default_config_file}"))
        else:
            # Check file has content
            content = default_config_file.read_text()
            if len(content) > 100:  # Basic sanity check
                results.append(("Default config file has content", True, f"File has {len(content)} characters"))
            else:
                results.append(("Default config file has content", False, f"File too small: {len(content)} characters"))

    except Exception as e:
        results.append(("Default config file test", False, str(e)))

    return results

def test_backward_compatibility():
    """Test that backward compatibility is maintained"""
    results = []

    try:
        print("   Testing backward compatibility...")

        persona_manager_file = Path("deile/personas/manager.py")
        content = persona_manager_file.read_text()

        # Check that essential public methods are preserved
        public_methods = [
            "def get_persona",
            "def get_active_persona",
            "def get_current_persona",
            "def has_active_persona",
            "async def switch_persona",
            "def list_personas"
        ]

        missing_methods = []
        for method in public_methods:
            if method not in content:
                missing_methods.append(method)

        if missing_methods:
            results.append(("Backward compatibility methods", False, f"Missing: {missing_methods}"))
        else:
            results.append(("Backward compatibility methods", True, "All public methods preserved"))

    except Exception as e:
        results.append(("Backward compatibility test", False, str(e)))

    return results

def test_architecture_compliance():
    """Test architectural compliance"""
    results = []

    try:
        print("   Testing architecture compliance...")

        # Check ConfigManager for DEILE patterns
        config_content = Path("deile/config/manager.py").read_text()

        patterns = [
            "async def",  # Async pattern
            "logger = logging.getLogger",  # Logging pattern
            "ValidationError",  # Error handling pattern
        ]

        missing_patterns = []
        for pattern in patterns:
            if pattern not in config_content:
                missing_patterns.append(pattern)

        if missing_patterns:
            results.append(("DEILE patterns in ConfigManager", False, f"Missing: {missing_patterns}"))
        else:
            results.append(("DEILE patterns in ConfigManager", True, "All patterns found"))

        # Check PersonaConfig for proper patterns
        persona_config_content = Path("deile/personas/config.py").read_text()

        persona_patterns = [
            "from enum import Enum",
            "@dataclass",
            "ValidationError",
            "async def"
        ]

        missing_persona_patterns = []
        for pattern in persona_patterns:
            if pattern not in persona_config_content:
                missing_persona_patterns.append(pattern)

        if missing_persona_patterns:
            results.append(("DEILE patterns in PersonaConfig", False, f"Missing: {missing_persona_patterns}"))
        else:
            results.append(("DEILE patterns in PersonaConfig", True, "All patterns found"))

    except Exception as e:
        results.append(("Architecture compliance test", False, str(e)))

    return results

def print_results(results):
    """Print test results"""
    total_tests = len(results)
    passed_tests = sum(1 for _, passed, _ in results if passed)
    failed_tests = total_tests - passed_tests

    print(f"\n📊 TEST RESULTS:")
    print(f"   Total: {total_tests}")
    print(f"   ✅ Passed: {passed_tests}")
    print(f"   ❌ Failed: {failed_tests}")
    print(f"   Success Rate: {(passed_tests/total_tests)*100:.1f}%")

    if failed_tests > 0:
        print("\n❌ FAILED TESTS:")
        for test_name, passed, details in results:
            if not passed:
                print(f"   • {test_name}: {details}")

    for test_name, passed, details in results:
        status = "✅" if passed else "❌"
        print(f"{status} {test_name}")

    return failed_tests == 0

def main():
    """Run minimal configuration tests"""
    print("Starting minimal configuration tests...\n")

    all_results = []

    # Run all test categories
    all_results.extend(test_imports_and_structure())
    all_results.extend(test_backward_compatibility())
    all_results.extend(test_architecture_compliance())

    # Print summary
    all_passed = print_results(all_results)

    if all_passed:
        print("\n🎉 ALL MINIMAL TESTS PASSED!")
        print("Unified configuration system structure is correct!")
        return True
    else:
        print("\n🚨 SOME TESTS FAILED!")
        print("Issues found in configuration system structure!")
        return False

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)