from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
import ipaddress
import os
from dotenv import load_dotenv

class Settings(BaseSettings):
    chatgpt_base_url: str = "https://chatgpt.com"
    openai_base_url: str = "https://api.openai.com"
    host: str = "127.0.0.1"
    port: int = 8000
    bridge_api_key: str = Field(default="", repr=False)
    allow_unauthenticated_public: bool = False
    max_request_bytes: int = Field(default=16 * 1024 * 1024, gt=0)
    max_concurrent_requests: int = Field(default=32, gt=0)
    request_body_timeout_seconds: float = Field(default=30.0, gt=0)
    deployment_commit: str = ""

    def validate_network_binding(self) -> None:
        host = self.host.strip().strip("[]").lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback and not self.bridge_api_key and not self.allow_unauthenticated_public:
            raise ValueError(
                "Public binding requires BRIDGE_API_KEY or ALLOW_UNAUTHENTICATED_PUBLIC=true"
            )
    
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
