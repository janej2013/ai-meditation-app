"""FastAPI application and Lambda entry point.

Constraint 2: this Lambda validates the JWT and starts work. It never calls
Bedrock or a TTS vendor synchronously.
"""

from fastapi import FastAPI
from mangum import Mangum

from api.routers import account, generate, health

app = FastAPI(
    title="AI Meditation API",
    version="0.1.0",
    # CORS is handled by API Gateway's preflight config, not here, so there is
    # no CORSMiddleware: two layers would only disagree with each other.
)

app.include_router(health.router)
app.include_router(account.router)
app.include_router(generate.router)

# lifespan="off": Lambda gives no startup/shutdown window worth running.
handler = Mangum(app, lifespan="off")
