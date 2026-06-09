import ollama

EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768

_client = ollama.AsyncClient()


async def embed(text: str) -> list[float]:
    resp = await _client.embed(model=EMBED_MODEL, input=text)
    vec = list(resp.embeddings[0])
    if len(vec) != EMBED_DIM:
        raise ValueError(f"expected {EMBED_DIM} dims, got {len(vec)}")
    return vec
