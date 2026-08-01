from app.core.contracts import StageId
from .base import SkeletonStage


class S3DstStage(SkeletonStage):
    stage_id = StageId.S3
