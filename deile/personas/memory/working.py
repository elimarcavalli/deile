"""Working Memory Implementation - Short-term context management"""

import time
import logging
from collections import deque
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta

from .models import ContextItem, ContextRelevance, MemoryContext

logger = logging.getLogger(__name__)


class WorkingMemory:
    """Short-term context for active sessions

    Implements a sophisticated working memory system that maintains
    relevant context for ongoing conversations and tasks.
    """

    def __init__(self, max_context_length: int = 8000, context_window_overlap: int = 200):
        self.max_context_length = max_context_length
        self.context_window_overlap = context_window_overlap

        # Context storage
        self.context_buffer = deque(maxlen=max_context_length)
        self.current_task_state: Dict[str, Any] = {}
        self.active_tools: Set[str] = set()
        self.reasoning_trace: List[Dict[str, Any]] = []

        # Attention mechanism
        self.attention_weights: Dict[str, float] = {}
        self.relevance_cache: Dict[str, ContextRelevance] = {}

        # Performance tracking
        self.access_stats: Dict[str, int] = {}
        self.last_cleanup: float = time.time()

        logger.info(f"WorkingMemory initialized with max_length={max_context_length}")

    async def add_context(self, item: ContextItem) -> None:
        """Add context item to working memory

        Args:
            item: Context item to add
        """
        try:
            # Update relevance if not set
            if item.relevance == ContextRelevance.MEDIUM:
                item.relevance = await self._calculate_relevance(item)

            # Add to buffer
            self.context_buffer.append(item)

            # Update attention weights
            await self._update_attention_weights(item)

            # Cache relevance for future use
            item_hash = hash(item.content[:100])  # Hash first 100 chars
            self.relevance_cache[str(item_hash)] = item.relevance

            # Periodic cleanup
            if time.time() - self.last_cleanup > 300:  # 5 minutes
                await self._cleanup_stale_context()

            logger.debug(f"Added context item with relevance {item.relevance.value}")

        except Exception as e:
            logger.error(f"Error adding context item: {e}")
            raise

    async def get_relevant_context(self, query: str, max_items: int = 10) -> List[ContextItem]:
        """Get most relevant context items for query

        Args:
            query: Query to find relevant context for
            max_items: Maximum number of items to return

        Returns:
            List of most relevant context items
        """
        try:
            # Update access stats
            query_hash = hash(query[:50])
            self.access_stats[str(query_hash)] = self.access_stats.get(str(query_hash), 0) + 1

            # Calculate relevance scores for each context item
            scored_items = []

            for item in self.context_buffer:
                relevance_score = await self._calculate_context_relevance(item, query)
                scored_items.append((item, relevance_score))

            # Sort by relevance score (descending)
            scored_items.sort(key=lambda x: x[1], reverse=True)

            # Take top items
            relevant_items = [item for item, score in scored_items[:max_items]]

            logger.debug(f"Retrieved {len(relevant_items)} relevant context items for query")
            return relevant_items

        except Exception as e:
            logger.error(f"Error retrieving relevant context: {e}")
            return []

    async def clear_context(self) -> None:
        """Clear working memory context"""
        try:
            self.context_buffer.clear()
            self.current_task_state.clear()
            self.active_tools.clear()
            self.reasoning_trace.clear()
            self.attention_weights.clear()
            self.relevance_cache.clear()

            logger.info("Working memory context cleared")

        except Exception as e:
            logger.error(f"Error clearing context: {e}")
            raise

    async def summarize_context(self, max_length: int = 500) -> str:
        """Summarize current context

        Args:
            max_length: Maximum length of summary

        Returns:
            Context summary string
        """
        try:
            if not self.context_buffer:
                return "No context available."

            # Group context by relevance
            critical_items = []
            high_items = []
            medium_items = []

            for item in self.context_buffer:
                if item.relevance == ContextRelevance.CRITICAL:
                    critical_items.append(item.content)
                elif item.relevance == ContextRelevance.HIGH:
                    high_items.append(item.content)
                elif item.relevance == ContextRelevance.MEDIUM:
                    medium_items.append(item.content)

            # Build summary
            summary_parts = []

            if critical_items:
                summary_parts.append(f"Critical: {'; '.join(critical_items[:3])}")

            if high_items:
                summary_parts.append(f"Important: {'; '.join(high_items[:5])}")

            if medium_items and len(summary_parts) < 2:
                summary_parts.append(f"Context: {'; '.join(medium_items[:3])}")

            # Current task state
            if self.current_task_state:
                task_info = ', '.join([f"{k}: {v}" for k, v in self.current_task_state.items()])
                summary_parts.append(f"Task state: {task_info}")

            # Active tools
            if self.active_tools:
                summary_parts.append(f"Active tools: {', '.join(self.active_tools)}")

            full_summary = ". ".join(summary_parts)

            # Truncate if needed
            if len(full_summary) > max_length:
                full_summary = full_summary[:max_length - 3] + "..."

            logger.debug(f"Generated context summary: {len(full_summary)} characters")
            return full_summary

        except Exception as e:
            logger.error(f"Error summarizing context: {e}")
            return "Error generating context summary."

    async def update_task_state(self, key: str, value: Any) -> None:
        """Update current task state

        Args:
            key: State key
            value: State value
        """
        try:
            self.current_task_state[key] = value

            # Add as context item if important
            if key in ['current_goal', 'active_task', 'main_objective']:
                context_item = ContextItem(
                    content=f"{key}: {value}",
                    metadata={'type': 'task_state', 'key': key},
                    relevance=ContextRelevance.HIGH,
                    source='task_state'
                )
                await self.add_context(context_item)

            logger.debug(f"Updated task state: {key} = {value}")

        except Exception as e:
            logger.error(f"Error updating task state: {e}")
            raise

    async def add_tool_usage(self, tool_name: str, tool_input: Dict[str, Any], tool_output: str) -> None:
        """Record tool usage in working memory

        Args:
            tool_name: Name of tool used
            tool_input: Input provided to tool
            tool_output: Output from tool
        """
        try:
            self.active_tools.add(tool_name)

            # Create context item for tool usage
            context_item = ContextItem(
                content=f"Used {tool_name}: {tool_output}",
                metadata={
                    'type': 'tool_usage',
                    'tool_name': tool_name,
                    'tool_input': tool_input,
                    'timestamp': time.time()
                },
                relevance=ContextRelevance.HIGH,
                source='tool_execution'
            )

            await self.add_context(context_item)

            logger.debug(f"Recorded tool usage: {tool_name}")

        except Exception as e:
            logger.error(f"Error recording tool usage: {e}")
            raise

    async def add_reasoning_step(self, thought: str, action: str, observation: str) -> None:
        """Add reasoning step to trace

        Args:
            thought: Reasoning thought
            action: Action taken
            observation: Observation from action
        """
        try:
            reasoning_step = {
                'step': len(self.reasoning_trace) + 1,
                'thought': thought,
                'action': action,
                'observation': observation,
                'timestamp': time.time()
            }

            self.reasoning_trace.append(reasoning_step)

            # Add as context item
            context_item = ContextItem(
                content=f"Reasoning: {thought} -> {action} -> {observation}",
                metadata={
                    'type': 'reasoning_step',
                    'step': reasoning_step['step']
                },
                relevance=ContextRelevance.MEDIUM,
                source='reasoning_trace'
            )

            await self.add_context(context_item)

            logger.debug(f"Added reasoning step {reasoning_step['step']}")

        except Exception as e:
            logger.error(f"Error adding reasoning step: {e}")
            raise

    async def get_memory_stats(self) -> Dict[str, Any]:
        """Get working memory statistics

        Returns:
            Dictionary of memory statistics
        """
        try:
            total_items = len(self.context_buffer)
            relevance_counts = {}

            for item in self.context_buffer:
                relevance_counts[item.relevance.value] = relevance_counts.get(item.relevance.value, 0) + 1

            return {
                'total_context_items': total_items,
                'max_capacity': self.max_context_length,
                'utilization_percent': (total_items / self.max_context_length) * 100,
                'relevance_distribution': relevance_counts,
                'active_tools_count': len(self.active_tools),
                'reasoning_steps': len(self.reasoning_trace),
                'task_state_keys': len(self.current_task_state),
                'attention_weights_count': len(self.attention_weights),
                'cache_size': len(self.relevance_cache),
                'access_patterns': dict(list(self.access_stats.items())[:10])  # Top 10
            }

        except Exception as e:
            logger.error(f"Error getting memory stats: {e}")
            return {}

    async def _calculate_relevance(self, item: ContextItem) -> ContextRelevance:
        """Calculate relevance of context item

        Args:
            item: Context item to analyze

        Returns:
            Calculated relevance level
        """
        try:
            content = item.content.lower()

            # Keywords that indicate high relevance
            critical_keywords = ['error', 'failed', 'urgent', 'critical', 'immediate']
            high_keywords = ['important', 'goal', 'objective', 'task', 'result']

            # Check metadata
            if item.metadata.get('type') == 'tool_usage':
                return ContextRelevance.HIGH
            elif item.metadata.get('type') == 'task_state':
                return ContextRelevance.CRITICAL

            # Check content keywords
            if any(keyword in content for keyword in critical_keywords):
                return ContextRelevance.CRITICAL
            elif any(keyword in content for keyword in high_keywords):
                return ContextRelevance.HIGH

            # Check recency
            if item.age_seconds < 300:  # 5 minutes
                return ContextRelevance.HIGH
            elif item.age_seconds < 1800:  # 30 minutes
                return ContextRelevance.MEDIUM

            return ContextRelevance.LOW

        except Exception as e:
            logger.error(f"Error calculating relevance: {e}")
            return ContextRelevance.MEDIUM

    async def _calculate_context_relevance(self, item: ContextItem, query: str) -> float:
        """Calculate relevance score of context item to query

        Args:
            item: Context item
            query: Query string

        Returns:
            Relevance score (0.0 to 1.0)
        """
        try:
            score = 0.0

            # Base relevance
            relevance_scores = {
                ContextRelevance.CRITICAL: 1.0,
                ContextRelevance.HIGH: 0.8,
                ContextRelevance.MEDIUM: 0.6,
                ContextRelevance.LOW: 0.4,
                ContextRelevance.NOISE: 0.2
            }
            score = relevance_scores.get(item.relevance, 0.5)

            # Content similarity (simple keyword matching)
            query_words = set(query.lower().split())
            content_words = set(item.content.lower().split())

            if query_words and content_words:
                overlap = len(query_words.intersection(content_words))
                similarity = overlap / len(query_words.union(content_words))
                score += similarity * 0.5

            # Recency bonus
            age_hours = item.age_seconds / 3600
            if age_hours < 1:
                score += 0.2
            elif age_hours < 6:
                score += 0.1

            # Attention weight
            item_hash = hash(item.content[:100])
            attention_weight = self.attention_weights.get(str(item_hash), 1.0)
            score *= attention_weight

            return min(score, 1.0)

        except Exception as e:
            logger.error(f"Error calculating context relevance: {e}")
            return 0.5

    async def _update_attention_weights(self, item: ContextItem) -> None:
        """Update attention weights based on item

        Args:
            item: Context item that was added
        """
        try:
            item_hash = hash(item.content[:100])

            # Increase attention for high-relevance items
            if item.relevance in [ContextRelevance.CRITICAL, ContextRelevance.HIGH]:
                self.attention_weights[str(item_hash)] = 1.2
            else:
                self.attention_weights[str(item_hash)] = 1.0

        except Exception as e:
            logger.error(f"Error updating attention weights: {e}")

    async def _cleanup_stale_context(self) -> None:
        """Clean up stale context items"""
        try:
            items_before = len(self.context_buffer)

            # Remove very old items with low relevance
            current_time = time.time()
            items_to_keep = []

            for item in self.context_buffer:
                age_hours = (current_time - item.timestamp) / 3600

                # Keep if recent or high relevance
                if (age_hours < 24 or
                    item.relevance in [ContextRelevance.CRITICAL, ContextRelevance.HIGH]):
                    items_to_keep.append(item)

            # Update buffer
            self.context_buffer.clear()
            self.context_buffer.extend(items_to_keep)

            # Clean attention weights
            valid_hashes = {str(hash(item.content[:100])) for item in items_to_keep}
            self.attention_weights = {
                k: v for k, v in self.attention_weights.items()
                if k in valid_hashes
            }

            # Clean relevance cache
            self.relevance_cache = {
                k: v for k, v in self.relevance_cache.items()
                if k in valid_hashes
            }

            self.last_cleanup = current_time
            items_removed = items_before - len(self.context_buffer)

            if items_removed > 0:
                logger.info(f"Cleaned up {items_removed} stale context items")

        except Exception as e:
            logger.error(f"Error during context cleanup: {e}")