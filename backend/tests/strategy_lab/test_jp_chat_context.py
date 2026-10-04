"""JP IDE top-level market reaches the existing skill dispatch context."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_top_level_jp_market_reaches_skill_context(monkeypatch):
    from backend.services.engine.routers.ai_ide import chat
    from backend.services.engine.alpha_agent import llm_client

    observed = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def chat_stream(self, prompt, context):
            observed.append(context)
            yield "local response"

    monkeypatch.setattr(chat, "QuantAgent", Agent)
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_config",
        lambda: SimpleNamespace(
            api_key="test-local-key",
            base_url="http://invalid.local",
            model="local",
            headers={},
        ),
    )
    app = FastAPI()
    app.include_router(chat.router)
    response = TestClient(app).post(
        "/chat",
        json={"message": "generate", "market": "JP", "extra_context": {"market": "CN"}},
    )
    assert response.status_code == 200
    assert observed[0]["market"] == "JP"
    assert observed[0]["market_context"] == chat.STRATEGY_MARKET_CONTEXT["JP"]


def test_original_market_context_resolution_remains_unchanged(monkeypatch):
    from backend.services.engine.routers.ai_ide import chat
    from backend.services.engine.alpha_agent import llm_client

    observed = []

    class Agent:
        def __init__(self, *args, **kwargs):
            pass

        async def chat_stream(self, prompt, context):
            observed.append(context)
            yield "local response"

    monkeypatch.setattr(chat, "QuantAgent", Agent)
    monkeypatch.setattr(
        llm_client,
        "resolve_llm_config",
        lambda: SimpleNamespace(
            api_key="test-local-key",
            base_url="http://invalid.local",
            model="local",
            headers={},
        ),
    )
    app = FastAPI()
    app.include_router(chat.router)
    response = TestClient(app).post(
        "/chat",
        json={"message": "generate", "market": "CN", "extra_context": {"market": "US"}},
    )
    assert response.status_code == 200
    assert observed[0]["market"] == "US"
