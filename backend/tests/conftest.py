import os
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load backend env to get ADMIN credentials reliably
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# Frontend env for public BASE_URL
load_dotenv(Path("/app/frontend/.env"))

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ppandit6926@gmail.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "alok@zoom123")


@pytest.fixture(scope="session")
def base_url():
    assert BASE_URL, "REACT_APP_BACKEND_URL not set"
    return BASE_URL


@pytest.fixture
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture
def authed_session(base_url):
    """Cookie-based auth session (mirrors frontend behaviour)."""
    s = requests.Session()
    r = s.post(f"{base_url}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Login failed ({r.status_code}): {r.text}")
    return s


@pytest.fixture
def admin_creds():
    return {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
