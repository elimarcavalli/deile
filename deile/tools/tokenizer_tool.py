"""
Tokenizer/Context Tool for DEILE v4.0
=====================================

Token estimation and context management tool for efficient
LLM token usage tracking and optimization.

Author: DEILE
Version: 4.0
"""

import logging
import re
import json
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, asdict
from datetime import datetime

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..core.exceptions import ToolError

logger = logging.getLogger(__name__)


@dataclass
class TokenEstimate:
    """Token count estimation result"""
    text_length: int
    estimated_tokens: int
    tokens_per_char: float
    model_used: str
    estimation_method: str


@dataclass
class ContextAnalysis:
    """Context window analysis result"""
    total_tokens: int
    system_tokens: int
    user_tokens: int
    assistant_tokens: int
    remaining_capacity: int
    context_utilization: float
    truncation_needed: bool


class TokenizerTool(SyncTool):
    """
    Token estimation and context management tool
    
    Features:
    - Multi-model token estimation
    - Context window analysis
    - Token optimization suggestions
    - Text truncation strategies
    - Cost estimation integration
    """
    
    def __init__(self):
        super().__init__(
            name="tokenizer",
            description="Estimate tokens and analyze context windows for LLM optimization",
            category="analysis",
            security_level="safe"
        )
        
        # Token estimation ratios for different models
        self.model_token_ratios = {
            'gemini-pro': {'chars_per_token': 4.0, 'context_window': 32768},
            'gemini-2.5-pro': {'chars_per_token': 4.0, 'context_window': 2097152},
            'gemini-1.5-pro': {'chars_per_token': 4.0, 'context_window': 1048576},
            'gemini-flash': {'chars_per_token': 4.2, 'context_window': 1048576},
            'gpt-4': {'chars_per_token': 3.8, 'context_window': 8192},
            'gpt-4-turbo': {'chars_per_token': 3.8, 'context_window': 128000},
            'gpt-3.5-turbo': {'chars_per_token': 4.0, 'context_window': 16384},
            'claude-3': {'chars_per_token': 3.5, 'context_window': 200000},
            'default': {'chars_per_token': 4.0, 'context_window': 32768}
        }

    def get_schema(self) -> Dict[str, Any]:
        """Get tool schema for function calling"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "action": {
                        "type": "STRING",
                        "enum": ["estimate", "analyze", "optimize"],
                        "description": "Action: estimate tokens, analyze context, or optimize text"
                    },
                    "text": {
                        "type": "STRING",
                        "description": "Text to analyze or estimate tokens for"
                    },
                    "model": {
                        "type": "STRING",
                        "description": "Target model for estimation (default: gemini-pro)"
                    },
                    "context_parts": {
                        "type": "OBJECT",
                        "description": "Context parts: {system: str, user: str, assistant: str}",
                        "properties": {
                            "system": {"type": "STRING"},
                            "user": {"type": "STRING"},
                            "assistant": {"type": "STRING"}
                        }
                    },
                    "target_tokens": {
                        "type": "NUMBER",
                        "description": "Target token count for optimization"
                    },
                    "truncation_strategy": {
                        "type": "STRING",
                        "enum": ["start", "end", "middle", "smart"],
                        "description": "How to truncate text if needed (default: smart)"
                    },
                    "include_cost": {
                        "type": "BOOLEAN",
                        "description": "Include cost estimation in results (default: false)"
                    },
                    "show_cli": {
                        "type": "BOOLEAN",
                        "description": "Display results in terminal (default: true)"
                    }
                },
                "required": ["action"]
            }
        }

    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute tokenizer operations"""
        try:
            # Extract parameters
            action = context.get_parameter("action")
            text = context.get_parameter("text", "")
            model = context.get_parameter("model", "gemini-pro")
            context_parts = context.get_parameter("context_parts", {})
            target_tokens = context.get_parameter("target_tokens")
            truncation_strategy = context.get_parameter("truncation_strategy", "smart")
            include_cost = context.get_parameter("include_cost", False)
            show_cli = context.get_parameter("show_cli", True)
            
            # Execute based on action
            if action == "estimate":
                if not text and not context_parts:
                    raise ToolError("Text or context_parts must be provided for estimation")
                
                if text:
                    result = self._estimate_tokens(text, model)
                else:
                    result = self._estimate_context_tokens(context_parts, model)
                
                result_data = asdict(result)
                display_data = self._prepare_estimate_display(result, include_cost)
                
            elif action == "analyze":
                if not context_parts:
                    raise ToolError("context_parts must be provided for analysis")
                
                result = self._analyze_context(context_parts, model)
                result_data = asdict(result)
                display_data = self._prepare_analysis_display(result)
                
            elif action == "optimize":
                if not text and not context_parts:
                    raise ToolError("Text or context_parts must be provided for optimization")
                
                if not target_tokens:
                    raise ToolError("target_tokens must be provided for optimization")
                
                if text:
                    result = self._optimize_text(text, target_tokens, model, truncation_strategy)
                else:
                    result = self._optimize_context(context_parts, target_tokens, model, truncation_strategy)
                
                result_data = result
                display_data = self._prepare_optimization_display(result)
                
            else:
                raise ToolError(f"Unknown action: {action}")
            
            message = f"Tokenizer {action} completed successfully"
            
            return ToolResult(
                status=ToolStatus.SUCCESS,
                data=result_data,
                message=message,
                display_policy=DisplayPolicy.SYSTEM,
                show_cli=show_cli,
                display_data=display_data if show_cli else None
            )
            
        except Exception as e:
            logger.error(f"TokenizerTool error: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Tokenizer operation failed: {str(e)}",
                error=e,
                display_policy=DisplayPolicy.SYSTEM
            )

    def _estimate_tokens(self, text: str, model: str) -> TokenEstimate:
        """Estimate tokens for given text"""
        model_config = self.model_token_ratios.get(model, self.model_token_ratios['default'])
        chars_per_token = model_config['chars_per_token']
        
        # Enhanced estimation considering different content types
        estimated_tokens = self._enhanced_token_estimation(text, chars_per_token)
        
        return TokenEstimate(
            text_length=len(text),
            estimated_tokens=estimated_tokens,
            tokens_per_char=1.0 / chars_per_token,
            model_used=model,
            estimation_method="enhanced_ratio"
        )

    def _enhanced_token_estimation(self, text: str, base_chars_per_token: float) -> int:
        """Enhanced token estimation considering content patterns"""
        # Adjust ratio based on content characteristics
        
        # Code content typically has more tokens per character
        code_patterns = [r'def\s+\w+', r'function\s+\w+', r'class\s+\w+', r'\{.*\}', r'\[.*\]']
        code_score = sum(len(re.findall(pattern, text)) for pattern in code_patterns)
        
        # Repetitive content has fewer unique tokens
        words = text.split()
        unique_words = len(set(words))
        repetition_ratio = unique_words / max(len(words), 1)
        
        # JSON/structured data
        json_like = text.count('{') + text.count('[') + text.count('"')
        
        # Adjust chars_per_token based on content
        adjustment = 1.0
        
        if code_score > 10:
            adjustment *= 0.8  # Code uses more tokens
        
        if repetition_ratio < 0.5:
            adjustment *= 1.2  # Repetitive content uses fewer unique tokens
        
        if json_like > len(text) * 0.05:
            adjustment *= 0.9  # JSON-like content is more token-dense
        
        adjusted_chars_per_token = base_chars_per_token * adjustment
        return int(len(text) / adjusted_chars_per_token)

    def _estimate_context_tokens(self, context_parts: Dict[str, str], model: str) -> ContextAnalysis:
        """Estimate tokens for context parts"""
        system_text = context_parts.get('system', '')
        user_text = context_parts.get('user', '')
        assistant_text = context_parts.get('assistant', '')
        
        # Estimate each part
        system_tokens = self._estimate_tokens(system_text, model).estimated_tokens if system_text else 0
        user_tokens = self._estimate_tokens(user_text, model).estimated_tokens if user_text else 0
        assistant_tokens = self._estimate_tokens(assistant_text, model).estimated_tokens if assistant_text else 0
        
        total_tokens = system_tokens + user_tokens + assistant_tokens
        
        # Get context window for model
        model_config = self.model_token_ratios.get(model, self.model_token_ratios['default'])
        context_window = model_config['context_window']
        
        remaining_capacity = max(0, context_window - total_tokens)
        utilization = total_tokens / context_window
        truncation_needed = total_tokens > context_window
        
        return ContextAnalysis(
            total_tokens=total_tokens,
            system_tokens=system_tokens,
            user_tokens=user_tokens,
            assistant_tokens=assistant_tokens,
            remaining_capacity=remaining_capacity,
            context_utilization=utilization,
            truncation_needed=truncation_needed
        )

    def _analyze_context(self, context_parts: Dict[str, str], model: str) -> ContextAnalysis:
        """Analyze context window usage"""
        return self._estimate_context_tokens(context_parts, model)

    def _optimize_text(self, text: str, target_tokens: int, model: str, strategy: str) -> Dict[str, Any]:
        """Optimize text to fit target token count"""
        current_estimate = self._estimate_tokens(text, model)
        
        if current_estimate.estimated_tokens <= target_tokens:
            return {
                "optimization_needed": False,
                "original_tokens": current_estimate.estimated_tokens,
                "target_tokens": target_tokens,
                "optimized_text": text,
                "reduction_achieved": 0
            }
        
        # Calculate target character count
        model_config = self.model_token_ratios.get(model, self.model_token_ratios['default'])
        target_chars = int(target_tokens * model_config['chars_per_token'])
        
        # Apply truncation strategy
        if strategy == "start":
            optimized_text = text[-target_chars:]
        elif strategy == "end":
            optimized_text = text[:target_chars]
        elif strategy == "middle":
            half = target_chars // 2
            optimized_text = text[:half] + "\n[...truncated...]\n" + text[-half:]
        else:  # smart
            optimized_text = self._smart_truncate(text, target_chars)
        
        # Verify final token count
        final_estimate = self._estimate_tokens(optimized_text, model)
        
        return {
            "optimization_needed": True,
            "original_tokens": current_estimate.estimated_tokens,
            "target_tokens": target_tokens,
            "final_tokens": final_estimate.estimated_tokens,
            "optimized_text": optimized_text,
            "reduction_achieved": current_estimate.estimated_tokens - final_estimate.estimated_tokens,
            "strategy_used": strategy
        }

    def _smart_truncate(self, text: str, target_chars: int) -> str:
        """Intelligently truncate text preserving important content"""
        if len(text) <= target_chars:
            return text
        
        # Try to preserve beginning and end, removing middle sections
        preserve_start = min(target_chars // 3, 1000)
        preserve_end = min(target_chars // 3, 500)
        
        if preserve_start + preserve_end + 50 >= target_chars:
            # Fallback to simple truncation
            return text[:target_chars]
        
        truncation_marker = "\n\n[... content truncated for token optimization ...]\n\n"
        
        start_part = text[:preserve_start].rsplit('\n', 1)[0] + "\n"
        end_part = "\n" + text[-preserve_end:].split('\n', 1)[1]
        
        result = start_part + truncation_marker + end_part
        
        # Ensure we're under target
        if len(result) > target_chars:
            excess = len(result) - target_chars
            if len(end_part) > excess:
                end_part = end_part[excess:]
                result = start_part + truncation_marker + end_part
        
        return result

    def _optimize_context(self, context_parts: Dict[str, str], target_tokens: int, 
                         model: str, strategy: str) -> Dict[str, Any]:
        """Optimize context parts to fit target token count"""
        analysis = self._analyze_context(context_parts, model)
        
        if not analysis.truncation_needed and analysis.total_tokens <= target_tokens:
            return {
                "optimization_needed": False,
                "original_context": context_parts,
                "optimized_context": context_parts,
                "tokens_saved": 0
            }
        
        # Prioritize truncating user content first, then assistant, preserve system
        optimized_parts = context_parts.copy()
        tokens_to_save = analysis.total_tokens - target_tokens
        
        # Start with user content
        if tokens_to_save > 0 and 'user' in optimized_parts:
            user_target = max(0, analysis.user_tokens - tokens_to_save)
            user_optimization = self._optimize_text(
                optimized_parts['user'], user_target, model, strategy
            )
            optimized_parts['user'] = user_optimization['optimized_text']
            tokens_to_save -= user_optimization['reduction_achieved']
        
        # Then assistant content if still needed
        if tokens_to_save > 0 and 'assistant' in optimized_parts:
            assistant_target = max(0, analysis.assistant_tokens - tokens_to_save)
            assistant_optimization = self._optimize_text(
                optimized_parts['assistant'], assistant_target, model, strategy
            )
            optimized_parts['assistant'] = assistant_optimization['optimized_text']
            tokens_to_save -= assistant_optimization['reduction_achieved']
        
        final_analysis = self._analyze_context(optimized_parts, model)
        
        return {
            "optimization_needed": True,
            "original_context": context_parts,
            "optimized_context": optimized_parts,
            "original_tokens": analysis.total_tokens,
            "final_tokens": final_analysis.total_tokens,
            "tokens_saved": analysis.total_tokens - final_analysis.total_tokens,
            "strategy_used": strategy
        }

    def _prepare_estimate_display(self, result: TokenEstimate, include_cost: bool) -> Dict[str, Any]:
        """Prepare token estimation display data"""
        display_data = {
            "type": "token_estimate",
            "text_length": result.text_length,
            "estimated_tokens": result.estimated_tokens,
            "model": result.model_used,
            "efficiency": f"{result.tokens_per_char:.3f} tokens/char"
        }
        
        if include_cost:
            # Add basic cost estimation (would integrate with actual cost tracker)
            estimated_cost = result.estimated_tokens * 0.00001  # Placeholder rate
            display_data["estimated_cost"] = f"${estimated_cost:.6f}"
        
        return display_data

    def _prepare_analysis_display(self, result: ContextAnalysis) -> Dict[str, Any]:
        """Prepare context analysis display data"""
        return {
            "type": "context_analysis",
            "total_tokens": result.total_tokens,
            "breakdown": {
                "system": result.system_tokens,
                "user": result.user_tokens,
                "assistant": result.assistant_tokens
            },
            "utilization": f"{result.context_utilization:.1%}",
            "remaining_capacity": result.remaining_capacity,
            "truncation_needed": result.truncation_needed
        }

    def _prepare_optimization_display(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Prepare optimization display data"""
        display_data = {
            "type": "text_optimization",
            "optimization_needed": result["optimization_needed"]
        }
        
        if result["optimization_needed"]:
            display_data.update({
                "original_tokens": result["original_tokens"],
                "final_tokens": result.get("final_tokens", result["target_tokens"]),
                "tokens_saved": result.get("reduction_achieved", result.get("tokens_saved", 0)),
                "strategy": result.get("strategy_used", "smart")
            })
        
        return display_data


# Register the tool
from deile.tools.registry import ToolRegistry
ToolRegistry.register("tokenizer", TokenizerTool)