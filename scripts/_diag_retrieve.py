import sys, time
sys.path.insert(0, "backend")
from app.config import settings
from app.knowledge.chroma import _available, _ef, _client
from app.intent.vector_store import retrieve_intents

print("available:", _available(), "qwen_key set:", bool(settings.qwen_embedding_key))
t0 = time.time()
try:
    col = _client().get_or_create_collection(name=settings.chroma_collection_intents, embedding_function=_ef())
    print("[diag] collection ok, querying...", flush=True)
    res = col.query(query_texts=["你好"], n_results=10)
    print("[diag] raw col.query took %.2fs" % (time.time() - t0), flush=True)
    print("[diag] ids:", (res.get("ids") or [[]])[0][:10], flush=True)
except Exception as e:
    print("[diag] col.query EXC after %.2fs: %r" % (time.time() - t0, e), flush=True)

t1 = time.time()
try:
    cands = retrieve_intents("你好", top_k=5)
    print("[diag] retrieve_intents took %.2fs -> %d cands" % (time.time() - t1, len(cands)), flush=True)
    for c in cands[:5]:
        print("   ", c["intent_id"], round(c["score"], 3), flush=True)
except Exception as e:
    print("[diag] retrieve_intents EXC after %.2fs: %r" % (time.time() - t1, e), flush=True)
