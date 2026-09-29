import argparse
from app.config import settings, ROOT
from app.rag import Knowledge

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='索引整個文件目錄（MD / TXT / 可選取文字的 PDF）')
    parser.add_argument('directory', nargs='?', default=settings.knowledge_dir)
    args = parser.parse_args()
    kb = Knowledge(settings)
    try:
        print(f'已索引 {kb.ingest(args.directory)} 個 chunks。')
    finally:
        kb.close()
