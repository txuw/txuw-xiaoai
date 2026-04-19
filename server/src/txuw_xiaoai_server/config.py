from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    dashscope_api_key: str = ""
    dashscope_tts_model: str = "cosyvoice-v3-flash"
    dashscope_tts_voice: str = "longyan_v3"
    tts_intercept_enabled: bool = False
    llm_proxy_enabled: bool = False
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_system_prompt: str = (
        "你是一个适合直接口播的中文语音助手。"
        "请用自然、简短、口语化的中文回答。"
        "只输出纯文本，不要使用 Markdown、列表编号、代码块或表情。"
    )
    memory_enabled: bool = False
    memory_user_id: str = "txuw"
    memory_llm_model: str = "gpt-5-nano-2025-08-07"
    memory_embedding_model: str = "text-embedding-3-small"
    memory_milvus_url: str = ""
    memory_milvus_token: str = ""
    memory_milvus_db_name: str = "default"
    memory_milvus_collection_name: str = "txuw_xiaoai_mem0"
    memory_recall_max_results: int = 5
    memory_recall_min_score: float = 0.3
    memory_commit_queue_maxsize: int = 256
    memory_commit_worker_count: int = 2
    tts_sample_rate: int = 22050
    tts_channels: int = 1
    tts_bits_per_sample: int = 16
    tts_period_size: int = 330
    tts_buffer_size: int = 1320
    legacy_interrupt_enabled: bool = True
    legacy_interrupt_command: str = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1"


settings = Settings()
