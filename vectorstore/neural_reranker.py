"""Optional PhoBERT-based neural reranker (flag-gated, blended with heuristic).

PhoBERT fine-tuned for sentence similarity (default ``keepitreal/vietnamese-sbert``)
scores query/candidate semantic closeness. It is *blended* with the tuned
heuristic reranker rather than replacing it, because on Vietnamese financial
line-items the neural scores discriminate weakly. Enabled only when
``NEURAL_RERANK=1``; degrades to a no-op if torch/transformers/underthesea or the
model are unavailable, so the default pipeline is never affected.
"""
# Code note: lazy singleton; CPU inference; never raises into the retrieval path.

import os
import threading

_MODEL_NAME = os.getenv("NEURAL_RERANK_MODEL", "keepitreal/vietnamese-sbert")
_MAX_TOKENS = int(os.getenv("NEURAL_RERANK_MAX_TOKENS", "128"))

_lock = threading.Lock()
_state = {"loaded": False, "ok": False}


def neural_rerank_enabled() -> bool:
    return os.getenv("NEURAL_RERANK", "0").strip().lower() in {"1", "true", "yes", "on"}


def _ensure_loaded() -> bool:
    if _state["loaded"]:
        return _state["ok"]
    with _lock:
        if _state["loaded"]:
            return _state["ok"]
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
            from underthesea import word_tokenize

            tok = AutoTokenizer.from_pretrained(_MODEL_NAME)
            model = AutoModel.from_pretrained(_MODEL_NAME)
            model.eval()
            _state.update(torch=torch, tok=tok, model=model, seg=word_tokenize, ok=True)
        except Exception:
            _state["ok"] = False
        _state["loaded"] = True
    return _state["ok"]


def neural_similarity(query: str, docs: list[str], batch_size: int = 16) -> list[float]:
    """Cosine similarity (0..1) of each doc to the query.

    Returns [] when the reranker is unavailable, so callers can no-op cleanly.
    """
    if not docs or not _ensure_loaded():
        return []
    torch = _state["torch"]
    tok = _state["tok"]
    model = _state["model"]
    seg = _state["seg"]
    import torch.nn.functional as F

    def embed(texts: list[str]):
        segmented = [seg(str(t or ""), format="text") for t in texts]
        enc = tok(segmented, padding=True, truncation=True, max_length=_MAX_TOKENS, return_tensors="pt")
        with torch.no_grad():
            out = model(**enc)
        mask = enc["attention_mask"].unsqueeze(-1).float()
        pooled = (out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        return F.normalize(pooled, p=2, dim=1)

    try:
        qv = embed([query])
        sims: list[float] = []
        for start in range(0, len(docs), batch_size):
            dv = embed(docs[start:start + batch_size])
            sims.extend((qv @ dv.T).squeeze(0).tolist())
        return sims
    except Exception:
        return []
