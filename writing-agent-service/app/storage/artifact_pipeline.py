"""向后兼容模块；新代码请从 app.services.artifact_pipeline 导入。"""

from app.services.artifact_pipeline import ArtifactPipeline

__all__ = ["ArtifactPipeline"]
