"""定义任务目标。 JobScenario 区分“交付研究资料包”和“辅助完成文章写作”两种业务场景。"""

from enum import Enum

class JobScenario(str, Enum):
    """运营任务想要交付的业务结果，与内部 Agent 数量无关。"""

    RESEARCH_PACKAGE = "research_package"
    ASSISTED_WRITING = "assisted_writing"