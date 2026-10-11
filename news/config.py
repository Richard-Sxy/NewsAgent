from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    crawler_timeout: float = 15.0
    crawler_user_agent: str = "Mozilla/5.0"
    newsagent_base_url: str = "http://127.0.0.1:28000"
    newsagent_knowledge_token: str = ""
    newsagent_tenant_id: str = ""
    newsagent_timeout: float = 120.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

    ingest_db_path: str = "data/news_ingest.db"
    article_cache_dir: str = "data/articles"

    # 标题分词与实体抽取。先执行 scripts/download_title_nlp_model.py，
    # 再显式启用；生产运行默认禁止推理时临时联网下载模型。
    title_nlp_enabled: bool = False
    title_nlp_model: str = "LTP/tiny"
    title_nlp_cache_dir: str = "data/models/huggingface"
    title_nlp_local_files_only: bool = True
    title_nlp_lexicon_path: str = (
        "intelligence/extraction/title_entity_lexicon.json"
    )
    title_nlp_required: bool = False

    # 稳定性配置
    crawler_max_retries: int = 3
    crawler_retry_base_delay: float = 1.0
    crawler_request_interval: float = 0.5
    ingest_max_retry_count: int = 3
    qa_max_retry_count: int = 3

    # 每日自动抓取脚本
    daily_limit_per_topic: int = 20
    daily_max_pages: int = 3
    daily_timezone: str = "Asia/Shanghai"
    daily_qa_limit: int = 20
    daily_report_dir: str = "reports"
    daily_evaluation_enabled: bool = False
    daily_evaluation_limit: int = 20
    evaluation_questions_path: str = "tests/evaluation_questions_v2.json"

settings = Settings()
