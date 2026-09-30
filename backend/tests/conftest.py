import os

# Must run before `app` is imported anywhere: the engine is created at import time.
os.environ["DATABASE_URL"] = "sqlite:///./test_colunimbus.db"
os.environ["SECRET_KEY"] = "test-secret-test-secret-test-secret-xx"
os.environ["GROQ_API_KEYS"] = ""

if os.path.exists("test_colunimbus.db"):
    os.remove("test_colunimbus.db")
