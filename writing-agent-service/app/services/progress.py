from dataclasses import dataclass

from app.domain.execution import StepType
from app.schemas.events import ProgressValue


class InvalidProgressInput(ValueError):
    pass


@dataclass(frozen=True)
class ProgressContext:
    step: StepType | None
    completed_sections: int = 0
    total_sections: int = 0
    previous_percent: int = 0


class ProgressCalculator:
    """由 Python 状态机计算进度，禁止 Agent 自己生成百分比。"""

    _STEP_PERCENT = {
        StepType.RESEARCH: 15,
        StepType.OUTLINE: 25,
        StepType.ASSEMBLE: 75,
        StepType.REVIEW: 90,
        StepType.SECTION_REVISE: 85,
        StepType.FINALIZE: 100,
    }

    def calculate(self, context: ProgressContext) -> ProgressValue:
        self._validate(context)
        if context.step == StepType.SECTION_DRAFT:
            ratio = context.completed_sections / context.total_sections
            calculated = 25 + round(ratio * 40)
            completed = context.completed_sections
            total = context.total_sections
        elif context.step is None:
            calculated = 0
            completed = 0
            total = 0
        else:
            calculated = self._STEP_PERCENT[context.step]
            completed = 1
            total = 1

        # 审核返工后可能再次出现 assemble 等步骤，进度不能倒退。
        percent = max(calculated, context.previous_percent)
        return ProgressValue(
            completed=completed,
            total=total,
            percent=min(percent, 100),
        )

    @staticmethod
    def _validate(context: ProgressContext) -> None:
        if not 0 <= context.previous_percent <= 100:
            raise InvalidProgressInput("previous_percent 必须在 0 到 100 之间")
        if context.step == StepType.SECTION_DRAFT:
            if context.total_sections <= 0:
                raise InvalidProgressInput("章节写作必须提供大于 0 的 total_sections")
            if not 0 <= context.completed_sections <= context.total_sections:
                raise InvalidProgressInput(
                    "completed_sections 必须在 0 和 total_sections 之间"
                )
        elif context.completed_sections or context.total_sections:
            raise InvalidProgressInput("只有章节写作步骤可以提供章节进度")
