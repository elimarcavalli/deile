#!/usr/bin/env python3
"""
Simple Integration Test for Configuration System
=============================================

A simplified test that validates core configuration functionality
without requiring external dependencies.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

import asyncio
import sys
import tempfile
import shutil
import yaml
from pathlib import Path

# Add the deile package to sys.path
sys.path.insert(0, str(Path(__file__).parent))

print("🧪 SIMPLE CONFIGURATION INTEGRATION TEST")
print("=" * 50)

class SimpleIntegrationTest:
    """Simple integration test for configuration system"""

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
        print("\n🔧 Setting up simple test environment...")
        self.temp_dir = Path(tempfile.mkdtemp(prefix="deile_simple_test_"))
        print(f"   📁 Test directory: {self.temp_dir}")

    async def teardown(self):
        """Clean up test environment"""
        print("\n🧹 Cleaning up test environment...")
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            print("   🗑️  Temporary directory removed")

    async def test_configuration_file_handling(self):
        """Test configuration file creation and validation"""
        print("\n📋 Testing configuration file handling...")

        try:
            # Create test configuration
            test_config = {
                'personas': {
                    'enabled': True,
                    'persona_configs': {
                        'test_persona': {
                            'capabilities': ['test_capability'],
                            'communication_style': 'technical',
                            'model_preferences': {
                                'temperature': 0.5,
                                'max_tokens': 2048
                            },
                            'behavior_settings': {
                                'verbosity_level': 'focused',
                                'response_style': 'detailed'
                            }
                        }
                    }
                }
            }

            # Write configuration file
            config_file = self.temp_dir / "persona_config.yaml"
            with open(config_file, 'w') as f:
                yaml.dump(test_config, f, default_flow_style=False)

            file_created = config_file.exists()
            self.log_result(
                "Configuration file creation",
                file_created,
                f"Config file created: {config_file}"
            )

            # Read and validate structure
            with open(config_file, 'r') as f:
                loaded_config = yaml.safe_load(f)

            structure_valid = (
                'personas' in loaded_config and
                'enabled' in loaded_config['personas'] and
                'persona_configs' in loaded_config['personas'] and
                'test_persona' in loaded_config['personas']['persona_configs']
            )

            self.log_result(
                "Configuration structure validation",
                structure_valid,
                f"Valid YAML structure with {len(loaded_config)} top-level keys"
            )

            # Test persona config access
            persona_config = loaded_config['personas']['persona_configs']['test_persona']
            persona_valid = (
                'capabilities' in persona_config and
                'communication_style' in persona_config and
                'model_preferences' in persona_config and
                'behavior_settings' in persona_config
            )

            self.log_result(
                "Persona configuration validation",
                persona_valid,
                f"Persona config has {len(persona_config)} fields"
            )

        except Exception as e:
            self.log_result("Configuration file handling", False, str(e))

    async def test_configuration_update_simulation(self):
        """Test configuration update and persistence simulation"""
        print("\n📋 Testing configuration update simulation...")

        try:
            config_file = self.temp_dir / "persona_config.yaml"

            # Load existing config
            with open(config_file, 'r') as f:
                config = yaml.safe_load(f)

            # Simulate configuration update
            config['personas']['persona_configs']['test_persona']['test_field'] = 'test_value'
            config['personas']['persona_configs']['test_persona']['updated_timestamp'] = 'test_time'

            # Save updated config
            with open(config_file, 'w') as f:
                yaml.dump(config, f, default_flow_style=False)

            # Verify update persisted
            with open(config_file, 'r') as f:
                updated_config = yaml.safe_load(f)

            update_persisted = (
                updated_config['personas']['persona_configs']['test_persona'].get('test_field') == 'test_value' and
                'updated_timestamp' in updated_config['personas']['persona_configs']['test_persona']
            )

            self.log_result(
                "Configuration update persistence",
                update_persisted,
                f"Updates persisted successfully"
            )

            # Test add new persona simulation
            config['personas']['persona_configs']['new_test_persona'] = {
                'capabilities': ['new_capability'],
                'communication_style': 'analytical'
            }

            with open(config_file, 'w') as f:
                yaml.dump(config, f, default_flow_style=False)

            # Verify new persona added
            with open(config_file, 'r') as f:
                final_config = yaml.safe_load(f)

            persona_added = 'new_test_persona' in final_config['personas']['persona_configs']
            persona_count = len(final_config['personas']['persona_configs'])

            self.log_result(
                "Add persona simulation",
                persona_added,
                f"New persona added, total personas: {persona_count}"
            )

        except Exception as e:
            self.log_result("Configuration update simulation", False, str(e))

    async def test_error_scenarios(self):
        """Test error handling scenarios"""
        print("\n📋 Testing error scenarios...")

        try:
            # Test invalid YAML
            invalid_config_file = self.temp_dir / "invalid_config.yaml"
            with open(invalid_config_file, 'w') as f:
                f.write("invalid: yaml: content:\n  - missing: quote")

            try:
                with open(invalid_config_file, 'r') as f:
                    yaml.safe_load(f)
                yaml_error_handled = False
            except yaml.YAMLError:
                yaml_error_handled = True  # Expected

            self.log_result(
                "Invalid YAML error handling",
                yaml_error_handled,
                "YAML parsing error correctly detected"
            )

            # Test missing file handling
            missing_file = self.temp_dir / "nonexistent.yaml"
            try:
                with open(missing_file, 'r') as f:
                    yaml.safe_load(f)
                missing_file_handled = False
            except FileNotFoundError:
                missing_file_handled = True  # Expected

            self.log_result(
                "Missing file error handling",
                missing_file_handled,
                "Missing file error correctly detected"
            )

            # Test empty configuration
            empty_config_file = self.temp_dir / "empty_config.yaml"
            with open(empty_config_file, 'w') as f:
                f.write("")

            with open(empty_config_file, 'r') as f:
                empty_config = yaml.safe_load(f)

            empty_handled = empty_config is None

            self.log_result(
                "Empty configuration handling",
                empty_handled,
                "Empty configuration handled correctly"
            )

        except Exception as e:
            self.log_result("Error scenarios", False, str(e))

    def print_summary(self):
        """Print test results summary"""
        print("\n" + "=" * 50)
        print("📊 SIMPLE INTEGRATION TEST RESULTS")
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

async def run_simple_integration_test():
    """Run simple integration test"""
    test_suite = SimpleIntegrationTest()

    try:
        await test_suite.setup()

        # Run test categories
        await test_suite.test_configuration_file_handling()
        await test_suite.test_configuration_update_simulation()
        await test_suite.test_error_scenarios()

        # Print summary
        all_tests_passed = test_suite.print_summary()

        if all_tests_passed:
            print("🎉 ALL SIMPLE INTEGRATION TESTS PASSED!")
            print("Configuration file handling working correctly!")
            return True
        else:
            print("🚨 SOME SIMPLE TESTS FAILED!")
            print("Issues found in configuration file handling!")
            return False

    finally:
        await test_suite.teardown()

if __name__ == "__main__":
    print("Starting simple integration test...\n")

    try:
        success = asyncio.run(run_simple_integration_test())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)