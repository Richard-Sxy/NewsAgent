import asyncio

from redis.asyncio import Redis

from app.config import get_settings
from app.db.session import Database
from app.services.event_publisher import RedisProgressPublisher
from app.services.outbox import OutboxRelay


async def run() -> None:
    settings = get_settings()
    database = Database(settings)
    redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    relay = OutboxRelay(
        database,
        RedisProgressPublisher(redis, settings),
        settings,
    )
    try:
        await relay.run_forever()
    finally:
        await redis.aclose()
        await database.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
