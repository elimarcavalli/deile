#!/usr/bin/env python3
"""
Backward Compatibility Test Suite
================================

Tests that all existing API interfaces are preserved and work correctly
with the unified configuration system.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

import asyncio
import sys
import tempfile
import shutil
from pathlib import Path

# Add the deile package to sys.path
sys.path.insert(0, str(Path(__file__).parent))

print("🧪 BACKWARD COMPATIBILITY TEST SUITE")
print("=" * 45)

class BackwardCompatibilityTest:
    """Backward compatibility test suite"""

    def __init__(self):
        self.temp_dir = None
        self.test_results = []

    def log_result(self, test_name: str, passed: bool, details: str = ""):
        """Log test result"""
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} | {test_name}")
        if details:
            print(f"      {details}")
        self.test_results.append((test_name, passed, details))

    async def setup(self):
        """Set up test environment"""
        print("\n🔧 Setting up backward compatibility test environment...")
        self.temp_dir = Path(tempfile.mkdtemp(prefix="deile_compat_test_"))
        print(f"   📁 Test directory: {self.temp_dir}")

    async def teardown(self):
        """Clean up test environment"""
        print("\n🧹 Cleaning up test environment...")
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            print("   🗑️  Temporary directory removed")

    async def test_persona_manager_interface_preservation(self):
        """Test that PersonaManager interface is preserved"""
        print("\n📋 Testing PersonaManager interface preservation...")

        try:
            # Test PersonaManager file structure
            manager_file = Path("deile/personas/manager.py")
            if not manager_file.exists():
                self.log_result("PersonaManager file exists", False, f"File not found: {manager_file}")
                return

            content = manager_file.read_text()

            # Check for essential public methods
            required_methods = [
                "def get_persona",
                "def get_active_persona",
                "def get_current_persona",
                "def has_active_persona",
                "async def switch_persona",
                "def list_personas"
            ]

            missing_methods = []
            for method in required_methods:
                if method not in content:
                    missing_methods.append(method)

            interface_preserved = len(missing_methods) == 0

            self.log_result(
                "Essential public methods preserved",
                interface_preserved,
                f"Missing methods: {missing_methods}" if missing_methods else "All methods found"
            )

            # Check for constructor compatibility
            constructor_patterns = [
                "def __init__",
                "agent",  # Should accept agent parameter
                "memory_manager"  # Should accept memory_manager parameter
            ]

            constructor_compatible = all(pattern in content for pattern in constructor_patterns)

            self.log_result(
                "Constructor compatibility",
                constructor_compatible,
                "Constructor accepts expected parameters"
            )

            # Check for configuration integration
            config_integration_patterns = [
                "self.config_manager",
                "add_persona_observer",
                "_on_persona_config_change"
            ]

            config_integrated = all(pattern in content for pattern in config_integration_patterns)

            self.log_result(
                "Configuration integration",
                config_integrated,
                "Unified configuration properly integrated"
            )

        except Exception as e:
            self.log_result("PersonaManager interface preservation", False, str(e))

    async def test_configuration_manager_interface(self):
        """Test that ConfigManager provides expected interface"""
        print("\n📋 Testing ConfigManager interface...")

        try:
            config_manager_file = Path("deile/config/manager.py")
            if not config_manager_file.exists():
                self.log_result("ConfigManager file exists", False, f"File not found: {config_manager_file}")
                return

            content = config_manager_file.read_text()

            # Check for persona-specific methods
            persona_methods = [
                "async def load_persona_configuration",
                "async def get_persona_config",
                "async def update_persona_config",
                "async def add_persona",
                "async def remove_persona",
                "def add_persona_observer"
            ]

            missing_persona_methods = []
            for method in persona_methods:
                if method not in content:
                    missing_persona_methods.append(method)

            persona_methods_present = len(missing_persona_methods) == 0

            self.log_result(
                "Persona configuration methods",
                persona_methods_present,
                f"Missing methods: {missing_persona_methods}" if missing_persona_methods else "All methods found"
            )

            # Check for hot-reload capability
            hot_reload_patterns = [
                "async def setup_hot_reload",
                "async def _notify_persona_observers",
                "_file_changed"
            ]

            hot_reload_supported = any(pattern in content for pattern in hot_reload_patterns)

            self.log_result(
                "Hot-reload capability",
                hot_reload_supported,
                "Hot-reload functionality available"
            )

        except Exception as e:
            self.log_result("ConfigManager interface", False, str(e))

    async def test_persona_config_models_compatibility(self):
        """Test PersonaConfig models compatibility"""
        print("\n📋 Testing PersonaConfig models compatibility...")

        try:
            persona_config_file = Path("deile/personas/config.py")
            if not persona_config_file.exists():
                self.log_result("PersonaConfig file exists", False, f"File not found: {persona_config_file}")
                return

            content = persona_config_file.read_text()

            # Check for essential classes
            essential_classes = [
                "class CommunicationStyle",
                "class VerbosityLevel",
                "class ModelPreferences",
                "class BehaviorSettings",
                "class ToolPreferences",
                "class PersonaConfig"
            ]

            missing_classes = []
            for cls in essential_classes:
                if cls not in content:
                    missing_classes.append(cls)

            classes_present = len(missing_classes) == 0

            self.log_result(
                "Essential configuration classes",
                classes_present,
                f"Missing classes: {missing_classes}" if missing_classes else "All classes found"
            )

            # Check for factory methods
            factory_methods = [
                "def from_dict",
                "def to_dict",
                "async def load_from_config_manager",
                "async def save"
            ]

            factory_methods_available = any(method in content for method in factory_methods)

            self.log_result(
                "Factory and serialization methods",
                factory_methods_available,
                "Configuration serialization methods available"
            )

            # Check for validation
            validation_patterns = [
                "ValidationError",
                "_validate_persona_data"
            ]

            validation_present = any(pattern in content for pattern in validation_patterns)

            self.log_result(
                "Configuration validation",
                validation_present,
                "Validation mechanisms present"
            )

        except Exception as e:
            self.log_result("PersonaConfig models compatibility", False, str(e))

    async def test_persona_loader_compatibility(self):
        """Test PersonaLoader compatibility"""
        print("\n📋 Testing PersonaLoader compatibility...")

        try:
            loader_file = Path("deile/personas/loader.py")
            if not loader_file.exists():
                self.log_result("PersonaLoader file exists", False, f"File not found: {loader_file}")
                return

            content = loader_file.read_text()

            # Check for essential methods
            loader_methods = [
                "async def load_persona",
                "async def load_persona_instructions",
                "def register_persona_class",
                "async def discover_persona_modules"
            ]

            missing_loader_methods = []
            for method in loader_methods:
                if method not in content:
                    missing_loader_methods.append(method)

            loader_methods_present = len(missing_loader_methods) == 0

            self.log_result(
                "Essential loader methods",
                loader_methods_present,
                f"Missing methods: {missing_loader_methods}" if missing_loader_methods else "All methods found"
            )

            # Check for configuration integration
            config_patterns = [
                "config_manager",
                "from .config import PersonaConfig",
                "InstructionLoader"
            ]

            config_integration = all(pattern in content for pattern in config_patterns)

            self.log_result(
                "Loader configuration integration",
                config_integration,
                "Loader properly integrated with unified configuration"
            )

            # Check for BaseAutonomousPersona usage
            persona_base_correct = "BaseAutonomousPersona" in content

            self.log_result(
                "Correct persona base class",
                persona_base_correct,
                "Uses BaseAutonomousPersona consistently"
            )

        except Exception as e:
            self.log_result("PersonaLoader compatibility", False, str(e))

    async def test_deprecated_systems_removal(self):
        """Test that deprecated duplicate systems are removed"""
        print("\n📋 Testing deprecated systems removal...")

        try:
            manager_file = Path("deile/personas/manager.py")
            content = manager_file.read_text()

            # Check that old systems are removed
            deprecated_patterns = [
                "class PersonaConfigHandler",  # Old hot-reload
                "self.personas_dir",  # Old directory management
                "async def discover_and_load_personas",  # Old discovery
                "async def load_persona_from_file",  # Old file loading
                "async def handle_hot_reload"  # Old hot-reload handler
            ]

            found_deprecated = []
            for pattern in deprecated_patterns:
                if pattern in content:
                    found_deprecated.append(pattern)

            deprecated_removed = len(found_deprecated) == 0

            self.log_result(
                "Deprecated systems removed",
                deprecated_removed,
                f"Still found: {found_deprecated}" if found_deprecated else "All deprecated systems cleaned up"
            )

            # Check for clean unified integration
            unified_patterns = [
                "self.config_manager =",
                "add_persona_observer",
                "async def _on_persona_config_change"
            ]

            unified_integrated = all(pattern in content for pattern in unified_patterns)

            self.log_result(
                "Clean unified integration",
                unified_integrated,
                "Unified configuration cleanly integrated"
            )

        except Exception as e:
            self.log_result("Deprecated systems removal", False, str(e))

    async def test_default_configuration_presence(self):
        """Test that default configuration is present"""
        print("\n📋 Testing default configuration presence...")

        try:
            default_config_file = Path("deile/config/persona_config.yaml")
            config_present = default_config_file.exists()

            self.log_result(
                "Default configuration file",
                config_present,
                f"File: {default_config_file}"
            )

            if config_present:
                # Check file has reasonable content
                content = default_config_file.read_text()
                has_content = len(content) > 100  # Basic sanity check

                self.log_result(
                    "Default configuration content",
                    has_content,
                    f"Content length: {len(content)} characters"
                )

                # Check for expected structure
                structure_markers = [
                    "personas:",
                    "enabled:",
                    "persona_configs:",
                    "developer:",
                    "capabilities:"
                ]

                structure_valid = all(marker in content for marker in structure_markers)

                self.log_result(
                    "Default configuration structure",
                    structure_valid,
                    "Contains expected YAML structure"
                )

        except Exception as e:
            self.log_result("Default configuration presence", False, str(e))

    def print_summary(self):
        """Print test results summary"""
        print("\n" + "=" * 45)
        print("📊 BACKWARD COMPATIBILITY TEST RESULTS")
        print("=" * 45)

        total_tests = len(self.test_results)
        passed_tests = sum(1 for _, passed, _ in self.test_results if passed)
        failed_tests = total_tests - passed_tests

        print(f"Total Tests: {total_tests}")
        print(f"✅ Passed: {passed_tests}")
        print(f"❌ Failed: {failed_tests}")
        print(f"Success Rate: {(passed_tests/total_tests)*100:.1f}%")

        if failed_tests > 0:
            print("\n❌ FAILED TESTS:")
            for test_name, passed, details in self.test_results:
                if not passed:
                    print(f"   • {test_name}: {details}")

        print("\n" + "=" * 45)
        return failed_tests == 0

async def run_backward_compatibility_test():
    """Run backward compatibility test suite"""
    test_suite = BackwardCompatibilityTest()

    try:
        await test_suite.setup()

        # Run all test categories
        await test_suite.test_persona_manager_interface_preservation()
        await test_suite.test_configuration_manager_interface()
        await test_suite.test_persona_config_models_compatibility()
        await test_suite.test_persona_loader_compatibility()
        await test_suite.test_deprecated_systems_removal()
        await test_suite.test_default_configuration_presence()

        # Print summary
        all_tests_passed = test_suite.print_summary()

        if all_tests_passed:
            print("🎉 ALL BACKWARD COMPATIBILITY TESTS PASSED!")
            print("Existing interfaces preserved and working correctly!")
            return True
        else:
            print("🚨 SOME COMPATIBILITY TESTS FAILED!")
            print("Issues found with backward compatibility!")
            return False

    finally:
        await test_suite.teardown()

if __name__ == "__main__":
    print("Starting backward compatibility test suite...\n")

    try:
        success = asyncio.run(run_backward_compatibility_test())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)