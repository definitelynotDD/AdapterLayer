#!/usr/bin/env python3
"""
Production start script for Railway deployment.
Runs FastAPI on 0.0.0.0:$PORT (Railway sets PORT env var).
"""
import os
import uvicorn
from server import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"\n  🚀 API Explorer starting on port {port}\n")
    uvicorn.run(
        app,
        host="0.0.0.0",  # Listen on all interfaces for Railway
        port=port,
        log_level="info"
    )
