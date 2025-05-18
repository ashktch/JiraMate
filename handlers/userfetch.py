import os
import json
from handlers.app_state import redis_client
import redis.exceptions  # Add this at the top
from handlers.jira_token_store import redis_client
from datetime import datetime, timedelta, timezone

"""redis_client = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)"""

# Constants
USER_CACHE_TTL = 60 * 60 * 2  # 2 hours
DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/149/149071.png"
REDIS_USER_LIST_KEY = "slack:users"

# In-memory user cache
_user_cache = {
    "data": None,
    "expires_at": datetime.now(timezone.utc),
    "by_email": {},
    "by_username": {},
    "by_id": {}
}

def is_valid_user(u):
    return u.get("id") != "USLACKBOT" and not u.get("deleted") and not u.get("is_bot") and not u.get("is_app_user")

def index_users(users):
    _user_cache["by_email"].clear()
    _user_cache["by_username"].clear()
    _user_cache["by_id"].clear()

    for u in users:
        profile = u.get("profile", {})
        email = profile.get("email")
        uname = u.get("name", "").lower()
        display = profile.get("display_name", "").lower()
        real = profile.get("real_name_normalized", "").lower()

        if email:
            _user_cache["by_email"][email] = u
        for name in {uname, display, real}:
            if name:
                _user_cache["by_username"][name] = u
        _user_cache["by_id"][u["id"]] = u

async def resolve_user(text, client, get_id, force_refresh=False):
    now = datetime.now(timezone.utc)

    try:
        if force_refresh or not await redis_client.exists(REDIS_USER_LIST_KEY) or _user_cache["expires_at"] <= now:
            print("🔁 Fetching fresh user list from Slack...")
            response = await client.users_list()
            members = response["members"]
            valid_users = [u for u in members if is_valid_user(u)]

            try:
                await redis_client.setex(REDIS_USER_LIST_KEY, USER_CACHE_TTL, json.dumps(valid_users))
            except redis.exceptions.RedisError as e:
                print(f"⚠️ Redis set failed: {e}")

            _user_cache["data"] = valid_users
            _user_cache["expires_at"] = now + timedelta(seconds=USER_CACHE_TTL)
            index_users(valid_users)

        elif not _user_cache["data"]:
            try:
                cached = await redis_client.get(REDIS_USER_LIST_KEY)
                if cached:
                    _user_cache["data"] = json.loads(cached)
                    _user_cache["expires_at"] = now + timedelta(seconds=USER_CACHE_TTL)
                    index_users(_user_cache["data"])
            except redis.exceptions.RedisError as e:
                print(f"⚠️ Redis get failed: {e}")

    except Exception as e:
        print("❌ Slack user fetch failed:", e)
        return None if get_id != "profile" else (None, DEFAULT_AVATAR)

    # Resolving user based on mode
    if get_id == "id":
        if text.startswith("<@") and text.endswith(">"):
            return text[2:-1].split("|")[0]
        user = _user_cache["by_username"].get(text.lstrip("@").lower())
        return user["id"] if user else None

    elif get_id == "profile":
        user = _user_cache["by_email"].get(text)
        return (user["id"], user["profile"].get("image_72", DEFAULT_AVATAR)) if user else (None, DEFAULT_AVATAR)

    elif get_id == "email":
        user = _user_cache["by_id"].get(text)
        return user["profile"].get("email") if user else None

    return _user_cache["data"]

def fetchUsers(client):
    return resolve_user("", client, get_id="data", force_refresh=True)

async def refresh_user_cache(client):
    await redis_client.delete(REDIS_USER_LIST_KEY)
    _user_cache["data"] = None
    _user_cache["expires_at"] = datetime.now(timezone.utc)
    users =await resolve_user("", client, get_id="data", force_refresh=True)
    return users
