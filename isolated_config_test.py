#!/usr/bin/env python3
"""
Isolated Configuration Test Suite
===============================

This test isolates the configuration components to avoid dependency issues
while thoroughly testing the unified configuration system.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

import asyncio
import sys
import tempfile
import shutil
import yaml
from pathlib import Path
from typing import Dict, Any

# Add the deile package to sys.path
sys.path.insert(0, str(Path(__file__).parent))

print("🧪 ISOLATED CONFIGURATION TEST SUITE")
print("=" * 50)

class IsolatedConfigTestSuite:
    """Isolated configuration test suite"""

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
        print("\n🔧 Setting up isolated test environment...")
        self.temp_dir = Path(tempfile.mkdtemp(prefix="deile_config_test_"))
        print(f"   📁 Test directory: {self.temp_dir}")

    async def teardown(self):
        """Clean up test environment"""
        print("\n🧹 Cleaning up test environment...")
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            print("   🗑️  Temporary directory removed")

    async def test_config_manager_isolated(self):
        """Test ConfigManager in isolation"""
        print("\n📋 Testing ConfigManager in isolation...")

        try:
            # Import just the ConfigManager components we need
            sys.path.insert(0, str(Path(__file__).parent / 'deile'))

            # Create a minimal ConfigManager test
            from deile.config.manager import ConfigManager

            # Test basic initialization
            config_manager = ConfigManager(config_dir=self.temp_dir)
            init_success = config_manager is not None

            self.log_result(
                "ConfigManager initialization",
                init_success,
                "ConfigManager created successfully"
            )

            # Test persona configuration loading
            personas_config = await config_manager.load_persona_configuration()
            config_loaded = 'enabled' in personas_config

            self.log_result(
                "Load persona configuration",
                config_loaded,
                f"Config loaded with {len(personas_config)} top-level keys"
            )

            # Test get persona config
            dev_config = await config_manager.get_persona_config('developer')
            dev_loaded = 'capabilities' in dev_config

            self.log_result(
                "Get specific persona config",
                dev_loaded,
                f"Developer config has {len(dev_config)} fields"
            )

            # Test update persona config
            test_update = {'test_field': 'test_value'}
            await config_manager.update_persona_config('developer', test_update)

            updated_config = await config_manager.get_persona_config('developer')
            update_worked = updated_config.get('test_field') == 'test_value'

            self.log_result(
                "Update persona configuration",
                update_worked,
                f"Update persisted: {updated_config.get('test_field')}"
            )

            # Test add persona
            new_persona_config = {
                'capabilities': ['test_capability'],
                'communication_style': 'technical'
            }
            await config_manager.add_persona('test_persona', new_persona_config)

            added_config = await config_manager.get_persona_config('test_persona')
            add_worked = 'capabilities' in added_config

            self.log_result(
                "Add new persona",
                add_worked,
                f"New persona added with {len(added_config)} fields"
            )

            # Test remove persona
            await config_manager.remove_persona('test_persona')

            removed_config = await config_manager.get_persona_config('test_persona')
            remove_worked = len(removed_config) == 0

            self.log_result(
                "Remove persona",
                remove_worked,
                "Persona successfully removed"
            )

            # Test observer pattern
            observer_calls = []

            def test_observer(persona_id, config, event_type):
                observer_calls.append((persona_id, event_type))

            config_manager.add_persona_observer(test_observer)

            await config_manager.update_persona_config('developer', {'observer_test': 'value'})
            await asyncio.sleep(0.1)

            observer_worked = len(observer_calls) > 0

            self.log_result(
                "Observer pattern",
                observer_worked,
                f"Observer calls: {len(observer_calls)}"
            )

        except Exception as e:
            self.log_result("ConfigManager isolated test", False, str(e))

    async def test_persona_config_models_isolated(self):
        """Test PersonaConfig models in isolation"""
        print("\n📋 Testing PersonaConfig models in isolation...")

        try:
            from deile.personas.config import PersonaConfig, CommunicationStyle

            # Test PersonaConfig creation
            config = PersonaConfig(
                persona_id='test',
                capabilities=['test_capability'],
                communication_style=CommunicationStyle.TECHNICAL
            )

            creation_success = config.persona_id == 'test'

            self.log_result(
                "PersonaConfig creation",
                creation_success,
                f"Created config for {config.persona_id}"
            )

            # Test from_dict conversion
            config_data = {
                'capabilities': ['debugging'],
                'communication_style': 'analytical',
                'model_preferences': {'temperature': 0.3},
                'behavior_settings': {'verbosity_level': 'focused'},
                'tool_preferences': {'preferred_tools': ['analysis_tools']}
            }

            config_from_dict = PersonaConfig.from_dict('test_dict', config_data)
            dict_conversion_success = config_from_dict.persona_id == 'test_dict'

            self.log_result(
                "PersonaConfig from_dict",
                dict_conversion_success,
                f"Converted config with {len(config_from_dict.capabilities)} capabilities"
            )

            # Test to_dict serialization
            serialized = config.to_dict()
            serialization_success = (
                'capabilities' in serialized and
                'communication_style' in serialized
            )

            self.log_result(
                "PersonaConfig serialization",
                serialization_success,
                f"Serialized to {len(serialized)} fields"
            )

            # Test validation
            valid_config = PersonaConfig._validate_persona_data(config_data)
            validation_success = valid_config is None  # No exception raised

            self.log_result(
                "PersonaConfig validation",
                validation_success,
                "Valid configuration accepted"
            )

        except Exception as e:
            self.log_result("PersonaConfig models isolated test", False, str(e))

    async def test_persona_loader_isolated(self):
        """Test PersonaLoader in isolation"""
        print("\n📋 Testing PersonaLoader in isolation...")

        try:
            from deile.personas.loader import PersonaLoader

            # Test PersonaLoader creation
            loader = PersonaLoader()
            creation_success = loader is not None

            self.log_result(
                "PersonaLoader creation",
                creation_success,
                "PersonaLoader created successfully"
            )

            # Test instruction loading
            instructions = await loader.load_persona_instructions('developer')
            instructions_success = len(instructions) > 0

            self.log_result(
                "Load persona instructions",
                instructions_success,
                f"Loaded {len(instructions)} characters of instructions"
            )

            # Test fallback instruction generation
            fallback_instructions = loader._generate_basic_instruction('developer')
            fallback_success = 'developer' in fallback_instructions.lower()

            self.log_result(
                "Generate fallback instructions",
                fallback_success,
                f"Generated {len(fallback_instructions)} characters"
            )

        except Exception as e:
            self.log_result("PersonaLoader isolated test", False, str(e))

    async def test_file_persistence(self):
        """Test configuration file persistence"""
        print("\n📋 Testing file persistence...")

        try:
            from deile.config.manager import ConfigManager

            # Create config manager and make changes
            config_manager = ConfigManager(config_dir=self.temp_dir)
            await config_manager.load_persona_configuration()

            # Make a change
            test_data = {'persistence_test': 'success'}
            await config_manager.update_persona_config('developer', test_data)

            # Check file exists
            config_file = self.temp_dir / "persona_config.yaml"
            file_exists = config_file.exists()

            self.log_result(
                "Configuration file creation",
                file_exists,
                f"File created: {config_file}"
            )

            # Verify file content
            if file_exists:
                with open(config_file, 'r') as f:
                    file_content = yaml.safe_load(f)

                content_valid = (
                    'personas' in file_content and
                    'persona_configs' in file_content['personas']
                )

                self.log_result(
                    "Configuration file structure",
                    content_valid,
                    f"File has {len(file_content)} top-level keys"
                )

                # Test persistence across instances
                new_config_manager = ConfigManager(config_dir=self.temp_dir)
                persisted_config = await new_config_manager.get_persona_config('developer')

                persistence_success = persisted_config.get('persistence_test') == 'success'

                self.log_result(
                    "Configuration persistence",
                    persistence_success,
                    f"Value persisted: {persisted_config.get('persistence_test')}"
                )

        except Exception as e:
            self.log_result("File persistence test", False, str(e))

    async def test_error_handling(self):
        """Test error handling scenarios"""
        print("\n📋 Testing error handling scenarios...")

        try:
            from deile.config.manager import ConfigManager

            config_manager = ConfigManager(config_dir=self.temp_dir)

            # Test invalid configuration validation
            try:
                await config_manager._validate_persona_config({'invalid': 'structure'})
                validation_failed = False
            except Exception:
                validation_failed = True  # Expected

            self.log_result(
                "Invalid config validation",
                validation_failed,
                "Invalid configuration correctly rejected"
            )

            # Test non-existent persona access
            nonexistent_config = await config_manager.get_persona_config('nonexistent')
            empty_returned = len(nonexistent_config) == 0

            self.log_result(
                "Non-existent persona handling",
                empty_returned,
                "Empty config returned for non-existent persona"
            )

            # Test remove non-existent persona
            try:
                await config_manager.remove_persona('nonexistent')
                removal_failed = False
            except Exception:
                removal_failed = True  # Expected

            self.log_result(
                "Remove non-existent persona",
                removal_failed,
                "Correctly raised error for non-existent persona removal"
            )

        except Exception as e:
            self.log_result("Error handling test", False, str(e))

    def print_summary(self):
        """Print test results summary"""
        print("\n" + "=" * 50)
        print("📊 ISOLATED TEST RESULTS SUMMARY")
        print("=" * 50)

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

        print("\n" + "=" * 50)
        return failed_tests == 0


async def run_isolated_tests():
    """Run isolated configuration tests"""
    test_suite = IsolatedConfigTestSuite()

    try:
        await test_suite.setup()

        # Run all test categories
        await test_suite.test_config_manager_isolated()
        await test_suite.test_persona_config_models_isolated()
        await test_suite.test_persona_loader_isolated()
        await test_suite.test_file_persistence()
        await test_suite.test_error_handling()

        # Print summary
        all_tests_passed = test_suite.print_summary()

        if all_tests_passed:
            print("🎉 ALL ISOLATED TESTS PASSED - Configuration system working correctly!")
            return True
        else:
            print("🚨 SOME TESTS FAILED - Issues found in configuration system")
            return False

    finally:
        await test_suite.teardown()


if __name__ == "__main__":
    print("Starting isolated configuration test suite...\n")

    try:
        success = asyncio.run(run_isolated_tests())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)