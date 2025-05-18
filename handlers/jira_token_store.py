import os
import json
from handlers.app_state import redis_client
from datetime import datetime, timedelta, timezone
from handlers.jira_models import JiraToken, SessionLocal



_token_cache = {}
IN_MEMORY_CACHE_TTL = timedelta(minutes=3)

def _clean_token_cache():
    now = datetime.now(timezone.utc)
    expired_keys = []
    for uid, entry in _token_cache.items():
        token_expired = False
        token_obj = entry["token"]
        if isinstance(token_obj, dict):
            token_expired = token_obj.get("expires_at") and datetime.fromisoformat(token_obj["expires_at"]) <= now
        elif hasattr(token_obj, "token_expires_at"):
            token_expired = token_obj.token_expires_at <= now

        if token_expired or entry["expires_at"] <= now:
            expired_keys.append(uid)

    for uid in expired_keys:
        del _token_cache[uid]
        print(f"🧹 Removed expired token for {uid}")


async def save_jira_token(slack_user_id, access_token, refresh_token, expires_in, cloud_id, account_id, display_name):
    session = SessionLocal()
    token = session.get(JiraToken, slack_user_id)
    if not token:
        token = JiraToken(slack_user_id=slack_user_id)
        session.add(token)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)
    token.account_id = account_id
    token.display_name = display_name
    token.set_token(access_token)
    token.set_refresh_token(refresh_token)
    token.token_expires_at = expires_at
    token.cloud_id = cloud_id
    token.connected_at = now

    session.commit()
    session.close()

    # Cache to Redis with token expiry as TTL
    redis_key = f"jira_token:{slack_user_id}"
    redis_data = {
        "account_id": account_id,
        "display_name": display_name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "cloud_id": cloud_id,
        "expires_at": expires_at.isoformat()
    }
    ttl_seconds = int(expires_in)
    await redis_client.setex(redis_key, ttl_seconds, json.dumps(redis_data))


async def get_valid_jira_token(slack_user_id,http_client):
    now = datetime.now(timezone.utc)
    _clean_token_cache()

    # In-memory cache
    cached = _token_cache.get(slack_user_id)
    if cached and cached["expires_at"] > now:
        print(f"✅ In-memory cache hit for {slack_user_id}")
        return cached["token"]

    # Redis cache
    redis_key = f"jira_token:{slack_user_id}"
    redis_data = await redis_client.get(redis_key)
    if redis_data:
        token_data = json.loads(redis_data)
        expires_at = datetime.fromisoformat(token_data["expires_at"])
        if expires_at - now> IN_MEMORY_CACHE_TTL:
            print(f"✅ Redis cache hit for {slack_user_id}")
            _token_cache[slack_user_id] = {
                "token": token_data,
                "expires_at": now + IN_MEMORY_CACHE_TTL
            }
            return token_data

    # Postgres fallback
    session = SessionLocal()
    token = session.get(JiraToken, slack_user_id)
    if not token:
        session.close()
        return None
    # Refresh if expired
    if token.token_expires_at and (token.token_expires_at - now) <= IN_MEMORY_CACHE_TTL:
        print(f"🔄 Refreshing expired Jira token for user {slack_user_id}")
        refresh_token = token.get_refresh_token()
        refresh_response = await http_client.post("https://auth.atlassian.com/oauth/token", json={
            "grant_type": "refresh_token",
            "client_id": os.getenv("JIRA_CLIENT_ID"),
            "client_secret": os.getenv("JIRA_CLIENT_SECRET"),
            "refresh_token": refresh_token
        })

        if refresh_response.status_code != 200:
            session.close()
            raise Exception("❌ Failed to refresh Jira token.")

        refresh_data = refresh_response.json()
        new_access_token = refresh_data["access_token"]
        new_refresh_token = refresh_data.get("refresh_token", refresh_token)

        token.set_token(new_access_token)
        token.set_refresh_token(new_refresh_token)
        token.token_expires_at = now + timedelta(seconds=refresh_data.get("expires_in", 3600))
        session.commit()
        print("✅ Token refreshed and saved.")

    result = {
        "account_id": token.account_id,
        "display_name": token.display_name,
        "access_token": token.get_token(),
        "refresh_token": token.get_refresh_token(),
        "cloud_id": token.cloud_id,
        "expires_at": token.token_expires_at.isoformat()
    }
    session.close()

    # Set Redis and memory cache to expire at actual token expiration
    ttl_seconds = int((token.token_expires_at - now).total_seconds())
    await redis_client.setex(redis_key, ttl_seconds, json.dumps(result))
    _token_cache[slack_user_id] = {
        "token": result,
        "expires_at": now + IN_MEMORY_CACHE_TTL
    }
    return result
async def reset_user():
    async for key in redis_client.scan_iter("jira_token:*"):
            await redis_client.delete(key)
    _token_cache.clear()
    print("🧹 Cleared all Jira tokens from Redis and in-memory cache.")
