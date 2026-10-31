from app.config import Settings
from app.ports.providers import LanguageModel


def build_language_model(settings: Settings) -> LanguageModel:
    fallbacks = [m.strip() for m in settings.llm_fallback_models.split(",") if m.strip()]
    if not fallbacks:
        return _build(settings, settings.llm_model)
    from app.adapters.llm.fallback import FallbackLanguageModel

    return FallbackLanguageModel([_build(settings, m) for m in [settings.llm_model, *fallbacks]])


def _build(settings: Settings, model: str | None) -> LanguageModel:
    match settings.llm_provider:
        case "groq":
            from app.adapters.llm.openai_compat import OpenAICompatibleLanguageModel

            return OpenAICompatibleLanguageModel(settings.groq_api_key.get_secret_value(), model=model)
        case "openai-compatible":
            from app.adapters.llm.openai_compat import OpenAICompatibleLanguageModel

            if not settings.llm_base_url or not model:
                raise ValueError("LLM_PROVIDER=openai-compatible requires LLM_BASE_URL and LLM_MODEL")
            return OpenAICompatibleLanguageModel(
                settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url,
                model=model,
                provider="openai-compatible",
            )
        case "anthropic":
            from app.adapters.llm.anthropic import AnthropicLanguageModel

            return AnthropicLanguageModel(settings.anthropic_api_key.get_secret_value(), model=model)
        case "fake":
            from app.adapters.llm.stub import StubLanguageModel

            return StubLanguageModel()
