"""
热点Worker的依赖装配入口。

热点编排层只依赖四个领域 Port (行为数据、基线提供方、内容仓储、编排策略),
他们的真实实现来自企业 RPC SDK, 不属于本仓库。因此这边只在运行期按照配置解析
一个「依赖工厂」,把企业的 Adapter 的装配留在部署侧。

HOT_NEWS_DEPENDENCIES_FACTORY=your_package.hot_news_writing:build_dependencies

工厂签名固定为‘(Settings) -> 依赖项’。未配置时直接抛出错误，不提供任何隐式的默认值。
避免生产链路悄悄跑在本地假数据上。
"""

from __future__ import annotations  # 函数/变量注解在定义时不求值，而是以字符串形式保存

import importlib
from typing import Protocol         # 让这个对象由哪些方法/属性来定义类型，而不是它继承谁

from app.analytics.data_source import BehaviorDataSource
from app.analytics.news_content import NewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.config import Settings
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)

REQUIRED_ATTRIBUTES = (
    "behavior_data_source",
    "baseline_provider",
    "content_repository",
    "policy",
)

class HotNewsDependencyConfigError(RuntimeError):
    """依赖工厂没有配置、路径非法，或返回值不足以依赖束契约。"""

class HotNewsDependencyBundle(Protocol):
    """热点分析启动所需要的最小依赖"""
    behavior_data_source: BehaviorDataSource
    baseline_provider: HotNewsBaselineProvider
    content_repository: NewsContentRepository
    policy: HotNewsOrchestrationPolicy
    knowledge_search: KnowledgeSearchClient | None

def resolve_dependency_factory(target: str):
    """把“package.module:callable”解析为可调用对象。"""
    module_path, separator, attribute = target.partition(":")
    if not separator or not module_path.strip() or not attribute.strip():
        raise HotNewsDependencyConfigError(
            "HOT_NEWS_DEPENDENCIES_FACTORY 必须是 'package.module:callable' 形式，"
            f"当前值为 {target!r}"
        )

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise HotNewsDependencyConfigError(
            f"无法导入模块 {module_path!r}: {exc}"
        ) from exc

    factory = getattr(module, attribute, None)
    if factory is None:
        raise HotNewsDependencyConfigError(
            f"模块 {module_path!r} 中不存在属性 {attribute!r}"
        )
    if not callable(factory):
        raise HotNewsDependencyConfigError(
            f"HOT_NEWS_DEPENDENCIES_FACTORY 解析出的 {target!r} 不可调用"
        )
    return factory

def build_hot_news_dependencies(settings: Settings) -> HotNewsDependencyBundle:
    """按配置装配热点 Worker 依赖；未配置则 fail-closed。"""

    target = (settings.hot_news_dependencies_factory or "").strip()
    if not target:
        raise HotNewsDependencyConfigError(
            "HOT_NEWS_DEPENDENCIES_FACTORY 未配置，热点 Worker 拒绝启动。"
            "生产环境指向企业 Adapter 工厂；本地联调可指向 "
            "examples.hot_news_scenario_factory:build_from_scenario。"
        )

    factory = resolve_dependency_factory(target)
    try:
        bundle = factory(settings)
    except TypeError as exc:
        raise HotNewsDependencyConfigError(
            f"{target} 调用失败：工厂签名必须接受一个位置参数 Settings（{exc}）"
        ) from exc

    missing = [name for name in REQUIRED_ATTRIBUTES if not hasattr(bundle, name)]
    if missing:
        raise HotNewsDependencyConfigError(
            f"{target} 返回的对象缺少必需属性：{', '.join(missing)}"
        )
    return bundle