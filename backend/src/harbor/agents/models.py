"""Chat-model factory: one agent, selectable provider (S4).

``HARBOR_MODEL_PROVIDER`` only chooses the client class. The endpoint and model
name still come from ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``, so switching
provider is three env vars: provider, base URL, model name.
"""

from agentscope.credential import DashScopeCredential, DeepSeekCredential
from agentscope.model import ChatModelBase, DashScopeChatModel, DeepSeekChatModel

from harbor.core.config import DASHSCOPE_BASE_URL, DEEPSEEK_BASE_URL, Settings

DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"


def build_chat_model(settings: Settings) -> ChatModelBase:
    """Build the chat model for the configured provider."""
    if settings.model_provider == "dashscope":
        if settings.openai_base_url.rstrip("/") == DEEPSEEK_BASE_URL:
            raise ValueError(
                "HARBOR_MODEL_PROVIDER=dashscope，但 OPENAI_BASE_URL 仍指向 DeepSeek；"
                f"请设为 {DASHSCOPE_BASE_URL}"
            )
        if settings.openai_model == DEEPSEEK_DEFAULT_MODEL:
            raise ValueError(
                "HARBOR_MODEL_PROVIDER=dashscope，但 OPENAI_MODEL 仍是 deepseek-chat；"
                "请设为 qwen-plus（或 qwen-max / qwen-turbo）"
            )
        return DashScopeChatModel(
            credential=DashScopeCredential(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            ),
            model=settings.openai_model,
        )

    return DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        ),
        model=settings.openai_model,
    )
