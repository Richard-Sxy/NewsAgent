"""保存查询快照和结果。PostgresQuerySnapshotStore 保存 SQL、参数、查询身份等信息，后续通过 query_id 找回并校验查询。"""

import json
from typing import Protocol

from sqlalchemy import text

from app.db.session import Database


class QuerySnapshotStore(Protocol):
    async def save(self, snapshot: dict) -> None: ...
    async def get(self, query_id: str, tenant_id: str, user_id: str) -> dict | None: ...
    async def complete(self, query_id: str, tenant_id: str, user_id: str, result: dict) -> None: ...

"""初始化查询审计，执行审计表查询：用户信息、查询快照、查询结果、创建时间、过期时间、完成时间等信息"""
async def initialize_query_audit(database: Database, *, environment: str) -> None:
    if environment != "e2e":
        raise ValueError("SQL demo audit initialization is only allowed in e2e")
    async with database.session() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS public.sql_assistant_query_audit (
                query_id uuid PRIMARY KEY,
                tenant_id uuid NOT NULL,
                user_id uuid NOT NULL,
                snapshot jsonb NOT NULL,
                result jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz NOT NULL,
                completed_at timestamptz
            )
        """))

"""PostgreSQL查询快照存储"""
class PostgresQuerySnapshotStore:
    def __init__(self, database: Database) -> None:
        self.database = database
    
    # 保存租户信息
    async def save(self, snapshot: dict) -> None:
        async with self.database.session() as session:
            await session.execute(text("""
                INSERT INTO public.sql_assistant_query_audit
                (query_id, tenant_id, user_id, snapshot, expires_at)
                VALUES (CAST(:query_id AS uuid), CAST(:tenant_id AS uuid),
                    CAST(:user_id AS uuid), CAST(:snapshot AS jsonb), CAST(:expires_at AS timestamptz))
            """), {
                "query_id": snapshot["preview"]["query_id"],
                "tenant_id": snapshot["tenant_id"], "user_id": snapshot["user_id"],
                "snapshot": json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                "expires_at": snapshot["preview"]["expires_at"],
            })

    async def get(self, query_id: str, tenant_id: str, user_id: str) -> dict | None:
        async with self.database.session() as session:
            result = await session.execute(text("""
                SELECT snapshot, result FROM public.sql_assistant_query_audit
                WHERE query_id = CAST(:query_id AS uuid)
                  AND tenant_id = CAST(:tenant_id AS uuid) AND user_id = CAST(:user_id AS uuid)
            """), {"query_id": query_id, "tenant_id": tenant_id, "user_id": user_id})
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return {**row["snapshot"], "result": row["result"]}

    async def complete(self, query_id: str, tenant_id: str, user_id: str, result: dict) -> None:
        async with self.database.session() as session:
            await session.execute(text("""
                UPDATE public.sql_assistant_query_audit
                SET result = COALESCE(result, CAST(:result AS jsonb)), completed_at = COALESCE(completed_at, now())
                WHERE query_id = CAST(:query_id AS uuid)
                  AND tenant_id = CAST(:tenant_id AS uuid) AND user_id = CAST(:user_id AS uuid)
            """), {"query_id": query_id, "tenant_id": tenant_id, "user_id": user_id,
                      "result": json.dumps(result, ensure_ascii=False, allow_nan=False)})
