"""Quick smoke test: confirms an OpenAI API key is valid by listing models."""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from openai import OpenAI

api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    print("OPENAI_API_KEY not set (check .env or your shell environment).")
    sys.exit(1)

client = OpenAI(api_key=api_key)

try:
    models = client.models.list()
    print(f"Key is valid. {len(models.data)} models visible, e.g. {models.data[0].id}")
except Exception as e:
    print(f"Key check failed: {e}")
    sys.exit(1)
