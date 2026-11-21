from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- embeddings (Gemini) ---
    gemini_api_key: str = ""
    gemini_embed_model: str = "gemini-embedding-001"  # verify against current Gemini docs
    embed_dim: int = 768                              # must match vector(768) in sql/schema.sql
    database_url: str = ""                            # Supabase Postgres connection string
    embed_batch_size: int = 25                        # chunks per Gemini embedding call (smaller = gentler on free-tier quota)
    embed_batch_delay_s: float = 1.0                  # pause between embedding calls
    embed_batch_tokens: int = 8000                    # est. tokens per embedding call (one call above the TPM limit can NEVER succeed)
    embed_tpm_budget: int = 20000                     # pace calls so est. tokens/minute stays under this (free tier limit is 30K)

    # --- answering LLM (any OpenAI-compatible API; Groq by default) ---
    llm_provider: str = "openai_compat"               # "openai_compat" | "gemini"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"            # verify against the provider's current model list
    llm_json_mode: bool = True                        # response_format=json_object (set false if a model rejects it)
    llm_max_output_tokens: int = 1500
    gemini_chat_model: str = "gemini-2.5-flash"       # only used when llm_provider="gemini"

    # --- optional fallback provider (used on rate limits / 5xx), e.g. OpenRouter ---
    llm_fallback_base_url: str = "https://openrouter.ai/api/v1"
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""                      # e.g. a ":free" model id from openrouter.ai/models

    # --- call-saving ---
    llm_cache: str = "readwrite"                      # off | readwrite | replay (replay never calls the API)
    cache_dir: str = "./data/cache"
    default_ask_mode: str = "fast"                    # fast = retrieve once + 1 LLM call; agent = full loop
    fast_top_k: int = 5
    fast_max_steps: int = 3

    # --- auth: Supabase Auth ("Sign in with GitHub") ---
    auth_required: bool = True                        # set false ONLY for local development
    supabase_url: str = ""                            # https://<project>.supabase.co
    supabase_anon_key: str = ""                       # publishable/anon key (public by design, never the secret key)
    auth_cache_ttl_s: int = 60
    max_new_repos_per_user_per_day: int = 5

    # --- GitHub publishing (PRs / issues) ---
    github_api_url: str = "https://api.github.com"
    # Optional read-only token (fine-grained PAT, NO permissions) used ONLY for the public-repo metadata
    # check against api.github.com. Raises the REST limit from 60/h per shared IP to 5000/h. Never sent to git.
    github_api_token: str = ""

    # --- storage / ingestion limits ---
    repos_dir: str = "./data/repos"
    max_repo_mb: int = 50
    max_file_kb: int = 200
    max_chunks_per_repo: int = 4000

    # --- agent guardrail budgets ---
    max_agent_steps: int = 8
    max_tokens_per_query: int = 60000
    query_timeout_s: int = 90
    max_query_chars: int = 1000
    rate_limit_per_min: int = 20

    # --- retrieval ---
    retrieval_top_k: int = 5
    retrieval_pool: int = 30

    # --- cost tracking (USD per 1M tokens; set from current Gemini pricing) ---
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0

    cors_origins: str = "*"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
