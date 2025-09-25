"""Memory System Data Models - 2025 LLM Agent Architecture"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union
from enum import Enum
from pydantic import BaseModel, Field
import time
from datetime import datetime, timedelta


class MemoryType(Enum):
    """Types of memory in the system"""
    WORKING = "working"
    PERSISTENT = "persistent"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class ContextRelevance(Enum):
    """Relevance levels for context items"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOISE = "noise"


@dataclass
class ContextItem:
    """Individual context item in memory"""
    content: str
    metadata: Dict[str, Any]
    relevance: ContextRelevance
    timestamp: float = field(default_factory=time.time)
    embedding: Optional[List[float]] = None
    source: Optional[str] = None

    @property
    def age_seconds(self) -> float:
        """Age of context item in seconds"""
        return time.time() - self.timestamp

    @property
    def is_stale(self, max_age_hours: int = 24) -> bool:
        """Check if context item is stale"""
        return self.age_seconds > (max_age_hours * 3600)


@dataclass
class Memory:
    """Persistent memory item"""
    memory_id: str
    content: str
    metadata: Dict[str, Any]
    embedding: List[float]
    relevance_score: float
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0

    def update_access(self) -> None:
        """Update access statistics"""
        self.last_accessed = datetime.now()
        self.access_count += 1


@dataclass
class Event:
    """Event in an episode"""
    event_id: str
    event_type: str
    description: str
    timestamp: datetime
    context: Dict[str, Any]
    success: bool
    duration: Optional[timedelta] = None


@dataclass
class Outcome:
    """Outcome of an episode"""
    success: bool
    result: str
    lessons_learned: List[str]
    improvements: List[str]
    failure_reason: Optional[str] = None


@dataclass
class Episode:
    """Episodic memory of task sequence"""
    episode_id: str
    task: str
    start_time: datetime
    end_time: Optional[datetime]
    events: List[Event]
    outcome: Optional[Outcome]
    context: Dict[str, Any]

    @property
    def duration(self) -> Optional[timedelta]:
        """Duration of episode"""
        if self.end_time:
            return self.end_time - self.start_time
        return None

    @property
    def is_complete(self) -> bool:
        """Check if episode is complete"""
        return self.end_time is not None and self.outcome is not None


class MemoryConfig(BaseModel):
    """Configuration for memory system"""

    # Working memory settings
    max_context_length: int = Field(default=8000, description="Maximum working memory context length")
    context_window_overlap: int = Field(default=200, description="Overlap between context windows")

    # Persistent memory settings
    vector_db_config: Dict[str, Any] = Field(default_factory=dict, description="Vector database configuration")
    embedding_model: str = Field(default="all-MiniLM-L6-v2", description="Embedding model for semantic search")
    max_memories_per_query: int = Field(default=10, description="Maximum memories returned per query")
    similarity_threshold: float = Field(default=0.7, description="Minimum similarity for memory retrieval")

    # Episodic memory settings
    max_episodes: int = Field(default=1000, description="Maximum number of episodes to retain")
    episode_retention_days: int = Field(default=30, description="Days to retain completed episodes")

    # Semantic memory settings
    knowledge_graph_config: Dict[str, Any] = Field(default_factory=dict, description="Knowledge graph configuration")
    concept_extraction_enabled: bool = Field(default=True, description="Enable automatic concept extraction")

    # Memory consolidation settings
    consolidation_interval_hours: int = Field(default=24, description="Hours between memory consolidation")
    consolidation_enabled: bool = Field(default=True, description="Enable automatic memory consolidation")

    class Config:
        """Pydantic configuration"""
        validate_assignment = True


@dataclass
class MemoryContext:
    """Enhanced context from memory system"""
    working_context: List[ContextItem]
    relevant_memories: List[Memory]
    similar_episodes: List[Episode]
    semantic_concepts: List[Dict[str, Any]]
    consolidated_summary: Optional[str] = None
    confidence_score: float = 0.0

    @property
    def total_context_items(self) -> int:
        """Total number of context items"""
        return len(self.working_context) + len(self.relevant_memories) + len(self.similar_episodes)

    def get_context_text(self, max_length: int = 4000) -> str:
        """Get consolidated context text with length limit"""
        context_parts = []

        # Add working context
        for item in self.working_context:
            if item.relevance in [ContextRelevance.CRITICAL, ContextRelevance.HIGH]:
                context_parts.append(f"Recent: {item.content}")

        # Add relevant memories
        for memory in self.relevant_memories[:5]:  # Top 5 memories
            context_parts.append(f"Memory: {memory.content}")

        # Add episode insights
        for episode in self.similar_episodes[:3]:  # Top 3 episodes
            if episode.outcome and episode.outcome.success:
                context_parts.append(f"Experience: {episode.task} -> {episode.outcome.result}")

        # Truncate if needed
        full_context = "\n".join(context_parts)
        if len(full_context) > max_length:
            full_context = full_context[:max_length] + "..."

        return full_context


@dataclass
class EnhancedContext:
    """Context enhanced with memory insights"""
    original_query: str
    memory_context: MemoryContext
    extracted_entities: List[str]
    inferred_intent: str
    complexity_score: float
    suggested_approach: Optional[str] = None

    @property
    def context_quality(self) -> str:
        """Assess context quality"""
        if self.memory_context.confidence_score > 0.8:
            return "excellent"
        elif self.memory_context.confidence_score > 0.6:
            return "good"
        elif self.memory_context.confidence_score > 0.4:
            return "fair"
        else:
            return "poor"


@dataclass
class ConsolidationResult:
    """Result of memory consolidation process"""
    consolidation_id: str
    start_time: datetime
    end_time: datetime
    memories_processed: int
    memories_merged: int
    memories_archived: int
    new_insights: List[str]
    performance_improvements: Dict[str, float]

    @property
    def consolidation_duration(self) -> timedelta:
        """Duration of consolidation process"""
        return self.end_time - self.start_time

    @property
    def efficiency_ratio(self) -> float:
        """Ratio of memories merged/archived vs processed"""
        if self.memories_processed == 0:
            return 0.0
        return (self.memories_merged + self.memories_archived) / self.memories_processed