"""Persistent Memory Implementation - Long-term vector-based storage"""

import logging
import hashlib
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import asyncio

from .models import Memory, MemoryConfig

logger = logging.getLogger(__name__)

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    logger.warning("sentence-transformers not available, using mock embeddings")

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    logger.warning("numpy not available, using alternative implementations")


class MockVectorDB:
    """Mock vector database for development/testing"""

    def __init__(self):
        self.memories: Dict[str, Memory] = {}
        self.embeddings: Dict[str, List[float]] = {}

    async def store(self, memory_id: str, embedding: List[float], metadata: Dict[str, Any]) -> None:
        """Store embedding with metadata"""
        self.embeddings[memory_id] = embedding

    async def search(self, query_embedding: List[float], k: int = 5, threshold: float = 0.7) -> List[str]:
        """Search for similar embeddings"""
        if not NUMPY_AVAILABLE:
            # Return some mock results
            return list(self.embeddings.keys())[:k]

        import numpy as np

        results = []
        query_vec = np.array(query_embedding)

        for memory_id, embedding in self.embeddings.items():
            memory_vec = np.array(embedding)
            similarity = np.dot(query_vec, memory_vec) / (np.linalg.norm(query_vec) * np.linalg.norm(memory_vec))

            if similarity >= threshold:
                results.append((memory_id, similarity))

        # Sort by similarity descending
        results.sort(key=lambda x: x[1], reverse=True)
        return [memory_id for memory_id, _ in results[:k]]

    async def delete(self, memory_id: str) -> None:
        """Delete embedding"""
        self.embeddings.pop(memory_id, None)


