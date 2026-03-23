#!/usr/bin/env python3
"""
Rode localmente para testar antes do deploy:
  python run_local.py
"""
import os
from dotenv import load_dotenv
load_dotenv()

import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.index:app", host="0.0.0.0", port=8000, reload=True)
