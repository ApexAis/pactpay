import os
from functools import lru_cache
from pydantic import BaseModel, Field


class Settings(BaseModel):
    """Application settings loaded exclusively from environment variables."""

    app_name: str = Field(default="PactPay")
    app_version: str = Field(default="1.0.0")

    # PayPal Sandbox Configuration (never uses live endpoints)
    paypal_client_id: str = Field(default="")
    paypal_client_secret: str = Field(default="")
    paypal_mode: str = Field(default="sandbox")
    paypal_base_url: str = Field(default="https://api-m.sandbox.paypal.com")
    paypal_webhook_id: str = Field(default="")

    # Persistence and demo identities
    db_path: str = Field(default="pactpay.db")
    demo_mode: bool = Field(default=True)
    public_url: str = Field(default="http://localhost:8000")

    # AI Provider Configuration (Gemini / OpenAI-compatible / deterministic fallback)
    gemini_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    openai_base_url: str = Field(default="")
    ai_model: str = Field(default="")

    @property
    def has_paypal_credentials(self) -> bool:
        """Return True if live PayPal Sandbox credentials are configured."""
        cid = self.paypal_client_id.strip()
        sec = self.paypal_client_secret.strip()
        placeholders = {
            "",
            "your_sandbox_client_id_here",
            "your_sandbox_client_secret_here",
            "sb",
            "test",
        }
        return cid not in placeholders and sec not in placeholders

    @property
    def active_ai_provider(self) -> str:
        """Identify which AI provider is active based on environment variables."""
        if self.gemini_api_key.strip():
            return "gemini"
        if self.openai_api_key.strip():
            return "openai_compatible"
        return "deterministic_sandbox_engine"

    @property
    def resolved_ai_model(self) -> str:
        """Return the configured model name or a sensible default per provider."""
        if self.ai_model.strip():
            return self.ai_model.strip()
        if self.active_ai_provider == "gemini":
            return "gemini-2.5-flash"
        if self.active_ai_provider == "openai_compatible":
            if "groq.com" in self.openai_base_url:
                return "llama-3.3-70b-versatile"
            return "gpt-4o-mini"
        return "pactpay-ast-rubric-v1"

    @property
    def paypal_sdk_client_id(self) -> str:
        """Return the client ID for the frontend PayPal JS SDK (defaults to 'sb' in sandbox)."""
        if self.has_paypal_credentials:
            return self.paypal_client_id.strip()
        return "sb"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings from environment variables, enforcing PayPal Sandbox URL safety."""
    raw_mode = os.getenv("PAYPAL_MODE", "sandbox").strip().lower()
    if raw_mode != "sandbox":
        raw_mode = "sandbox"

    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_mode=raw_mode,
        paypal_base_url="https://api-m.sandbox.paypal.com",
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
        db_path=os.getenv("PACTPAY_DB_PATH", "pactpay.db").strip() or "pactpay.db",
        demo_mode=os.getenv("PACTPAY_DEMO", "1").strip().lower() not in ("0", "false", "no", "off"),
        public_url=(os.getenv("PACTPAY_PUBLIC_URL", "http://localhost:8000").strip() or "http://localhost:8000").rstrip("/"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
        ai_model=os.getenv("AI_MODEL", "").strip(),
    )
