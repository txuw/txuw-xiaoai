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
    tts_sample_rate: int = 22050
    tts_channels: int = 1
    tts_bits_per_sample: int = 16
    tts_period_size: int = 330
    tts_buffer_size: int = 1320
    legacy_interrupt_enabled: bool = True
    legacy_interrupt_command: str = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1"


settings = Settings()
