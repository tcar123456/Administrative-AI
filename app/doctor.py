"""Read-only local preflight. Never prints credentials or sends model requests."""
import argparse
import importlib.util
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='檢查套件、設定及選用的本機服務。')
    parser.add_argument('--url', help='例如 http://127.0.0.1:8000；讀取 /api/health')
    args = parser.parse_args()
    missing = [name for name in ['fastapi', 'uvicorn', 'sqlalchemy', 'qdrant_client', 'openai', 'pydantic_settings', 'pypdf', 'psycopg', 'cryptography', 'argon2'] if importlib.util.find_spec(name) is None]
    if missing:
        print('缺少套件：' + ', '.join(missing))
        return 1
    from app.config import settings
    settings.validate_runtime()
    checks = {'Python 3.12+': sys.version_info >= (3, 12), '套件已安裝': True,
              '文件目錄存在': Path(settings.knowledge_dir).is_dir(),
              '嵌入設定': settings.embedding_mode == 'local' or bool(settings.openai_api_key.strip())}
    if args.url:
        import httpx
        try:
            response = httpx.get(args.url.rstrip('/') + '/api/health', timeout=10)
            checks['API / Database / Qdrant 就緒'] = response.status_code == 200 and response.json().get('status') == 'ok'
        except (httpx.HTTPError, ValueError):
            checks['API / Database / Qdrant 就緒'] = False
    for name, good in checks.items():
        print(f'{"PASS" if good else "FAIL"}  {name}')
    print(f'Mode: {settings.agent_mode}; OpenAI requests were not sent.')
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
