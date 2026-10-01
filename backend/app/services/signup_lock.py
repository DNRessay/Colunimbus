# Production lock: only addresses on a verified SES identity (the whole domain, or that exact address) may sign up.
import time

from ..config import settings

_cache = {"at": 0.0, "ids": None}


def verified_identities():
    if _cache["ids"] is not None and time.time() - _cache["at"] < 600:
        return _cache["ids"]
    import boto3

    client, ids, token = boto3.client("sesv2", region_name=settings.ses_region), set(), None
    while True:
        page = client.list_email_identities(**({"NextToken": token} if token else {}), PageSize=100)
        ids |= {i["IdentityName"].lower() for i in page.get("EmailIdentities", [])
                if i.get("VerificationStatus") == "SUCCESS"}
        token = page.get("NextToken")
        if not token:
            break
    _cache.update(at=time.time(), ids=ids)
    return ids


def signup_problem(email: str):
    """None when this address may sign up, else the reason."""
    if not settings.signup_lock:
        return None
    try:
        ids = verified_identities()
    except Exception:
        return "Sign-up can't be checked right now. Try again shortly."
    domain = email.rsplit("@", 1)[-1]
    if email in ids or domain in ids:
        return None
    return f"Sign-up is limited to registered organisations, and {domain} isn't registered yet. Ask us to add it."
