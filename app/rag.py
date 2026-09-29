"""One retrieval pipeline; demo uses deterministic hashed bigrams, live uses embeddings."""
import hashlib
import math
import re
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL
from qdrant_client import QdrantClient, models
from app.config import ROOT


def tokens(text):
    parts = re.findall(r'\w+', text.lower())
    return [part[i:i+2] for part in parts for i in range(max(0, len(part)-1))]


def query_tokens(query):
    # Remove conversational scaffolding, not domain terms such as 特休 or 餐費.
    cleaned = re.sub(r'請問|公司的?|規定|規則|政策|是什麼|怎麼|如何|可以|多少|一天|有哪些|需要|我還剩|我今年|另外|幫我|一下', ' ', query)
    return set(tokens(cleaned)) or set(tokens(query))


def demo_vector(text):
    vector = [0.0] * 512
    for token in tokens(text):
        digest = hashlib.sha256(token.encode()).digest()
        vector[int.from_bytes(digest[:4], 'big') % 512] += 1
    norm = math.sqrt(sum(x*x for x in vector)) or 1
    return [x/norm for x in vector]


def read_chunks(path):
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        pages = [(i+1, page.extract_text() or '') for i, page in enumerate(PdfReader(path).pages)]
    else:
        pages = [(None, path.read_text(encoding='utf-8'))]
    chunks = []
    for page, text in pages:
        title = text.splitlines()[0].lstrip('# ').strip() if text.strip() else path.stem
        for i in range(0, len(text), 550):
            content = text[i:i+650].strip()
            if content:
                chunks.append({'source': path.name, 'title': title, 'page': page, 'chunk': i // 550 + 1, 'text': content})
    return chunks


class Knowledge:
    def __init__(self, settings):
        self.settings = settings
        if settings.embedding_mode == 'openai' and not settings.openai_api_key:
            raise ValueError('EMBEDDING_MODE=openai 需要 OPENAI_API_KEY。')
        self.client = QdrantClient(url=settings.qdrant_url, timeout=20) if settings.qdrant_url else QdrantClient(path=settings.qdrant_path)
        tag = hashlib.sha256(settings.embedding_model.encode()).hexdigest()[:8]
        self.collection = f'company_{"openai_" + tag if settings.embedding_mode == "openai" else "demo_v2"}'
        self.api = None
        if settings.embedding_mode == 'openai':
            if not settings.openai_api_key:
                raise ValueError('EMBEDDING_MODE=openai 需要 OPENAI_API_KEY。')
            from openai import OpenAI
            self.api = OpenAI(api_key=settings.openai_api_key, timeout=30, max_retries=2)

    def embed(self, texts):
        if self.api:
            return [item.embedding for item in self.api.embeddings.create(model=self.settings.embedding_model, input=texts).data]
        return [demo_vector(t) for t in texts]

    def ingest(self, directory):
        chunks = [c for p in sorted(Path(directory).iterdir()) if p.suffix.lower() in ('.md', '.txt', '.pdf') for c in read_chunks(p)]
        if not chunks:
            raise ValueError('沒有可索引的文字。掃描 PDF 需要先做 OCR。')
        fingerprint = hashlib.sha256(str(chunks).encode()).hexdigest()
        if self.client.collection_exists(self.collection):
            found, _ = self.client.scroll(self.collection, scroll_filter=models.Filter(must=[models.FieldCondition(key='fingerprint', match=models.MatchValue(value=fingerprint))]), limit=1)
            if found:
                return len(chunks)
        vectors = []
        for offset in range(0, len(chunks), 64):
            vectors.extend(self.embed([c['text'] for c in chunks[offset:offset+64]]))
        # Stage a complete new collection, then switch the alias atomically.
        physical = self.collection + '_' + fingerprint[:16]
        if not self.client.collection_exists(physical):
            self.client.create_collection(physical, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
        points = [models.PointStruct(id=str(uuid5(NAMESPACE_URL, f'{c["source"]}:{c["page"]}:{c["chunk"]}')), vector=v, payload={**c, 'fingerprint': fingerprint}) for c, v in zip(chunks, vectors)]
        self.client.upsert(physical, points=points, wait=True)
        aliases = {a.alias_name for a in self.client.get_aliases().aliases}
        changes = []
        if self.collection in aliases:
            changes.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=self.collection)))
        changes.append(models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name=physical, alias_name=self.collection)))
        self.client.update_collection_aliases(changes)
        return len(chunks)

    def search(self, query):
        results = self.client.query_points(collection_name=self.collection, query=self.embed([query])[0], limit=3 if self.api else 24, score_threshold=0.30 if self.api else None).points
        if self.api:
            return [{**r.payload, 'score': round(r.score, 3)} for r in results]
        terms = query_tokens(query)
        ranked = [(len(terms & set(tokens(r.payload['text']))), r) for r in results]
        ranked.sort(key=lambda item: (item[0], item[1].score), reverse=True)
        best = ranked[0][0] if ranked else 0
        # Exact lexical evidence removes false matches caused by hash collisions.
        return [{**r.payload, 'score': round(r.score, 3), 'matched_terms': count} for count, r in ranked if count and count >= max(1, best * .6) and count / max(1, len(terms)) >= .15][:3]

    def documents(self):
        points, offset = self.client.scroll(self.collection, limit=256, with_vectors=False)
        while offset is not None:
            batch, offset = self.client.scroll(self.collection, limit=256, offset=offset, with_vectors=False)
            points.extend(batch)
        docs = {}
        for p in points:
            source = p.payload['source']
            docs.setdefault(source, {'source': source, 'title': p.payload['title'], 'fingerprint': p.payload.get('fingerprint', ''), 'chunks': []})['chunks'].append({k:v for k,v in p.payload.items() if k != 'fingerprint'})
        for doc in docs.values():
            doc['chunks'].sort(key=lambda c: (c['page'] or 0, c['chunk']))
        return sorted(docs.values(), key=lambda x:x['source'])

    def close(self):
        self.client.close()
        if self.api:
            self.api.close()