class PersistentMemory:
    """Long-term recall via vector database

    Implements sophisticated persistent memory storage using vector embeddings
    for semantic search and retrieval.
    """

    def __init__(self, config: MemoryConfig, persona_id: str):
        self.config = config
        self.persona_id = persona_id

        # Initialize embedding model
        if SENTENCE_TRANSFORMERS_AVAILABLE:
            self.embedding_model = SentenceTransformer(config.embedding_model)
        else:
            self.embedding_model = None

        # Initialize vector database (mock for now)
        self.vector_db = MockVectorDB()

        # Memory storage
        self.memories: Dict[str, Memory] = {}

        # Performance tracking
        self.query_stats = {
            'total_queries': 0,
            'cache_hits': 0,
            'avg_query_time': 0.0
        }

        # Cache for recent queries
        self.query_cache: Dict[str, List[Memory]] = {}
        self.cache_ttl = 300  # 5 minutes

        logger.info(f"PersistentMemory initialized for persona {persona_id}")

    async def store_memory(self, content: str, metadata: Dict[str, Any]) -> str:
        """Store memory with vector embedding

        Args:
            content: Memory content to store
            metadata: Additional metadata

        Returns:
            Memory ID
        """
        try:
            # Generate memory ID
            memory_id = self._generate_memory_id(content)

            # Generate embedding
            embedding = await self._generate_embedding(content)

            # Create memory object
            memory = Memory(
                memory_id=memory_id,
                content=content,
                metadata=metadata,
                embedding=embedding,
                relevance_score=await self._calculate_initial_relevance(content, metadata),
                created_at=datetime.now(),
                last_accessed=datetime.now(),
                access_count=0
            )

            # Store in local memory
            self.memories[memory_id] = memory

            # Store in vector database
            await self.vector_db.store(memory_id, embedding, {
                'persona_id': self.persona_id,
                'content': content,
                'metadata': metadata,
                'created_at': memory.created_at.isoformat()
            })

            # Clear relevant cache entries
            await self._invalidate_cache()

            logger.debug(f"Stored memory {memory_id} with relevance {memory.relevance_score}")
            return memory_id

        except Exception as e:
            logger.error(f"Error storing memory: {e}")
            raise

    async def retrieve_memories(self, query: str, k: int = 5, threshold: float = None) -> List[Memory]:
        """Retrieve similar memories using vector search

        Args:
            query: Query string
            k: Maximum number of memories to return
            threshold: Minimum similarity threshold

        Returns:
            List of relevant memories
        """
        try:
            start_time = time.time()
            self.query_stats['total_queries'] += 1

            # Use configured threshold if not provided
            if threshold is None:
                threshold = self.config.similarity_threshold

            # Check cache first
            cache_key = hashlib.md5(f"{query}_{k}_{threshold}".encode()).hexdigest()
            if cache_key in self.query_cache:
                cached_result = self.query_cache[cache_key]
                if time.time() - cached_result[0].last_accessed.timestamp() < self.cache_ttl:
                    self.query_stats['cache_hits'] += 1
                    logger.debug(f"Cache hit for query: {query[:50]}")
                    return cached_result

            # Generate query embedding
            query_embedding = await self._generate_embedding(query)

            # Search vector database
            similar_memory_ids = await self.vector_db.search(
                query_embedding, k=k * 2, threshold=threshold  # Get more candidates
            )

            # Retrieve and rank memories
            candidate_memories = []
            for memory_id in similar_memory_ids:
                if memory_id in self.memories:
                    memory = self.memories[memory_id]
                    # Update access statistics
                    memory.update_access()
                    candidate_memories.append(memory)

            # Re-rank based on multiple factors
            ranked_memories = await self._rank_memories(candidate_memories, query)

            # Take top k
            result_memories = ranked_memories[:k]

            # Cache result
            self.query_cache[cache_key] = result_memories

            # Update stats
            query_time = time.time() - start_time
            self.query_stats['avg_query_time'] = (
                (self.query_stats['avg_query_time'] * (self.query_stats['total_queries'] - 1) + query_time)
                / self.query_stats['total_queries']
            )

            logger.debug(f"Retrieved {len(result_memories)} memories for query in {query_time:.3f}s")
            return result_memories

        except Exception as e:
            logger.error(f"Error retrieving memories: {e}")
            return []

    async def update_memory(self, memory_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Update existing memory

        Args:
            memory_id: ID of memory to update
            content: New content
            metadata: New metadata (optional)
        """
        try:
            if memory_id not in self.memories:
                raise ValueError(f"Memory {memory_id} not found")

            memory = self.memories[memory_id]

            # Update content and generate new embedding if content changed
            if content != memory.content:
                memory.content = content
                memory.embedding = await self._generate_embedding(content)

                # Update in vector database
                await self.vector_db.store(memory_id, memory.embedding, {
                    'persona_id': self.persona_id,
                    'content': content,
                    'metadata': metadata or memory.metadata,
                    'updated_at': datetime.now().isoformat()
                })

            # Update metadata if provided
            if metadata is not None:
                memory.metadata.update(metadata)

            # Recalculate relevance
            memory.relevance_score = await self._calculate_initial_relevance(content, memory.metadata)

            # Invalidate cache
            await self._invalidate_cache()

            logger.debug(f"Updated memory {memory_id}")

        except Exception as e:
            logger.error(f"Error updating memory: {e}")
            raise

    async def delete_memory(self, memory_id: str) -> None:
        """Delete memory

        Args:
            memory_id: ID of memory to delete
        """
        try:
            if memory_id in self.memories:
                del self.memories[memory_id]

            # Delete from vector database
            await self.vector_db.delete(memory_id)

            # Invalidate cache
            await self._invalidate_cache()

            logger.debug(f"Deleted memory {memory_id}")

        except Exception as e:
            logger.error(f"Error deleting memory: {e}")
            raise

    async def get_memory_stats(self) -> Dict[str, Any]:
        """Get persistent memory statistics

        Returns:
            Dictionary of memory statistics
        """
        try:
            total_memories = len(self.memories)

            # Calculate average relevance
            if total_memories > 0:
                avg_relevance = sum(m.relevance_score for m in self.memories.values()) / total_memories

                # Calculate access patterns
                total_accesses = sum(m.access_count for m in self.memories.values())
                recently_accessed = sum(
                    1 for m in self.memories.values()
                    if (datetime.now() - m.last_accessed).days < 7
                )
            else:
                avg_relevance = 0.0
                total_accesses = 0
                recently_accessed = 0

            return {
                'total_memories': total_memories,
                'average_relevance_score': avg_relevance,
                'total_accesses': total_accesses,
                'recently_accessed_count': recently_accessed,
                'query_statistics': self.query_stats.copy(),
                'cache_size': len(self.query_cache),
                'embedding_model': self.config.embedding_model,
                'similarity_threshold': self.config.similarity_threshold,
                'max_memories_per_query': self.config.max_memories_per_query
            }

        except Exception as e:
            logger.error(f"Error getting memory stats: {e}")
            return {}

    async def consolidate_memories(self) -> Dict[str, Any]:
        """Consolidate and optimize memory storage

        Returns:
            Consolidation results
        """
        try:
            start_time = time.time()
            initial_count = len(self.memories)

            # Find memories to consolidate
            consolidated_count = 0
            archived_count = 0

            # Group similar memories
            memory_groups = await self._group_similar_memories()

            for group in memory_groups:
                if len(group) > 1:
                    # Consolidate group into single memory
                    consolidated_memory = await self._consolidate_memory_group(group)
                    if consolidated_memory:
                        consolidated_count += len(group) - 1

            # Archive old, low-relevance memories
            cutoff_date = datetime.now() - timedelta(days=self.config.episode_retention_days)
            for memory_id, memory in list(self.memories.items()):
                if (memory.last_accessed < cutoff_date and
                    memory.relevance_score < 0.3 and
                    memory.access_count < 2):
                    await self.delete_memory(memory_id)
                    archived_count += 1

            # Clear cache
            await self._invalidate_cache()

            consolidation_time = time.time() - start_time

            result = {
                'initial_memory_count': initial_count,
                'final_memory_count': len(self.memories),
                'consolidated_count': consolidated_count,
                'archived_count': archived_count,
                'consolidation_time_seconds': consolidation_time
            }

            logger.info(f"Memory consolidation completed: {result}")
            return result

        except Exception as e:
            logger.error(f"Error during memory consolidation: {e}")
            return {}

    async def _generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for text

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        try:
            if self.embedding_model and SENTENCE_TRANSFORMERS_AVAILABLE:
                embedding = self.embedding_model.encode([text])[0]
                return embedding.tolist()
            else:
                # Mock embedding for development
                import hashlib
                text_hash = hashlib.md5(text.encode()).hexdigest()
                # Create deterministic "embedding" from hash
                embedding = [float(int(text_hash[i:i+2], 16)) / 255.0 for i in range(0, min(len(text_hash), 64), 2)]
                # Pad or truncate to 384 dimensions (common size)
                while len(embedding) < 384:
                    embedding.extend(embedding[:min(384 - len(embedding), len(embedding))])
                return embedding[:384]

        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            # Return zero vector as fallback
            return [0.0] * 384

    def _generate_memory_id(self, content: str) -> str:
        """Generate unique memory ID

        Args:
            content: Memory content

        Returns:
            Unique memory ID
        """
        timestamp = str(time.time())
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        return f"{self.persona_id}_{timestamp}_{content_hash}"

    async def _calculate_initial_relevance(self, content: str, metadata: Dict[str, Any]) -> float:
        """Calculate initial relevance score for memory

        Args:
            content: Memory content
            metadata: Memory metadata

        Returns:
            Relevance score (0.0 to 1.0)
        """
        try:
            score = 0.5  # Base score

            # Content-based scoring
            content_lower = content.lower()

            # High value indicators
            high_value_keywords = ['success', 'solution', 'result', 'achievement', 'learned']
            if any(keyword in content_lower for keyword in high_value_keywords):
                score += 0.2

            # Error/failure learning value
            error_keywords = ['error', 'failed', 'mistake', 'wrong']
            if any(keyword in content_lower for keyword in error_keywords):
                score += 0.15  # Failures are valuable for learning

            # Metadata-based scoring
            if metadata.get('type') == 'successful_task':
                score += 0.2
            elif metadata.get('type') == 'tool_usage':
                score += 0.1

            # Complexity bonus
            if len(content) > 200:  # Longer content often more valuable
                score += 0.1

            return min(score, 1.0)

        except Exception as e:
            logger.error(f"Error calculating relevance: {e}")
            return 0.5

    async def _rank_memories(self, memories: List[Memory], query: str) -> List[Memory]:
        """Rank memories by relevance to query

        Args:
            memories: List of candidate memories
            query: Query string

        Returns:
            Ranked list of memories
        """
        try:
            if not memories:
                return []

            # Calculate ranking scores
            scored_memories = []

            for memory in memories:
                score = memory.relevance_score  # Base relevance

                # Recency bonus
                age_days = (datetime.now() - memory.created_at).days
                if age_days < 7:
                    score += 0.1
                elif age_days < 30:
                    score += 0.05

                # Access frequency bonus
                if memory.access_count > 5:
                    score += 0.1
                elif memory.access_count > 1:
                    score += 0.05

                # Simple text matching bonus
                query_words = set(query.lower().split())
                content_words = set(memory.content.lower().split())
                if query_words and content_words:
                    overlap = len(query_words.intersection(content_words))
                    text_similarity = overlap / len(query_words.union(content_words))
                    score += text_similarity * 0.2

                scored_memories.append((memory, score))

            # Sort by score descending
            scored_memories.sort(key=lambda x: x[1], reverse=True)

            return [memory for memory, score in scored_memories]

        except Exception as e:
            logger.error(f"Error ranking memories: {e}")
            return memories

    async def _group_similar_memories(self) -> List[List[Memory]]:
        """Group similar memories for consolidation

        Returns:
            List of memory groups
        """
        try:
            # Simple grouping by content similarity
            # In production, use more sophisticated clustering
            groups = []
            processed = set()

            for memory_id, memory in self.memories.items():
                if memory_id in processed:
                    continue

                group = [memory]
                processed.add(memory_id)

                # Find similar memories
                for other_id, other_memory in self.memories.items():
                    if other_id in processed:
                        continue

                    # Simple similarity check
                    similarity = await self._calculate_memory_similarity(memory, other_memory)
                    if similarity > 0.8:  # High similarity threshold
                        group.append(other_memory)
                        processed.add(other_id)

                if len(group) > 1:
                    groups.append(group)

            return groups

        except Exception as e:
            logger.error(f"Error grouping memories: {e}")
            return []

    async def _calculate_memory_similarity(self, memory1: Memory, memory2: Memory) -> float:
        """Calculate similarity between two memories

        Args:
            memory1: First memory
            memory2: Second memory

        Returns:
            Similarity score (0.0 to 1.0)
        """
        try:
            if not NUMPY_AVAILABLE:
                # Simple text-based similarity
                words1 = set(memory1.content.lower().split())
                words2 = set(memory2.content.lower().split())
                if not words1 or not words2:
                    return 0.0
                overlap = len(words1.intersection(words2))
                return overlap / len(words1.union(words2))

            import numpy as np

            # Vector similarity
            vec1 = np.array(memory1.embedding)
            vec2 = np.array(memory2.embedding)

            similarity = np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))
            return float(similarity)

        except Exception as e:
            logger.error(f"Error calculating memory similarity: {e}")
            return 0.0

    async def _consolidate_memory_group(self, group: List[Memory]) -> Optional[Memory]:
        """Consolidate a group of similar memories

        Args:
            group: List of memories to consolidate

        Returns:
            Consolidated memory or None
        """
        try:
            if len(group) < 2:
                return None

            # Create consolidated content
            contents = [memory.content for memory in group]
            consolidated_content = f"Consolidated from {len(group)} memories: " + "; ".join(contents)

            # Merge metadata
            consolidated_metadata = {'type': 'consolidated', 'source_count': len(group)}
            for memory in group:
                consolidated_metadata.update(memory.metadata)

            # Use highest relevance score
            max_relevance = max(memory.relevance_score for memory in group)

            # Delete original memories
            for memory in group:
                await self.delete_memory(memory.memory_id)

            # Create consolidated memory
            consolidated_id = await self.store_memory(consolidated_content, consolidated_metadata)

            if consolidated_id and consolidated_id in self.memories:
                consolidated_memory = self.memories[consolidated_id]
                consolidated_memory.relevance_score = max_relevance
                return consolidated_memory

            return None

        except Exception as e:
            logger.error(f"Error consolidating memory group: {e}")
            return None

    async def _invalidate_cache(self) -> None:
        """Invalidate query cache"""
        try:
            self.query_cache.clear()
        except Exception as e:
            logger.error(f"Error invalidating cache: {e}")