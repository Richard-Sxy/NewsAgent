from app.api.jobs import router as jobs_router
from app.api.events import router as events_router
from app.api.data_loop import router as data_loop_router
from app.api.hot_news import router as hot_news_router
from app.api.memory import router as memory_router

__all__ = [
    "jobs_router",
    "events_router",
    "data_loop_router",
    "hot_news_router",
    "memory_router",
]
