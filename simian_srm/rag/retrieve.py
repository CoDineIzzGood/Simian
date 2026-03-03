import os
from typing import List, Dict, Any
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

class RagStore:
    def __init__(self):
        self.docs: List[str] = []
        self.meta: List[Dict[str,Any]] = []
        self.vec = None
        self.model = None

    def ingest_folder(self, folder: str) -> int:
        items = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(('.txt','.md')):
                    p = os.path.join(root,f)
                    with open(p, 'r', encoding='utf-8', errors='ignore') as fh:
                        items.append((p, fh.read()))
        self.docs = [text for _, text in items]
        self.meta = [{'path': p, 'len': len(t)} for p,t in items]
        if not self.docs:
            return 0
        self.vec = TfidfVectorizer(stop_words='english')
        self.model = self.vec.fit_transform(self.docs)
        return len(self.docs)

    def vocab_size(self) -> int:
        return 0 if not self.vec else len(self.vec.vocabulary_)

    def query(self, q: str, k: int=5) -> Dict[str, Any]:
        if not self.docs:
            self.ingest_folder(os.path.join(os.path.dirname(__file__), 'store'))
        if not self.docs:
            return {'hits': [], 'keywords': []}
        qv = self.vec.transform([q])
        sims = cosine_similarity(qv, self.model)[0]
        idxs = sims.argsort()[::-1][:k]
        hits = [{'score': float(sims[i]), 'doc': self.meta[i]['path'], 'excerpt': self.docs[i][:240]} for i in idxs]
        inv_vocab = {v:k for k,v in self.vec.vocabulary_.items()}
        nz = qv.nonzero()[1]
        key = [inv_vocab[i] for i in nz][:6]
        return {'hits': hits, 'keywords': key}
