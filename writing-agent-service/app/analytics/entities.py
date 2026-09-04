"""企业行为查询结果对应的 Python 领域对象。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EventType(StrEnum):
    """第一版支持的用户行为；字段值应与企业数据适配层完成映射。"""

    IMPRESSION = "impression"      # 曝光
    CLICK = "click"                # 点击新闻
    READ = "read"                  # 阅读新闻
    PLAY = "play"                  # 播放视频
    LIKE = "like"                  # 点赞
    COMMENT = "comment"            # 评论
    SHARE = "share"                # 分享
    FAVORITE = "favorite"          # 收藏


class ContentType(StrEnum):
    """第一版支持的内容类型：文章/视频"""
    ARTICLE = "article"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class BehaviorRecord:
    """一条完成字段映射后的企业用户行为记录。

    注意：这里的对象不是数据库 ORM 模型，也不会将企业原始行为明细落到本地库。
    `event_id` 是否必填以及是否需要本地去重，应以企业数据源契约为准。
    """

    event_id: str
    user_id: str
    news_id: str
    event_type: EventType
    event_time: datetime
    content_type: ContentType
    duration_seconds: int = 0
    channel: str | None = None
    device_type: str | None = None

    def validate(self) -> None:
        """校验分析层真正依赖的业务约束。

        数据库字符串到 ``datetime`` 的转换属于数据源适配器职责；进入领域层后，
        时间必须已经是带时区的 ``datetime``。只有 READ/PLAY 可以携带消费时长。
        """

        event_id = self.event_id.strip()
        user_id = self.user_id.strip()
        news_id = self.news_id.strip()

        if not event_id:
            raise ValueError("event_id cannot be empty")
        if not user_id:
            raise ValueError("user_id cannot be empty")
        if not news_id:
            raise ValueError("news_id cannot be empty")

        if not isinstance(self.event_time, datetime):
            raise ValueError("event_time must be a datetime")
        if (
            self.event_time.tzinfo is None
            or self.event_time.utcoffset() is None
        ):
            raise ValueError(f"event_time must have timezone info: {self.event_time}")

        if self.duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")

        if (
            self.event_type not in {
                EventType.READ,
                EventType.PLAY,
            }
            and self.duration_seconds != 0
        ):
            raise ValueError("duration_seconds must be 0 for non-READ/PLAY events")
