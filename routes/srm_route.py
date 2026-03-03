from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, Dict, Any, List

from simian_srm.srm_state import STATE
from simian_srm.geometry_nd.objects import tesseract_vertices_edges
from simian_srm.geometry_nd.rotations import rotate4d_xy_zw
from simian_srm.geometry_nd.project import project_4d_to_3d
from simian_srm.ai_predictor.predictor import AnglePredictor
from simian_srm.rag.retrieve import RagStore

router = APIRouter(prefix='/srm', tags=['srm'])

PRED = AnglePredictor()
RAG = RagStore()

class StepIn(BaseModel):
    steps: int = 1
    use_rag: bool = False
    query: Optional[str] = None

@router.get('/state')
def state():
    return {'theta': STATE.theta, 'phi': STATE.phi, 'uncertainty': STATE.uncertainty}

@router.post('/step')
def step(inp: StepIn):
    priors: Dict[str, Any] = {}
    citations: List[Dict[str, Any]] = []
    if inp.use_rag and inp.query:
        res = RAG.query(inp.query, k=5)
        priors['bias'] = res.get('keywords')
        citations = res.get('hits', [])

    dth, dph, sig = PRED.next_delta(priors=priors)
    STATE.theta += float(dth) * inp.steps
    STATE.phi   += float(dph) * inp.steps
    STATE.uncertainty = float(sig)

    V4, E = tesseract_vertices_edges()
    V3 = project_4d_to_3d(rotate4d_xy_zw(V4, STATE.theta, STATE.phi), d_w=3.0)
    return {
        'theta': STATE.theta, 'phi': STATE.phi, 'uncertainty': STATE.uncertainty,
        'vertices': V3.tolist(), 'edges': E, 'citations': citations
    }

@router.post('/rag/ingest')
def rag_ingest():
    cnt = RAG.ingest_folder('simian_srm/rag/store')
    return {'indexed_files': cnt, 'vocab': RAG.vocab_size()}

class RagQuery(BaseModel):
    query: str
    k: int = 5

@router.post('/rag/query')
def rag_query(q: RagQuery):
    return RAG.query(q.query, k=q.k)
