from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"
    UPLOAD_DIR: str = "uploads"

    # Auth (JWT). PORTAL_USERS is a comma-separated list of
    # 'username:password:Full Name' triples.
    SECRET_KEY: str = "dev-secret-change-me"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    PORTAL_USERS: str = "admin:admin123:System Admin"

    # SAP Open Items API (basic auth) — same service the payment portal uses.
    SAP_OPEN_ITEMS_URL: str = ""
    # SAP All Items (all transactions, date-range) API — Data Upload screen.
    SAP_ALL_ITEMS_URL: str = ""
    SAP_API_USERNAME: str = ""
    SAP_API_PASSWORD: str = ""
    SAP_DEFAULT_COMPANY_CODE: str = "EPL"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
