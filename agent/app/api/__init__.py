from app.modules.writing.jobs import router as jobs_router
from app.modules.writing.events import router as events_router
from app.modules.data_loop.api import router as data_loop_router
from app.modules.hot_news.api import router as hot_news_router
from app.modules.memory.api import router as memory_router
from app.api.security_tests import router as security_tests_router
from app.api.knowledge import router as knowledge_router
from app.api.conversations import router as conversations_router

__all__ = [
    "jobs_router",
    "events_router",
    "data_loop_router",
    "hot_news_router",
    "memory_router",
    "security_tests_router",
    "knowledge_router",
    "conversations_router",
]
