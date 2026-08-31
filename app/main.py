from fastapi import FastAPI

from .routes.invoices import router as invoices_router
from .routes.promises import router as promises_router
from .routes.webhooks import router as webhooks_router
from .routes.extraction import router as extraction_router


def create_app(*args, **kwargs) -> FastAPI:
    application = FastAPI(
        title="Promise-to-Pay Recovery Orchestrator",
        version="0.4.0",
    )

    application.include_router(invoices_router)
    application.include_router(promises_router)
    application.include_router(webhooks_router)
    application.include_router(extraction_router)

    @application.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    return application


app = create_app()