import os
from redis import asyncio as aioredis
from openai import AsyncOpenAI
import httpx
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
http_client = httpx.AsyncClient(timeout=10,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
    headers={"User-Agent": "JiraMate/1.0"},
    http2=True
)
gptclient = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
