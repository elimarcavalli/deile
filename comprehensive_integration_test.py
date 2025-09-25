#!/usr/bin/env python3
"""
Comprehensive Integration Test for Unified Configuration Management
===============================================================

This comprehensive test suite validates the complete integration of the
unified configuration system with real-world scenarios and edge cases.

Author: DEILE Team
Version: 5.0.0 ULTRA
"""

import asyncio
import sys
import tempfile
import shutil
import logging
from pathlib import Path
from typing import Dict, Any
from unittest.mock import Mock, AsyncMock, patch

# Add the deile package to sys.path
sys.path.insert(0, str(Path(__file__).parent))

print("🧪 COMPREHENSIVE INTEGRATION TEST SUITE")
print("=" * 60)

# Configure logging for tests
logging.basicConfig(level=logging.WARNING)  # Reduce noise during tests


class MockAgent:
    """Mock DeileAgent for testing"""
    def __init__(self, config_manager=None, memory_manager=None):
        self.config_manager = config_manager
        self.memory_manager = memory_manager
        self.tool_registry = Mock()


class MockMemoryManager:
    """Mock memory manager with required components"""
    def __init__(self):
        self.semantic_memory = Mock()
        self.episodic_memory = Mock()
        self.working_memory = Mock()


class ComprehensiveIntegrationTestSuite:
    """Comprehensive integration test suite"""

    def __init__(self):
        self.temp_dir = None
        self.test_results = []
        self.config_manager = None
        self.persona_manager = None

    def log_result(self, test_name: str, passed: bool, details: str = ""):
        """Log test result"""
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} | {test_name}")
        if details:
            print(f"      {details}")
        self.test_results.append((test_name, passed, details))

    async def setup(self):
        """Set up comprehensive test environment"""
        print("\n🔧 Setting up comprehensive test environment...")

        # Create temporary directory for config
        self.temp_dir = Path(tempfile.mkdtemp(prefix="deile_integration_test_"))
        print(f"   📁 Test directory: {self.temp_dir}")

        # Dynamically import modules to catch import errors
        try:
            from deile.config.manager import ConfigManager
            from deile.personas.config import PersonaConfig, CommunicationStyle
            from deile.personas.manager import PersonaManager
            print("   ✅ All modules imported successfully")
        except Exception as e:
            print(f"   ❌ Module import failed: {e}")
            raise

        # Create ConfigManager with temporary directory
        self.config_manager = ConfigManager(config_dir=self.temp_dir)

        # Create mock agent and PersonaManager
        mock_memory_manager = MockMemoryManager()
        self.mock_agent = MockAgent(self.config_manager, mock_memory_manager)
        self.persona_manager = PersonaManager(self.mock_agent)

        print("   ⚙️  Test environment initialized")

    async def teardown(self):
        """Clean up test environment"""
        print("\n🧹 Cleaning up test environment...")
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            print("   🗑️  Temporary directory removed")

    async def test_end_to_end_configuration_flow(self):
        """Test complete end-to-end configuration flow"""
        print("\n📋 Testing end-to-end configuration flow...")

        try:
            # 1. Initialize ConfigManager and load default configuration
            personas_config = await self.config_manager.load_persona_configuration()
            has_personas = len(personas_config.get('persona_configs', {})) > 0

            self.log_result(
                "Default configuration loading",
                has_personas,
                f"Loaded {len(personas_config.get('persona_configs', {}))} personas"
            )

            # 2. Test persona configuration access
            dev_config = await self.config_manager.get_persona_config('developer')
            has_dev_config = 'capabilities' in dev_config

            self.log_result(
                "Persona config access",
                has_dev_config,
                f"Developer config has {len(dev_config)} fields"
            )

            # 3. Test PersonaManager initialization with unified config
            await self.persona_manager.initialize()
            personas_loaded = len(self.persona_manager._personas) > 0

            self.log_result(
                "PersonaManager initialization",
                personas_loaded,
                f"Loaded {len(self.persona_manager._personas)} personas"
            )

            # 4. Test persona configuration updates
            test_updates = {'test_capability': 'integration_test'}
            await self.config_manager.update_persona_config('developer', test_updates)

            updated_config = await self.config_manager.get_persona_config('developer')
            update_persisted = updated_config.get('test_capability') == 'integration_test'

            self.log_result(
                "Configuration updates",
                update_persisted,
                f"Update persisted: {updated_config.get('test_capability')}"
            )

            # 5. Test PersonaConfig model integration
            from deile.personas.config import PersonaConfig
            persona_config = await PersonaConfig.load_from_config_manager(
                'developer', self.config_manager
            )
            model_loaded = persona_config.persona_id == 'developer'

            self.log_result(
                "PersonaConfig model integration",
                model_loaded,
                f"Loaded PersonaConfig for {persona_config.persona_id}"
            )

        except Exception as e:
            self.log_result("End-to-end configuration flow", False, str(e))

    async def test_observer_pattern_integration(self):
        """Test observer pattern with real configuration changes"""
        print("\n📋 Testing observer pattern integration...")

        try:
            # Set up observer tracking
            observer_calls = []

            async def test_observer(persona_id: str, config: Dict[str, Any], event_type: str):
                observer_calls.append((persona_id, event_type, config))

            # Add observer
            self.config_manager.add_persona_observer(test_observer)

            # Test update notification
            await self.config_manager.update_persona_config('developer', {'observer_test': 'value'})
            await asyncio.sleep(0.1)  # Give observer time to be called

            update_notified = any(
                call[0] == 'developer' and call[1] == 'updated'
                for call in observer_calls
            )

            self.log_result(
                "Observer update notification",
                update_notified,
                f"Observer calls: {len(observer_calls)}"
            )

            # Test add persona notification
            new_persona_config = {
                'capabilities': ['test'],
                'communication_style': 'technical'
            }
            await self.config_manager.add_persona('test_observer_persona', new_persona_config)
            await asyncio.sleep(0.1)

            add_notified = any(
                call[0] == 'test_observer_persona' and call[1] == 'added'
                for call in observer_calls
            )

            self.log_result(
                "Observer add notification",
                add_notified,
                f"Add notification received"
            )

            # Test remove persona notification
            await self.config_manager.remove_persona('test_observer_persona')
            await asyncio.sleep(0.1)

            remove_notified = any(
                call[0] == 'test_observer_persona' and call[1] == 'removed'
                for call in observer_calls
            )

            self.log_result(
                "Observer remove notification",
                remove_notified,
                f"Remove notification received"
            )

        except Exception as e:
            self.log_result("Observer pattern integration", False, str(e))

    async def test_persona_manager_configuration_changes(self):
        """Test PersonaManager responds to configuration changes"""
        print("\n📋 Testing PersonaManager configuration change handling...")

        try:
            # Initial persona count
            initial_count = len(self.persona_manager._personas)

            # Add a new persona via ConfigManager
            new_persona_config = {
                'capabilities': ['test_capability'],
                'communication_style': 'technical',
                'model_preferences': {'temperature': 0.5}
            }

            await self.config_manager.add_persona('integration_test_persona', new_persona_config)
            await asyncio.sleep(0.2)  # Give handler time to process

            # Check if PersonaManager handled the addition
            persona_added = 'integration_test_persona' in self.persona_manager._personas
            final_count = len(self.persona_manager._personas)

            self.log_result(
                "PersonaManager persona addition handling",
                persona_added,
                f"Personas: {initial_count} → {final_count}"
            )

            # Update the persona configuration
            update_config = {'test_update': 'success'}
            await self.config_manager.update_persona_config('integration_test_persona', update_config)
            await asyncio.sleep(0.2)

            # Verify the persona still exists and can be accessed
            updated_persona_exists = 'integration_test_persona' in self.persona_manager._personas

            self.log_result(
                "PersonaManager persona update handling",
                updated_persona_exists,
                "Persona survived configuration update"
            )

            # Remove the persona
            await self.config_manager.remove_persona('integration_test_persona')
            await asyncio.sleep(0.2)

            # Check if PersonaManager handled the removal
            persona_removed = 'integration_test_persona' not in self.persona_manager._personas
            final_count_after_removal = len(self.persona_manager._personas)

            self.log_result(
                "PersonaManager persona removal handling",
                persona_removed,
                f"Final count: {final_count_after_removal}"
            )

        except Exception as e:
            self.log_result("PersonaManager configuration changes", False, str(e))

    async def test_error_handling_scenarios(self):
        """Test comprehensive error handling scenarios"""
        print("\n📋 Testing error handling scenarios...")

        try:
            # Test 1: Invalid configuration data
            try:
                await self.config_manager._validate_persona_config({'invalid': 'structure'})
                validation_failed = False
            except Exception:
                validation_failed = True  # Expected

            self.log_result(
                "Invalid configuration validation",
                validation_failed,
                "Validation correctly rejected invalid config"
            )

            # Test 2: Non-existent persona access
            nonexistent_config = await self.config_manager.get_persona_config('nonexistent_persona')
            empty_config_returned = len(nonexistent_config) == 0

            self.log_result(
                "Non-existent persona handling",
                empty_config_returned,
                "Empty config returned for non-existent persona"
            )

            # Test 3: PersonaConfig creation with invalid data
            from deile.personas.config import PersonaConfig, ValidationError
            try:
                PersonaConfig.from_dict('test', {'capabilities': 'not_a_list'})
                validation_passed = False
            except (ValidationError, ValueError):
                validation_passed = True  # Expected

            self.log_result(
                "PersonaConfig validation",
                validation_passed,
                "PersonaConfig correctly rejected invalid data"
            )

            # Test 4: Observer error resilience
            failing_observer = Mock(side_effect=Exception("Observer failed"))
            working_observer = Mock()

            self.config_manager.add_persona_observer(failing_observer)
            self.config_manager.add_persona_observer(working_observer)

            # This should not raise an exception
            await self.config_manager._notify_persona_observers('test', {}, 'updated')

            observer_resilience = working_observer.called

            self.log_result(
                "Observer error resilience",
                observer_resilience,
                "Working observer called despite failing observer"
            )

        except Exception as e:
            self.log_result("Error handling scenarios", False, str(e))

    async def test_configuration_persistence(self):
        """Test configuration persistence across instances"""
        print("\n📋 Testing configuration persistence...")

        try:
            # Make a configuration change
            test_config = {'persistence_test': 'success', 'timestamp': str(asyncio.get_event_loop().time())}
            await self.config_manager.update_persona_config('developer', test_config)

            # Create a new ConfigManager instance
            new_config_manager = ConfigManager(config_dir=self.temp_dir)
            persisted_config = await new_config_manager.get_persona_config('developer')

            # Check if the change persisted
            persistence_worked = persisted_config.get('persistence_test') == 'success'

            self.log_result(
                "Configuration persistence",
                persistence_worked,
                f"Persisted value: {persisted_config.get('persistence_test')}"
            )

            # Test file structure integrity
            config_file = self.temp_dir / "persona_config.yaml"
            file_exists = config_file.exists()

            self.log_result(
                "Configuration file creation",
                file_exists,
                f"Config file: {config_file}"
            )

        except Exception as e:
            self.log_result("Configuration persistence", False, str(e))

    async def test_persona_loader_integration(self):
        """Test PersonaLoader integration with unified configuration"""
        print("\n📋 Testing PersonaLoader integration...")

        try:
            # Test PersonaLoader with config_manager
            from deile.personas.loader import PersonaLoader

            loader = PersonaLoader(self.config_manager)
            loader_created = loader.config_manager is self.config_manager

            self.log_result(
                "PersonaLoader config_manager integration",
                loader_created,
                "PersonaLoader properly accepts config_manager"
            )

            # Test instruction loading
            instructions = await loader.load_persona_instructions('developer')
            instructions_loaded = len(instructions) > 0

            self.log_result(
                "PersonaLoader instruction loading",
                instructions_loaded,
                f"Instructions length: {len(instructions)}"
            )

            # Test fallback instruction generation
            fallback_instructions = await loader.load_persona_instructions('unknown_persona')
            fallback_worked = 'unknown_persona' in fallback_instructions

            self.log_result(
                "PersonaLoader fallback instructions",
                fallback_worked,
                "Fallback instructions generated for unknown persona"
            )

        except Exception as e:
            self.log_result("PersonaLoader integration", False, str(e))

    async def test_backward_compatibility(self):
        """Test backward compatibility with existing usage patterns"""
        print("\n📋 Testing backward compatibility...")

        try:
            # Test original PersonaManager usage patterns
            original_personas_count = len(self.persona_manager._personas)

            # Test original methods still work
            has_active = self.persona_manager.has_active_persona()
            current_persona = self.persona_manager.get_current_persona()
            available_personas = self.persona_manager.list_personas()

            methods_work = isinstance(has_active, bool) and isinstance(available_personas, list)

            self.log_result(
                "Original PersonaManager methods",
                methods_work,
                f"Methods working, {len(available_personas)} personas available"
            )

            # Test persona switching if personas are loaded
            if len(available_personas) > 0:
                first_persona_id = available_personas[0]
                switch_result = await self.persona_manager.switch_persona(first_persona_id)

                switch_worked = isinstance(switch_result, bool)

                self.log_result(
                    "Persona switching compatibility",
                    switch_worked,
                    f"Switch to {first_persona_id}: {switch_result}"
                )

            # Test configuration management methods
            try:
                from deile.personas.config import PersonaConfig
                test_config = PersonaConfig(
                    persona_id='backward_test',
                    capabilities=['test'],
                    config_manager=self.config_manager
                )

                await self.persona_manager.add_persona('backward_test', test_config)
                config_mgmt_works = True
            except Exception:
                config_mgmt_works = False

            self.log_result(
                "Configuration management methods",
                config_mgmt_works,
                "PersonaManager config methods work with unified system"
            )

        except Exception as e:
            self.log_result("Backward compatibility", False, str(e))

    def print_summary(self):
        """Print comprehensive test results summary"""
        print("\n" + "=" * 60)
        print("📊 COMPREHENSIVE TEST RESULTS SUMMARY")
        print("=" * 60)

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

        print("\n" + "=" * 60)
        return failed_tests == 0


async def run_comprehensive_integration_tests():
    """Run comprehensive integration test suite"""
    test_suite = ComprehensiveIntegrationTestSuite()

    try:
        await test_suite.setup()

        # Run all test categories
        await test_suite.test_end_to_end_configuration_flow()
        await test_suite.test_observer_pattern_integration()
        await test_suite.test_persona_manager_configuration_changes()
        await test_suite.test_error_handling_scenarios()
        await test_suite.test_configuration_persistence()
        await test_suite.test_persona_loader_integration()
        await test_suite.test_backward_compatibility()

        # Print comprehensive summary
        all_tests_passed = test_suite.print_summary()

        if all_tests_passed:
            print("🎉 ALL INTEGRATION TESTS PASSED - Unified configuration system working perfectly!")
            return True
        else:
            print("🚨 SOME INTEGRATION TESTS FAILED - Issues found in unified configuration system")
            return False

    finally:
        await test_suite.teardown()


if __name__ == "__main__":
    print("Starting comprehensive integration test suite...\n")

    try:
        success = asyncio.run(run_comprehensive_integration_tests())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n🛑 Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Integration test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)