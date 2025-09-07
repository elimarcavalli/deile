"""
Model Switching System for DEILE v4.0
====================================

Advanced model switching and routing system with automatic failover,
cost optimization, performance tracking, and intelligent model selection.

Author: DEILE
Version: 4.0
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Any, List, Optional, Union, Callable, Tuple

from deile.core.context_manager import ContextManager
from deile.core.exceptions import ModelError
from deile.infrastructure.monitoring.cost_tracker import get_cost_tracker, track_api_call

logger = logging.getLogger(__name__)


class ModelProvider(Enum):
    """Supported model providers"""
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CLAUDE = "claude"
    HUGGINGFACE = "huggingface"
    LOCAL = "local"


class ModelCapability(Enum):
    """Model capabilities"""
    TEXT_GENERATION = "text_generation"
    CODE_GENERATION = "code_generation"
    REASONING = "reasoning"
    MATH = "math"
    VISION = "vision"
    FUNCTION_CALLING = "function_calling"
    LARGE_CONTEXT = "large_context"
    SPEED = "speed"
    COST_EFFECTIVE = "cost_effective"


class SwitchReason(Enum):
    """Reasons for model switching"""
    USER_REQUEST = "user_request"
    COST_OPTIMIZATION = "cost_optimization"
    PERFORMANCE_OPTIMIZATION = "performance_optimization"
    FAILOVER = "failover"
    CAPABILITY_REQUIREMENT = "capability_requirement"
    LOAD_BALANCING = "load_balancing"
    RATE_LIMIT = "rate_limit"
    ERROR_RECOVERY = "error_recovery"


@dataclass
class ModelConfig:
    """Configuration for a specific model"""
    provider: str
    model_id: str
    name: str
    display_name: str
    description: str
    capabilities: List[str]
    context_length: int
    max_tokens: int
    supports_streaming: bool
    supports_functions: bool
    cost_per_1k_input: float
    cost_per_1k_output: float
    rate_limit_rpm: int
    rate_limit_tpm: int
    priority: int = 5  # 1-10, higher is better
    enabled: bool = True
    api_key_required: bool = True
    endpoint: Optional[str] = None
    timeout: int = 60
    retry_attempts: int = 3
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class ModelPerformance:
    """Performance metrics for a model"""
    model_id: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    avg_response_time: float = 0.0
    avg_tokens_per_second: float = 0.0
    last_used: Optional[float] = None
    error_rate: float = 0.0
    availability: float = 1.0
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None

    def update_success(self, tokens: int, response_time: float, cost: float):
        """Update metrics for successful request"""
        self.total_requests += 1
        self.successful_requests += 1
        self.total_tokens += tokens
        self.total_cost += cost
        
        # Update averages
        self.avg_response_time = (
            (self.avg_response_time * (self.successful_requests - 1) + response_time) / 
            self.successful_requests
        )
        
        if response_time > 0:
            tokens_per_sec = tokens / response_time
            self.avg_tokens_per_second = (
                (self.avg_tokens_per_second * (self.successful_requests - 1) + tokens_per_sec) / 
                self.successful_requests
            )
        
        self.last_used = time.time()
        self.error_rate = self.failed_requests / self.total_requests
        self.availability = self.successful_requests / self.total_requests

    def update_failure(self, error: str):
        """Update metrics for failed request"""
        self.total_requests += 1
        self.failed_requests += 1
        self.last_error = error
        self.last_error_time = time.time()
        self.error_rate = self.failed_requests / self.total_requests
        self.availability = self.successful_requests / self.total_requests


@dataclass
class SwitchEvent:
    """Model switch event record"""
    timestamp: float
    from_model: Optional[str]
    to_model: str
    reason: str
    context: Dict[str, Any]
    success: bool
    error: Optional[str] = None


class ModelSwitcher:
    """
    Intelligent model switching and routing system
    
    Features:
    - Multi-provider model support
    - Automatic failover and retry
    - Cost optimization
    - Performance-based routing
    - Capability-based selection
    - Load balancing
    - Rate limit handling
    - Real-time performance tracking
    """
    
    def __init__(self, config_path: Optional[str] = None):
        self.context_manager = ContextManager()
        self.cost_tracker = get_cost_tracker()
        
        # Model configurations
        self.models: Dict[str, ModelConfig] = {}
        self.performance: Dict[str, ModelPerformance] = {}
        
        # Current state
        self.current_model: Optional[str] = None
        self.default_model: Optional[str] = None
        self.fallback_models: List[str] = []
        
        # Switching logic
        self.auto_switch_enabled = True
        self.cost_optimization_enabled = True
        self.failover_enabled = True
        self.load_balancing_enabled = False
        
        # Switch history
        self.switch_history: List[SwitchEvent] = []
        self.max_history = 1000
        
        # Rate limiting tracking
        self.rate_limits: Dict[str, Dict[str, float]] = {}
        
        # Callbacks
        self.switch_callbacks: List[Callable] = []
        
        # Load configuration
        self._load_default_models()
        if config_path:
            self._load_config(config_path)
        
        # Initialize performance tracking
        self._init_performance_tracking()

    def _load_default_models(self):
        """Load default model configurations"""
        default_models = [
            # Gemini Models
            ModelConfig(
                provider="gemini",
                model_id="gemini-pro",
                name="gemini-pro",
                display_name="Gemini Pro",
                description="Google's most capable model for complex reasoning",
                capabilities=[
                    ModelCapability.TEXT_GENERATION.value,
                    ModelCapability.CODE_GENERATION.value,
                    ModelCapability.REASONING.value,
                    ModelCapability.FUNCTION_CALLING.value
                ],
                context_length=32768,
                max_tokens=8192,
                supports_streaming=True,
                supports_functions=True,
                cost_per_1k_input=0.000125,
                cost_per_1k_output=0.000375,
                rate_limit_rpm=60,
                rate_limit_tpm=120000,
                priority=8,
                timeout=30
            ),
            
            ModelConfig(
                provider="gemini",
                model_id="gemini-flash",
                name="gemini-flash",
                display_name="Gemini Flash",
                description="Fast and cost-effective model for simple tasks",
                capabilities=[
                    ModelCapability.TEXT_GENERATION.value,
                    ModelCapability.SPEED.value,
                    ModelCapability.COST_EFFECTIVE.value
                ],
                context_length=32768,
                max_tokens=8192,
                supports_streaming=True,
                supports_functions=False,
                cost_per_1k_input=0.000075,
                cost_per_1k_output=0.0003,
                rate_limit_rpm=300,
                rate_limit_tpm=300000,
                priority=6
            ),
            
            # OpenAI Models
            ModelConfig(
                provider="openai",
                model_id="gpt-4",
                name="gpt-4",
                display_name="GPT-4",
                description="OpenAI's most capable model",
                capabilities=[
                    ModelCapability.TEXT_GENERATION.value,
                    ModelCapability.CODE_GENERATION.value,
                    ModelCapability.REASONING.value,
                    ModelCapability.FUNCTION_CALLING.value
                ],
                context_length=8192,
                max_tokens=4096,
                supports_streaming=True,
                supports_functions=True,
                cost_per_1k_input=0.03,
                cost_per_1k_output=0.06,
                rate_limit_rpm=200,
                rate_limit_tpm=40000,
                priority=9
            ),
            
            ModelConfig(
                provider="openai", 
                model_id="gpt-3.5-turbo",
                name="gpt-3.5-turbo",
                display_name="GPT-3.5 Turbo",
                description="Fast and cost-effective OpenAI model",
                capabilities=[
                    ModelCapability.TEXT_GENERATION.value,
                    ModelCapability.CODE_GENERATION.value,
                    ModelCapability.SPEED.value,
                    ModelCapability.COST_EFFECTIVE.value
                ],
                context_length=4096,
                max_tokens=4096,
                supports_streaming=True,
                supports_functions=True,
                cost_per_1k_input=0.0015,
                cost_per_1k_output=0.002,
                rate_limit_rpm=3500,
                rate_limit_tpm=90000,
                priority=7
            ),
            
            # Anthropic Models
            ModelConfig(
                provider="anthropic",
                model_id="claude-3-opus",
                name="claude-3-opus",
                display_name="Claude 3 Opus",
                description="Anthropic's most capable model",
                capabilities=[
                    ModelCapability.TEXT_GENERATION.value,
                    ModelCapability.CODE_GENERATION.value,
                    ModelCapability.REASONING.value,
                    ModelCapability.LARGE_CONTEXT.value
                ],
                context_length=200000,
                max_tokens=4096,
                supports_streaming=True,
                supports_functions=False,
                cost_per_1k_input=0.015,
                cost_per_1k_output=0.075,
                rate_limit_rpm=50,
                rate_limit_tpm=40000,
                priority=8
            ),
            
            ModelConfig(
                provider="anthropic",
                model_id="claude-3-sonnet",
                name="claude-3-sonnet",
                display_name="Claude 3 Sonnet",
                description="Balanced performance and cost",
                capabilities=[
                    ModelCapability.TEXT_GENERATION.value,
                    ModelCapability.CODE_GENERATION.value,
                    ModelCapability.REASONING.value
                ],
                context_length=200000,
                max_tokens=4096,
                supports_streaming=True,
                supports_functions=False,
                cost_per_1k_input=0.003,
                cost_per_1k_output=0.015,
                rate_limit_rpm=50,
                rate_limit_tpm=40000,
                priority=7
            )
        ]
        
        for model in default_models:
            self.models[model.model_id] = model
            self.performance[model.model_id] = ModelPerformance(model_id=model.model_id)
        
        # Set defaults
        self.default_model = "gemini-pro"
        self.current_model = self.default_model
        self.fallback_models = ["gemini-flash", "gpt-3.5-turbo", "claude-3-sonnet"]

    def _load_config(self, config_path: str):
        """Load configuration from file"""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            # Update settings
            self.auto_switch_enabled = config.get('auto_switch_enabled', True)
            self.cost_optimization_enabled = config.get('cost_optimization_enabled', True)
            self.failover_enabled = config.get('failover_enabled', True)
            self.default_model = config.get('default_model', self.default_model)
            self.fallback_models = config.get('fallback_models', self.fallback_models)
            
            # Load custom models
            custom_models = config.get('custom_models', [])
            for model_data in custom_models:
                model = ModelConfig(**model_data)
                self.models[model.model_id] = model
                if model.model_id not in self.performance:
                    self.performance[model.model_id] = ModelPerformance(model_id=model.model_id)
            
            logger.info(f"Loaded model configuration from {config_path}")
            
        except Exception as e:
            logger.error(f"Failed to load model config: {e}")

    def _init_performance_tracking(self):
        """Initialize performance tracking for all models"""
        for model_id in self.models:
            if model_id not in self.performance:
                self.performance[model_id] = ModelPerformance(model_id=model_id)

    def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of available models with their info"""
        models = []
        
        for model_id, config in self.models.items():
            if not config.enabled:
                continue
            
            perf = self.performance.get(model_id, ModelPerformance(model_id=model_id))
            
            model_info = {
                'model_id': model_id,
                'provider': config.provider,
                'display_name': config.display_name,
                'description': config.description,
                'capabilities': config.capabilities,
                'context_length': config.context_length,
                'cost_per_1k_input': config.cost_per_1k_input,
                'cost_per_1k_output': config.cost_per_1k_output,
                'priority': config.priority,
                'performance': {
                    'total_requests': perf.total_requests,
                    'success_rate': (1 - perf.error_rate) * 100 if perf.total_requests > 0 else 0,
                    'avg_response_time': perf.avg_response_time,
                    'availability': perf.availability * 100 if perf.total_requests > 0 else 100
                },
                'is_current': model_id == self.current_model
            }
            models.append(model_info)
        
        # Sort by priority (descending) then by success rate
        models.sort(key=lambda x: (x['priority'], x['performance']['success_rate']), reverse=True)
        
        return models

    def switch_model(self, 
                    model_id: str, 
                    reason: Union[str, SwitchReason] = SwitchReason.USER_REQUEST,
                    context: Optional[Dict[str, Any]] = None) -> bool:
        """Switch to a specific model"""
        
        if model_id not in self.models:
            logger.error(f"Model {model_id} not found")
            return False
        
        if not self.models[model_id].enabled:
            logger.error(f"Model {model_id} is disabled")
            return False
        
        # Check if model is available
        if not self._is_model_available(model_id):
            logger.warning(f"Model {model_id} is not currently available")
            return False
        
        # Record switch event
        reason_str = reason.value if isinstance(reason, SwitchReason) else reason
        switch_event = SwitchEvent(
            timestamp=time.time(),
            from_model=self.current_model,
            to_model=model_id,
            reason=reason_str,
            context=context or {},
            success=True
        )
        
        old_model = self.current_model
        self.current_model = model_id
        
        # Add to history
        self.switch_history.append(switch_event)
        if len(self.switch_history) > self.max_history:
            self.switch_history.pop(0)
        
        # Trigger callbacks
        for callback in self.switch_callbacks:
            try:
                callback(old_model, model_id, reason_str, context or {})
            except Exception as e:
                logger.error(f"Switch callback failed: {e}")
        
        logger.info(f"Switched model from {old_model} to {model_id} (reason: {reason_str})")
        return True

    def auto_select_model(self, 
                         required_capabilities: Optional[List[str]] = None,
                         max_cost_per_1k: Optional[float] = None,
                         min_context_length: Optional[int] = None,
                         prefer_speed: bool = False,
                         prefer_cost: bool = False) -> Optional[str]:
        """Automatically select the best model based on criteria"""
        
        if not self.auto_switch_enabled:
            return self.current_model
        
        candidates = []
        
        for model_id, config in self.models.items():
            if not config.enabled:
                continue
            
            # Check availability
            if not self._is_model_available(model_id):
                continue
            
            # Check capabilities
            if required_capabilities:
                if not all(cap in config.capabilities for cap in required_capabilities):
                    continue
            
            # Check cost constraint
            if max_cost_per_1k:
                if config.cost_per_1k_output > max_cost_per_1k:
                    continue
            
            # Check context length
            if min_context_length:
                if config.context_length < min_context_length:
                    continue
            
            # Calculate score
            perf = self.performance.get(model_id, ModelPerformance(model_id=model_id))
            score = self._calculate_model_score(config, perf, prefer_speed, prefer_cost)
            
            candidates.append((model_id, score))
        
        if not candidates:
            logger.warning("No suitable models found for auto-selection")
            return self.current_model
        
        # Select best candidate
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_model = candidates[0][0]
        
        # Switch if different from current
        if best_model != self.current_model:
            context = {
                'required_capabilities': required_capabilities,
                'max_cost_per_1k': max_cost_per_1k,
                'min_context_length': min_context_length,
                'prefer_speed': prefer_speed,
                'prefer_cost': prefer_cost,
                'candidates_count': len(candidates)
            }
            
            if self.switch_model(best_model, SwitchReason.PERFORMANCE_OPTIMIZATION, context):
                return best_model
        
        return self.current_model

    def handle_model_error(self, model_id: str, error: str) -> Optional[str]:
        """Handle model error and attempt failover"""
        
        if model_id in self.performance:
            self.performance[model_id].update_failure(error)
        
        if not self.failover_enabled:
            return None
        
        # Try fallback models
        for fallback_model in self.fallback_models:
            if fallback_model == model_id:  # Skip the failed model
                continue
                
            if fallback_model not in self.models or not self.models[fallback_model].enabled:
                continue
            
            if not self._is_model_available(fallback_model):
                continue
            
            logger.info(f"Failing over from {model_id} to {fallback_model}")
            
            context = {
                'failed_model': model_id,
                'error': error,
                'failover_attempt': True
            }
            
            if self.switch_model(fallback_model, SwitchReason.FAILOVER, context):
                return fallback_model
        
        logger.error(f"No available fallback models for {model_id}")
        return None

    def track_model_usage(self, 
                         model_id: str,
                         input_tokens: int,
                         output_tokens: int,
                         response_time: float,
                         success: bool = True,
                         error: Optional[str] = None):
        """Track model usage and performance"""
        
        if model_id not in self.performance:
            self.performance[model_id] = ModelPerformance(model_id=model_id)
        
        perf = self.performance[model_id]
        config = self.models.get(model_id)
        
        if success and config:
            # Calculate cost
            cost = (
                (input_tokens / 1000) * config.cost_per_1k_input +
                (output_tokens / 1000) * config.cost_per_1k_output
            )
            
            # Update performance metrics
            perf.update_success(input_tokens + output_tokens, response_time, cost)
            
            # Track cost
            track_api_call(
                provider=config.provider,
                model=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                description=f"Model usage: {model_id}",
                metadata={
                    'response_time': response_time,
                    'model_provider': config.provider
                }
            )
        else:
            # Update failure metrics
            perf.update_failure(error or "Unknown error")

    def get_model_performance(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Get performance metrics for a model"""
        
        if model_id not in self.performance:
            return None
        
        perf = self.performance[model_id]
        config = self.models.get(model_id)
        
        return {
            'model_id': model_id,
            'model_name': config.display_name if config else model_id,
            'total_requests': perf.total_requests,
            'successful_requests': perf.successful_requests,
            'failed_requests': perf.failed_requests,
            'success_rate': (1 - perf.error_rate) * 100 if perf.total_requests > 0 else 0,
            'error_rate': perf.error_rate * 100,
            'total_tokens': perf.total_tokens,
            'total_cost': perf.total_cost,
            'avg_response_time': perf.avg_response_time,
            'avg_tokens_per_second': perf.avg_tokens_per_second,
            'availability': perf.availability * 100 if perf.total_requests > 0 else 100,
            'last_used': datetime.fromtimestamp(perf.last_used).isoformat() if perf.last_used else None,
            'last_error': perf.last_error,
            'last_error_time': datetime.fromtimestamp(perf.last_error_time).isoformat() if perf.last_error_time else None
        }

    def get_switch_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get model switch history"""
        
        history = []
        
        for event in self.switch_history[-limit:]:
            history.append({
                'timestamp': datetime.fromtimestamp(event.timestamp).isoformat(),
                'from_model': event.from_model,
                'to_model': event.to_model,
                'reason': event.reason,
                'success': event.success,
                'context': event.context,
                'error': event.error
            })
        
        return history

    def register_switch_callback(self, callback: Callable):
        """Register callback for model switches"""
        self.switch_callbacks.append(callback)

    def _is_model_available(self, model_id: str) -> bool:
        """Check if model is currently available"""
        
        if model_id not in self.models:
            return False
        
        config = self.models[model_id]
        
        # Check if model is enabled
        if not config.enabled:
            return False
        
        # Check rate limits
        if self._is_rate_limited(model_id):
            return False
        
        # Check recent error rate
        perf = self.performance.get(model_id)
        if perf and perf.total_requests > 10:
            # If error rate is too high recently, consider unavailable
            recent_threshold = time.time() - 300  # Last 5 minutes
            if perf.last_error_time and perf.last_error_time > recent_threshold:
                if perf.error_rate > 0.5:  # 50% error rate
                    return False
        
        return True

    def _is_rate_limited(self, model_id: str) -> bool:
        """Check if model is currently rate limited"""
        
        if model_id not in self.rate_limits:
            self.rate_limits[model_id] = {'requests': 0, 'tokens': 0, 'reset_time': 0}
        
        config = self.models.get(model_id)
        if not config:
            return False
        
        current_time = time.time()
        rate_limit = self.rate_limits[model_id]
        
        # Reset counters if minute has passed
        if current_time > rate_limit['reset_time']:
            rate_limit['requests'] = 0
            rate_limit['tokens'] = 0
            rate_limit['reset_time'] = current_time + 60  # Next minute
        
        # Check limits
        if rate_limit['requests'] >= config.rate_limit_rpm:
            return True
        
        if rate_limit['tokens'] >= config.rate_limit_tpm:
            return True
        
        return False

    def _calculate_model_score(self, 
                              config: ModelConfig, 
                              perf: ModelPerformance,
                              prefer_speed: bool = False,
                              prefer_cost: bool = False) -> float:
        """Calculate model selection score"""
        
        score = config.priority * 10  # Base priority score
        
        # Performance factors
        if perf.total_requests > 0:
            score += perf.availability * 20  # Availability boost
            score -= perf.error_rate * 30    # Error rate penalty
            
            if perf.avg_response_time > 0:
                # Response time factor (lower is better)
                time_factor = min(10, 10 / max(1, perf.avg_response_time))
                score += time_factor
        
        # Speed preference
        if prefer_speed:
            if ModelCapability.SPEED.value in config.capabilities:
                score += 15
            if perf.avg_tokens_per_second > 0:
                score += min(10, perf.avg_tokens_per_second / 10)
        
        # Cost preference
        if prefer_cost:
            if ModelCapability.COST_EFFECTIVE.value in config.capabilities:
                score += 15
            # Lower cost = higher score
            cost_factor = max(1, 0.01 / max(0.001, config.cost_per_1k_output))
            score += min(10, cost_factor)
        
        return score


# Global model switcher instance
_model_switcher_instance = None


def get_model_switcher() -> ModelSwitcher:
    """Get global model switcher instance"""
    global _model_switcher_instance
    if _model_switcher_instance is None:
        _model_switcher_instance = ModelSwitcher()
    return _model_switcher_instance


def switch_model(model_id: str, reason: str = "user_request") -> bool:
    """Convenience function to switch model"""
    return get_model_switcher().switch_model(model_id, reason)


def get_current_model() -> Optional[str]:
    """Get current active model"""
    return get_model_switcher().current_model


def auto_select_model(**kwargs) -> Optional[str]:
    """Convenience function for auto model selection"""
    return get_model_switcher().auto_select_model(**kwargs)