from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    agent_mode: Literal['demo', 'openai'] = 'demo'
    openai_api_key: str = ''
    openai_model: str = 'gpt-4.1-mini'
    embedding_model: str = 'text-embedding-3-small'
    database_url: str = 'sqlite:///./data/admin.db'
    qdrant_url: str = ''
    qdrant_path: str = './data/qdrant'
    knowledge_dir: str = str(ROOT / 'knowledge')
    environment: Literal['local', 'production'] = 'local'
    public_origin: str = 'http://127.0.0.1:8000'
    encryption_key: str = ''
    secret_dir: str = str(ROOT / 'data/secrets')
    setup_token: str = ''
    seed_demo: bool = True
    allow_demo: bool = False
    demo_admin: bool = True
    embedding_mode: Literal['local', 'openai'] = 'local'
    session_hours: int = Field(default=8, ge=1, le=24)

    def validate_runtime(self):
        from cryptography.fernet import Fernet
        from urllib.parse import urlparse
        origin = urlparse(self.public_origin)
        if origin.scheme not in ('http', 'https') or not origin.netloc or origin.path not in ('', '/'):
            raise ValueError('PUBLIC_ORIGIN 必須是完整網站來源，不含路徑。')
        if self.environment == 'production':
            if origin.scheme != 'https' or not self.encryption_key:
                raise ValueError('正式環境需要 HTTPS PUBLIC_ORIGIN 與 ENCRYPTION_KEY。')
            if self.seed_demo and not self.allow_demo:
                raise ValueError('正式環境需要 SEED_DEMO=false；展示站必須明確設定 ALLOW_DEMO=true。')
            if not self.database_url.startswith('postgresql') or not self.qdrant_url:
                raise ValueError('正式環境需要 PostgreSQL 與 Qdrant server。')
            if not self.allow_demo and Path(self.knowledge_dir).resolve() == (ROOT / 'knowledge').resolve():
                raise ValueError('正式環境必須使用獨立 KNOWLEDGE_DIR，不能使用內建虛構政策。')
            if self.agent_mode != 'demo':
                raise ValueError('正式環境請使用管理員 AI 設定，移除舊版 AGENT_MODE=openai。')
        if self.encryption_key:
            Fernet(self.encryption_key.encode())
        if self.setup_token and len(self.setup_token) < 32:
            raise ValueError('SETUP_TOKEN 至少需 32 字元。')

    @property
    def demo_available(self) -> bool:
        return self.environment == 'local' or self.allow_demo


settings = Settings()
