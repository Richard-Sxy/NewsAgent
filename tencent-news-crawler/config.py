from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    crawler_timeout: float = 15.0
    crawler_user_agent: str = "Mozilla/5.0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

    fastgpt_base_url: str = "http://127.0.0.1:3000"
    fastgpt_api_key: str = ""
    fastgpt_dataset_id: str = ""
    fastgpt_timeout: float = 60.0
    ingest_db_path: str = "data/news_ingest.db"
    article_cache_dir: str = "data/articles"

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

    # 新闻知识库QA
    fastgpt_qa_dataset_id: str = ""

    # FastGPT 新闻问答应用（与知识库导入 API Key 分开）
    fastgpt_app_api_key: str = ""
    fastgpt_app_id: str = ""

settings = Settings()
