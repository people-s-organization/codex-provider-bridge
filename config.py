from pydantic_settings import BaseSettings, SettingsConfigDict
import os
from dotenv import load_dotenv

class Settings(BaseSettings):
    chatgpt_base_url: str = "https://chatgpt.com"
    openai_base_url: str = "https://api.openai.com"
    host: str = "0.0.0.0"
    port: int = 8000
    
    @property
    def chatgpt_access_token(self) -> str:
        load_dotenv(override=True)
        return os.getenv("CHATGPT_ACCESS_TOKEN", "")

    @property
    def openai_api_key(self) -> str:
        load_dotenv(override=False)
        return os.getenv("OPENAI_API_KEY", "")
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
