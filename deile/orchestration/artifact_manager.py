"""Artifact Management System for DEILE"""

from pathlib import Path
import json
import time
import hashlib
import uuid
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
import logging
import gzip


logger = logging.getLogger(__name__)


@dataclass
class ArtifactMetadata:
    """Metadata completo de artefatos"""
    run_id: str
    tool_name: str
    sequence: int
    timestamp: float
    input_hash: str
    output_size: int
    execution_time: float
    status: str
    error_info: Optional[Dict[str, Any]] = None
    compressed: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)


class ArtifactManager:
    """Gerenciador central de artefatos"""
    
    def __init__(self, artifacts_dir: Path = None):
        self.artifacts_dir = artifacts_dir or Path("ARTIFACTS")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._sequence_counter = 0
        
    def _generate_run_id(self) -> str:
        """Generate unique run ID"""
        timestamp = int(time.time())
        return f"run_{timestamp}_{uuid.uuid4().hex[:8]}"
        
    def _hash_input(self, input_data: Any) -> str:
        """Generate hash of input data"""
        input_str = json.dumps(input_data, sort_keys=True)
        return hashlib.md5(input_str.encode()).hexdigest()
        
    def _should_compress(self, data_size: int) -> bool:
        """Determine if data should be compressed (>10KB)"""
        return data_size > 10 * 1024
        
    def store_artifact(self, 
                      run_id: str,
                      tool_name: str, 
                      input_data: Dict[str, Any],
                      output_data: Any,
                      execution_time: float,
                      status: str = "success",
                      error_info: Optional[Dict[str, Any]] = None) -> str:
        """Armazena artefato com metadata completo"""
        try:
            self._sequence_counter += 1
            
            # Create run directory
            run_dir = self.artifacts_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate artifact ID
            artifact_id = f"{tool_name}_{self._sequence_counter:03d}"
            
            # Prepare artifact data
            artifact_data = {
                "input": input_data,
                "output": output_data,
                "timestamp": time.time(),
                "execution_time": execution_time,
                "status": status
            }
            
            if error_info:
                artifact_data["error"] = error_info
            
            # Serialize data
            artifact_json = json.dumps(artifact_data, indent=2, default=str)
            artifact_bytes = artifact_json.encode('utf-8')
            
            # Determine if compression is needed
            should_compress = self._should_compress(len(artifact_bytes))
            
            # Save artifact file
            if should_compress:
                artifact_path = run_dir / f"{artifact_id}.json.gz"
                with gzip.open(artifact_path, 'wb') as f:
                    f.write(artifact_bytes)
            else:
                artifact_path = run_dir / f"{artifact_id}.json"
                with open(artifact_path, 'wb') as f:
                    f.write(artifact_bytes)
            
            # Create metadata
            metadata = ArtifactMetadata(
                run_id=run_id,
                tool_name=tool_name,
                sequence=self._sequence_counter,
                timestamp=time.time(),
                input_hash=self._hash_input(input_data),
                output_size=len(artifact_bytes),
                execution_time=execution_time,
                status=status,
                error_info=error_info,
                compressed=should_compress
            )
            
            # Save metadata
            metadata_path = run_dir / f"{artifact_id}_metadata.json"
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata.to_dict(), f, indent=2)
            
            logger.info(f"Artifact stored: {artifact_id} in {run_id}")
            return str(artifact_path)
            
        except Exception as e:
            logger.error(f"Failed to store artifact: {e}")
            raise
            
    def get_artifact(self, artifact_path: str) -> Dict[str, Any]:
        """Recupera artefato por path"""
        try:
            artifact_path = Path(artifact_path)
            
            if artifact_path.suffix == '.gz':
                with gzip.open(artifact_path, 'rt', encoding='utf-8') as f:
                    return json.load(f)
            else:
                with open(artifact_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
                    
        except Exception as e:
            logger.error(f"Failed to get artifact {artifact_path}: {e}")
            raise
            
    def get_artifact_metadata(self, artifact_path: str) -> Optional[ArtifactMetadata]:
        """Get artifact metadata"""
        try:
            artifact_path = Path(artifact_path)
            metadata_path = artifact_path.parent / f"{artifact_path.stem}_metadata.json"
            
            if metadata_path.exists():
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return ArtifactMetadata(**data)
            return None
            
        except Exception as e:
            logger.error(f"Failed to get artifact metadata: {e}")
            return None
            
    def list_run_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        """List all artifacts for a run"""
        try:
            run_dir = self.artifacts_dir / run_id
            if not run_dir.exists():
                return []
                
            artifacts = []
            for artifact_file in run_dir.glob("*.json"):
                if not artifact_file.stem.endswith("_metadata"):
                    artifact_data = self.get_artifact(artifact_file)
                    metadata = self.get_artifact_metadata(artifact_file)
                    
                    artifacts.append({
                        "path": str(artifact_file),
                        "metadata": metadata.to_dict() if metadata else None,
                        "size": artifact_file.stat().st_size
                    })
                    
            return sorted(artifacts, key=lambda x: x["metadata"]["sequence"] if x["metadata"] else 0)
            
        except Exception as e:
            logger.error(f"Failed to list artifacts for run {run_id}: {e}")
            return []
            
    def cleanup_old_artifacts(self, days_old: int = 30) -> int:
        """Clean up artifacts older than specified days"""
        try:
            cutoff_time = time.time() - (days_old * 24 * 60 * 60)
            cleaned_count = 0
            
            for run_dir in self.artifacts_dir.iterdir():
                if run_dir.is_dir():
                    # Check if run is old enough
                    oldest_file_time = min((f.stat().st_mtime for f in run_dir.iterdir() if f.is_file()), default=time.time())
                    
                    if oldest_file_time < cutoff_time:
                        # Remove entire run directory
                        import shutil
                        shutil.rmtree(run_dir)
                        cleaned_count += 1
                        logger.info(f"Cleaned up old run: {run_dir.name}")
                        
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup artifacts: {e}")
            return 0
            
    def get_storage_stats(self) -> Dict[str, Any]:
        """Get artifact storage statistics"""
        try:
            total_size = 0
            total_files = 0
            run_count = 0
            
            for run_dir in self.artifacts_dir.iterdir():
                if run_dir.is_dir():
                    run_count += 1
                    for file_path in run_dir.rglob("*"):
                        if file_path.is_file():
                            total_files += 1
                            total_size += file_path.stat().st_size
                            
            return {
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "total_files": total_files,
                "run_count": run_count,
                "storage_dir": str(self.artifacts_dir)
            }
            
        except Exception as e:
            logger.error(f"Failed to get storage stats: {e}")
            return {}