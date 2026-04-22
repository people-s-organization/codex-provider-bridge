from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os
from dotenv import load_dotenv

class Settings(BaseSettings):
    chatgpt_base_url: str = "https://chatgpt.com"
    host: str = "0.0.0.0"
    port: int = 8000
    
    @property
    def chatgpt_access_token(self) -> str:
        load_dotenv(override=True)
        return os.getenv("CHATGPT_ACCESS_TOKEN", "")
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
