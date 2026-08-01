from app.core.contracts import StageId
from .base import SkeletonStage


class S7PersistStateStage(SkeletonStage):
    stage_id = StageId.S7
