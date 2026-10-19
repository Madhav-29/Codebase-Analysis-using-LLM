from codebase_analyzer.config import Settings
from codebase_analyzer.llm.providers import make_provider
from codebase_analyzer.llm.providers.google_genai import applied_temperature


def test_gemini_chat_model_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-not-a-real-key")  # constructing the client makes no request
    p = make_provider(Settings(llm_provider="google_genai", llm_model="gemini-3.8-flash"))
    llm = p.chat_model
    assert type(llm).__name__ == "ChatGoogleGenerativeAI"
    assert llm.model == "gemini-3.8-flash" and llm.temperature == 0.0 and p.temperature == 0.0
    assert llm.max_output_tokens == Settings().budget.output_reserve and llm.max_retries == 1
    assert p.concurrency == 1 and p.structured_output == {"method": "json_schema"}


def test_temperature_is_not_sent_to_fixed_sampling_models(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-not-a-real-key")
    lite = Settings(llm_provider="google_genai", llm_model="gemini-3.5-flash-lite")
    p = make_provider(lite)
    assert applied_temperature(lite) is None and p.temperature is None and p.chat_model.temperature is None
    assert "fixed sampling" in p.temperature_note
    assert applied_temperature(Settings(llm_provider="google_genai", llm_model="gemini-3.8-flash")) == 0.0
