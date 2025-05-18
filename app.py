from dotenv import load_dotenv
load_dotenv("./.env")
import os,json,re,asyncio,time
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import httpx
from contextlib import asynccontextmanager
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from datetime import datetime 
from handlers.app_state import redis_client,gptclient,http_client
from handlers.modal_builder import build_project_selection_modal,build_ticket_fields_modal,open_status_modal,open_assign_modal,open_comment_modal,open_summary_modal,issue_options
from handlers.jira_client import fetch_issue_fields, build_jira_payload_from_submission, create_jira_ticket, search_similar_tickets,attach_file_to_ticket,build_home_view_for_user,build_adf_comment
from handlers.jira_token_store import save_jira_token,get_valid_jira_token,reset_user
from handlers.llm import generate_and_update_summary,gptprompt,analyze_user_query_and_respond
from handlers.project_loader import load_projects
from handlers.userfetch import resolve_user,fetchUsers,refresh_user_cache, USER_CACHE_TTL
from handlers.jira_models import SessionLocal, JiraToken
import traceback


JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
ADMIN_USER_IDS = os.getenv("SLACK_ADMIN_USERS", "").split(",")
ADMIN_LOG_CHANNEL = os.getenv("ADMIN_LOG_CHANNEL")


proj_index = {}

async def set_pending_ticket(user_id, data):
    await redis_client.set(f"user_pending:{user_id}",json.dumps(data))
async def get_pending_ticket(user_id):
    raw = await redis_client.get(f"user_pending:{user_id}")
    return json.loads(raw) if raw else None
async def clear_pending_ticket(user_id):
    await redis_client.delete(f"user_pending:{user_id}")

@asynccontextmanager
async def app_lifespan(app):
    global proj_index
    print("🔁 Calling load_projects()...")
    _, proj_index, _ = await asyncio.to_thread(load_projects)
    print("✅ Projects loaded into memory.")
    await redis_client.ping()
    await http_client.get("https://www.google.com") 
    yield
    await http_client.aclose()

fastapi_app = FastAPI(lifespan=app_lifespan)
templates = Jinja2Templates(directory="templates")
app = AsyncApp(token=os.getenv("SLACK_BOT_TOKEN"), signing_secret=os.getenv("SLACK_SIGNING_SECRET"))
handler = AsyncSlackRequestHandler(app)


@fastapi_app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    print(f"⏱️ {request.method} {request.url.path} took {duration:.3f}s")
    return response

@app.action("jiralink")
async def handle(ack, body, logger):
    await ack()

def build_jira_auth_url(user_id):
    return (
        "https://auth.atlassian.com/authorize"
        "?audience=api.atlassian.com"
        f"&client_id={os.getenv('JIRA_CLIENT_ID')}"
        f"&scope=read%3Ajira-user%20read%3Ajira-work%20write%3Ajira-work%20offline_access"
        f"&redirect_uri={os.getenv('JIRA_REDIRECT_URI')}"
        f"&response_type=code"
        f"&state={user_id}"
        f"&prompt=consent"
    )

@app.event("app_home_opened")
async def update_home_tab(event, client, logger):
    user_id = event["user"]
    try:
        token_info = await get_valid_jira_token(user_id,http_client)
        if not token_info:
            # Show login button
            blocks = [
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "*🔐 You haven’t connected your Jira account yet!*\n\n"
                "To use *JiraMate* in Slack, connect your Jira account. This enables:\n\n"
                "• 🎟️ *Create Jira tickets* quickly using `/createticket`\n"
                "• 📎 *Attach files, assign teammates,* and *add comments* right from Slack\n"
                "• 🧠 *AI-powered summaries* with `/summarize [ISSUE-KEY]` to understand tickets faster\n"
                "• 🗂️ View issues assigned or watched by you in the Home tab\n\n"
                "• ✨ Ask JiraMate about any ticket in a direct message and get answers.\n\n"
                "*Click on Connect Jira to connect your account and get started.*"
            )
        },
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "🔗 Connect Jira"},
            "url": build_jira_auth_url(user_id),
            "action_id": "jiralink"
        }
    }
]
        else:
            # Show full ticket UI
            access_token = token_info["access_token"]
            cloud_id = token_info["cloud_id"]
            blocks = await build_home_view_for_user(user_id, client,access_token,cloud_id,http_client)

        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})

    except Exception as e:
        logger.error(f"Failed to render home tab: {e}")

@app.action("create_ticket_button")
async def handle_create_ticket_button(ack, body, client):
    await ack()
    trigger_id = body["trigger_id"]
    modal = build_project_selection_modal()
    await client.views_open(trigger_id=trigger_id, view=modal)

@app.action("refresh_home")
async def update_home(ack,body, client, logger):
    await ack()
    user_id = body["user"]["id"]
    try:
        token_info = await get_valid_jira_token(user_id,http_client)
        access_token = token_info["access_token"]
        cloud_id = token_info["cloud_id"]
        blocks = await build_home_view_for_user(user_id, client,access_token,cloud_id,http_client)
        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        logger.error(f"Failed to render home tab: {e}")

@app.command("/resetjira")
async def handle_reset_jira_db(ack, body, client, logger):
    await ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]

    if user_id not in ADMIN_USER_IDS:
        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text="❌ You are not authorized to perform this action."
        )
        return

    try:
        session = SessionLocal()
        session.query(JiraToken).delete()
        session.commit()
        session.close()
        await reset_user()
        # Notify the admin
        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text="✅ All Jira tokens have been deleted from the database."
        )

        # Log to admin channel
        await client.chat_postMessage(
            channel=ADMIN_LOG_CHANNEL,
            text=f"🧹 *Admin action:* `<@{user_id}>` reset the Jira token table via `/resetjira`."
        )
        

    except Exception as e:
        logger.error(f"❌ Error clearing Jira token table: {e}")
        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text="❌ An error occurred while clearing the database."
        )

@app.command("/refreshusers")
async def handle_refresh_users(ack, body, client, logger):
    await ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]

    if user_id not in ADMIN_USER_IDS:
        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text="❌ You are not authorized to perform this action."
        )
        return

    try:
        users = await refresh_user_cache(client)

        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text=f"✅ Slack user cache has been refreshed. Loaded `{len(users)}` users `TTL: {USER_CACHE_TTL // 60} min`."
        )

        await client.chat_postMessage(
            channel=ADMIN_LOG_CHANNEL,
            text=f"🔁 *Admin action:* `<@{user_id}>` refreshed the Slack user cache using `/refreshusers`."
        )

    except Exception as e:
        logger.error(f"❌ Error refreshing user cache: {e}")
        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text="❌ An error occurred while refreshing the user cache."
        )

@app.command("/jiratoken")
async def handle_debug_jira_token(ack, body, client, logger):
    await ack()
    admin_id = body["user_id"]
    text = body.get("text", "").strip()
    channel_id = body["channel_id"]
    ADMIN_LOG_CHANNEL = os.getenv("ADMIN_LOG_CHANNEL")
    if admin_id not in ADMIN_USER_IDS:
        await client.chat_postMessage(
            channel=channel_id,
            text="❌ You are not authorized to use this command."
        )
        return
    session = SessionLocal()
    if text:
        target_id = text[1:].split("|")[0]
        user_id=await resolve_user(target_id,client,get_id="id")
        token = session.get(JiraToken, user_id)
        if not token:
            session.close()
            await client.chat_postMessage(channel=channel_id, text=f"🔍 No token found for <@{target_id}>.")
            return
        session.close()
        await client.chat_postMessage(
            channel=ADMIN_LOG_CHANNEL,
            text=(
                f"🔎 *Token Info for <@{target_id}>*\n"
                f"> Cloud ID: `{token.cloud_id}`\n"
                f"> Connected: `{token.connected_at}`\n"
                f"> Expires: `{token.token_expires_at}`"
            )
        )
        return
    session.close()
    await client.chat_postMessage(
        channel=channel_id,
        text="⚠️ Usage: /jiratoken `@user`"
    )

@app.command("/createticket")
async def create_ticket(ack, body, client,logger):
    await ack()
    user_id = body["user_id"]
    token_info = await get_valid_jira_token(user_id,http_client)
    if not token_info:
        auth_url = build_jira_auth_url(user_id)
        await client.chat_postMessage(
            channel=body["channel_id"],
            user=user_id,
            text=f"🔐 You haven't connected your Jira account. <{auth_url}|🔗 Connect Jira.>"
        )
        return
    modal = build_project_selection_modal()
    await client.views_open(trigger_id=body["trigger_id"], view=modal)
    data = await get_pending_ticket(user_id)
    if data:
        await finalize_ticket_no_attachment(user_id, data, client)

@app.command("/summarize")
async def create_ticket(ack, body, client):
    await ack()
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    token_info = await get_valid_jira_token(user_id,http_client)
    if not token_info:
        auth_url = build_jira_auth_url(user_id)
        await client.chat_postMessage(
            channel=body["channel_id"],
            user=user_id,
            text=f"🔐 You haven't connected your Jira account. <{auth_url}|🔗 Connect Jira.>"
        )
        return
    text = body.get("text", "").strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9]+-\d+$", text):
        await client.chat_postMessage(
            channel=channel_id,
            text="⚠️ Please provide a valid Jira issue key. Example: `/summarize AT-123`"
        )
        return

    issue_key = text
    load=await client.chat_postMessage(
            channel=user_id,
            user=user_id,
            blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🧠 *Generating summary for `{issue_key}`, Please wait...*"
                }
            },
            {"type": "divider"}
        ],
            text=f"🧠 Summary for {issue_key}"
        )
    token_info = await get_valid_jira_token(user_id,http_client)
    if not token_info:
        auth_url = (
            "https://auth.atlassian.com/authorize"
            "?audience=api.atlassian.com"
            f"&client_id={os.getenv('JIRA_CLIENT_ID')}"
            "&scope=read%3Ajira-user%20read%3Ajira-work%20write%3Ajira-work%20offline_access"
            f"&redirect_uri={os.getenv('JIRA_REDIRECT_URI')}"
            "&response_type=code"
            f"&state={user_id}"
            "&prompt=consent"
        )
        await client.chat_postMessage(
            channel=channel_id,
            user=user_id,
            text=f"❌ You haven't connected your Jira account. <{auth_url}|🔗 Connect Jira.>"
        )
        return
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]
    try:
        metadata = json.dumps({
            "issue_key": issue_key,
            "access_token": access_token,
            "cloud_id": cloud_id,
            "user_id": user_id
        })
        view_id = "slash-command-view"  
        await generate_and_update_summary(client, view_id, metadata,http_client,load["channel"],load["ts"])
    except Exception as e:
        await client.chat_postMessage(
            channel=channel_id,
            text=f"❌ Failed to summarize {issue_key}: {e}"
        )

@app.action("project_selected")
async def handle_project_dropdown(ack, body, client):
    await ack()
    user_id = body["user"]["id"]
    project_key = body["actions"][0]["selected_option"]["value"]
    issue_id=issue_options(project_key)[0]["value"]
    p=proj_index.get(project_key)
    project_name=p["name"]
    fields = await fetch_issue_fields(user_id, project_key,issue_id,http_client)
    view_id = body["view"]["id"]
    updated_view = await build_ticket_fields_modal(fields,project_key,project_name,issue_id)
    await client.views_update(
        view_id=view_id,
        view=updated_view
    )

@app.action("issue_selected")
async def handle_issue_type_selected(ack, body, client):
    await ack()
    metadata = json.loads(body["view"]["private_metadata"])
    project_key=metadata.get("project_key")
    project_name=metadata.get("project_name")
    user_id = body["user"]["id"]
    view_id = body["view"]["id"]
    selected_issue_type_id = body["actions"][0]["selected_option"]["value"]
    fields = await fetch_issue_fields(user_id, project_key, selected_issue_type_id,http_client)
    updated_modal = await build_ticket_fields_modal(fields, project_key, project_name, selected_issue_type_id)
    await client.views_update(view_id=view_id, view=updated_modal)

@app.options("assignee")
async def handle_external_options(ack, body):
    user_input = body.get("value", "")
    user_id = body["user"]["id"]
    block_id = body["block_id"]
    project_key = block_id.split("|")[1]

    results = await fetch_assignable_users(user_id,project_key, user_input)

    options = [{"text": {"type": "plain_text", "text": "Unassigned"}, "value": "null"}] + [
    {"text": {"type": "plain_text", "text": user["displayName"]}, "value": user["accountId"]}
    for user in results
    ]

    await ack(options=options)

async def fetch_assignable_users(user_id,project_key, query):
    token_info=await get_valid_jira_token(user_id,http_client)
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/user/assignable/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    params = {"project": project_key, "query": query}
    response = await http_client.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"❌ Failed to fetch users: {response.status_code}")
        return []

@app.options("input_value")
async def load_external_options(ack, body, logger):
    query = (body.get("value") or "").lower()
    block_id = body.get("block_id") 

    logger.info(f"[options] Query: '{query}' | Block ID: '{block_id}'")
    cache_key = f"external_fields:{block_id}"
    raw = await redis_client.get(cache_key)
    if not raw:
        logger.warning(f"[options] No cached values found for {block_id}")
        await ack(options=[{
            "text": {"type": "plain_text", "text": "⚠️ No options available"},
            "value": "no_options"
        }])
        return
    try:
        allowed = json.loads(raw)
    except Exception as e:
        logger.error(f"[options] Failed to parse JSON for {block_id}: {e}")
        await ack(options=[])
        return
    matches = []
    for opt in allowed:
        label = opt.get("name") or opt.get("value") or str(opt)
        value = opt.get("id") or opt.get("value") or str(opt)

        if label and query in label.lower():
            matches.append({
                "text": {"type": "plain_text", "text": label[:75]},
                "value": value
            })
            if len(matches) >= 100:
                break
    logger.info(f"[options] Returning {len(matches)} matches for query: '{query}'")
    if not matches:
        matches.append({
            "text": {"type": "plain_text", "text": "No matches found"},
            "value": "no_match"
        })
    await ack(options=matches)

@app.view("submit_ticket_modal")
async def handle_ticket_submission(ack, body, client, view, logger):
    await ack()
    user_id = body["user"]["id"]
    state_values = view["state"]["values"]
    metadata = json.loads(body["view"]["private_metadata"])
    project_key = metadata.get("project_key")
    project_name = metadata.get("project_name")
    issue_type_id = metadata.get("issue_type_id")
    issue_name = metadata.get("issue_name")
    title = state_values.get("summary", {}).get("input_value", {}).get("value", "")
    description = state_values.get("description", {}).get("input_value", {}).get("value", "")
    await set_pending_ticket(user_id, {
        "state_values": state_values,
        "project_key": project_key,
        "issue_type_id": issue_type_id,
        "project_name": project_name,
        "issue_name": issue_name
    })
    await process_ticket_similarity_async(client, user_id, title, description, project_key, issue_name)

async def summarize_with_gpt4o(current_title, current_description, past_issues):
    prompt = gptprompt(current_title, current_description, past_issues)

    response = await gptclient.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": """
    You are a Slack-integrated assistant that summarizes Jira tickets professionally.

    ### Behavior:
    - Always speak as a Slack bot — concise, professional, and helpful.
    - Format output in Slack mrkdwn style.
    - Do NOT include greetings or intros like "Based on your request" or "Here are the results".

    ### Format:
    - Start with: `Here's a summary of past Jira Tickets:`
    - Then, for each ticket:
    • *<TICKET-KEY>* <summary>  
    ↳ _Resolution_: <how it was resolved or suggestion>
    At the end, tell which of the past tickets are most similar to current ticket?
    - Keep responses to 2-3 lines per ticket.
    - Be clear, human-readable, and avoid sounding robotic.
    """
            },
            {
                "role": "user",
                "content": prompt 
            }
        ],
        temperature=0.5,
        max_tokens=1000
    )
    return response.choices[0].message.content.strip()

async def process_ticket_similarity_async(client, user_id, title, description, project_key, issue_type):
    try:
        similar_tickets = await search_similar_tickets(user_id, title, project_key, issue_type,http_client)
        if not similar_tickets:
            await proceed_to_ticket_creation(client, user_id)
            return
        load=await client.chat_postMessage(
            channel=user_id,
            user=user_id,
            blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🧠 *Generating a Summary of Similar Tickets Please Wait...*"
                }
            },
            {"type": "divider"}
        ],
            text="🔍 Similar tickets found — review before creating."
        )
        summary = await summarize_with_gpt4o(title,description,similar_tickets)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🧠 *Summary of Similar Tickets:*\n>{summary}"
                }
            },
            {"type": "divider"}
        ]

        for ticket in similar_tickets:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*<https://{JIRA_DOMAIN}/browse/{ticket['key']}|{ticket['key']}>* - {ticket['summary']} | Status: *{ticket['status']}*"
                }
            })

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Create Anyway"},
                    "style": "primary",
                    "action_id": "create_ticket_confirmed"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Cancel"},
                    "style": "danger",
                    "action_id": "cancel_ticket_creation"
                }
            ]
        })

        response=await client.chat_update(
            channel=load["channel"],
            ts=load["ts"],
            blocks=blocks,
            text="🔍 Similar tickets found — review before creating."
        )
        data = await get_pending_ticket(user_id)
        data.update({
            "summary_channel": response["channel"],
            "summary_ts": response["ts"],
            "summary_blocks": blocks
        })
        await set_pending_ticket(user_id, data)
    except Exception as e:
        await client.chat_postMessage(
            channel=user_id,
            user=user_id,
            text=f"❌ Error running similarity check: {str(e)}"
        )
        traceback.print_exc() 

@app.action("create_ticket_confirmed")
async def handle_create_ticket(ack, body, client, logger):
    await ack()
    user_id = body["user"]["id"]
    data=await get_pending_ticket(user_id)
    existing_blocks = data.get("summary_blocks", [])
    blocks = [
        block for block in existing_blocks
        if block.get("type") != "actions"
    ]
    await client.chat_update(
        channel=data["summary_channel"],
        ts=data["summary_ts"],
        text=f"🎟️ Jira Ticket Created!",
        blocks=blocks
    )
    await proceed_to_ticket_creation(client, user_id)

@app.action("cancel_ticket_creation")
async def handle_cancel_ticket(ack, body, client):
    await ack()
    user_id = body["user"]["id"]
    data=await get_pending_ticket(user_id)
    existing_blocks = data.get("summary_blocks", [])
    blocks = [
        block for block in existing_blocks
        if block.get("type") != "actions"
    ]
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🎟️ Jira Ticket Creation *Cancelled!*"
            }
        }
    )
    await client.chat_update(
        channel=data["summary_channel"],
        ts=data["summary_ts"],
        text=f"🎟️ Jira Ticket Creation *Cancelled!*",
        blocks=blocks
    )

async def proceed_to_ticket_creation(client, user_id):
    if not await get_pending_ticket(user_id):
        return

    user_data = await get_pending_ticket(user_id)
    await clear_pending_ticket(user_id)
    load = await client.chat_postMessage(
            channel=user_id,
            text=f"🎟️ Creating Jira Ticket.. Please wait.",
            blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"🎟️ Creating Jira Ticket.. Please wait."
                        }
                    }]
    )
    payload = build_jira_payload_from_submission(
        user_data["state_values"],
        user_data["project_key"],
        user_data["issue_type_id"]
    )
    ticket_url = await create_jira_ticket(user_id,payload,http_client)

    if ticket_url:
        summary = user_data["state_values"].get("summary", {}).get("input_value", {}).get("value", "-")
        now_str = datetime.now().strftime('%b %d, %Y %I:%M %p')

        response = await client.chat_update(
            channel=load["channel"],
            ts=load["ts"],
            text=f"🎟️ Jira Ticket Created: *<{ticket_url}|{ticket_url.split('/')[-1]}>*",
            blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"🎟️ Jira Ticket Created: *<{ticket_url}|{ticket_url.split('/')[-1]}>*"
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Project:* {user_data['project_name']} ({user_data['project_key']})"
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Issue Type:* {user_data['issue_name']}"
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Summary:* {summary}"
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Submitted:* {now_str}"
                            },
                            {
                                "type": "mrkdwn",
                                "text": "*Status:* Created ✅"
                            }
                        ]
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "📎 Add Attachment"},
                                "action_id": "add_attachment",
                                "value": ticket_url
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "❌ No Thanks"},
                                "action_id": "no_attachment"
                            }
                        ]
                    }
                ]
            )

        await set_pending_ticket(user_id, {
            "ticket_url": ticket_url,
            "project_key": user_data["project_key"],
            "issue_type_id": user_data["issue_type_id"],
            "state_values": user_data["state_values"],
            "project_name":user_data["project_name"],
            "issue_name":user_data["issue_name"],
            "channel_id": response["channel"],
            "message_ts": response["ts"],
            "time": now_str
        })

    else:
        await client.chat_postMessage(channel=user_id, text=f"❌ Failed to create Jira ticket.")

@app.event("message")
async def handle_message_events(event, client, logger):
    subtype = event.get("subtype")
    channel_type = event.get("channel_type")
    user_id = event.get("user")
    channel = event.get("channel")
    text = event.get("text", "").strip()
    token_info = await get_valid_jira_token(user_id,http_client)
    if not token_info:
        auth_url = build_jira_auth_url(user_id)
        await client.chat_postMessage(
            channel=channel,
            user=user_id,
            text=f"🔐 You haven't connected your Jira account. <{auth_url}|🔗 Connect Jira.>"
        )
        return
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]
    # 🔹 1. Handle file_share event for ticket attachments
    if subtype == "file_share":
        data = await get_pending_ticket(user_id)
        if not data or "ticket_url" not in data:
            logger.info(f"No ticket context for user {user_id}")
            return
        ticket_key = data["ticket_url"].split("/")[-1]
        summary = data["state_values"].get("summary", {}).get("input_value", {}).get("value", "-")

        successful_uploads = 0
        for file_info in event.get("files", []):
            file_name = file_info["name"]
            file_url = file_info["url_private_download"]
            try:
                headers = {"Authorization": f"Bearer {os.getenv('SLACK_BOT_TOKEN')}"}
                response = await http_client.get(file_url, headers=headers)
                file_content = response.content
                success = await attach_file_to_ticket(user_id, ticket_key, file_name, file_content, http_client)
                if success:
                    successful_uploads += 1
                    await client.chat_postMessage(channel=user_id, text=f"📎 `{file_name}` attached to ticket `{ticket_key}`.")
                else:
                    await client.chat_postMessage(channel=user_id, text=f"❌ Failed to attach `{file_name}`.")
            except Exception as e:
                logger.error(f"Error attaching `{file_name}`: {e}")
                await client.chat_postMessage(channel=user_id, text=f"⚠️ Error while attaching `{file_name}`.")

        if successful_uploads > 0:
            await client.chat_update(
                channel=data["channel_id"],
                ts=data["message_ts"],
                text=f"🎟️ Jira Ticket Created: *<{data['ticket_url']}|{ticket_key}>*",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"🎟️ Jira Ticket Created: *<{data['ticket_url']}|{ticket_key}>*"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Project:* {data['project_name']} ({data['project_key']})"},
                            {"type": "mrkdwn", "text": f"*Issue Type:* {data['issue_name']}"},
                            {"type": "mrkdwn", "text": f"*Summary:* {summary}"},
                            {"type": "mrkdwn", "text": f"*Submitted:* {data['time']}"},
                            {"type": "mrkdwn", "text": "*Status:* Created ✅"},
                            {"type": "mrkdwn", "text": f"*Attachment:* 📎 {successful_uploads} file(s) attached"}
                        ]
                    }
                ]
            )
            await clear_pending_ticket(user_id)
        return

    # 🔹 2. GPT DM Assistant — only handle messages in direct messages (channel_type == "im")
    if channel_type == "im" and subtype is None and text and not text.startswith("<@"):
        try:
            # Step 1: Post loading message
            loading = await client.chat_postMessage(
                channel=channel,
                text="🧠 Analyzing your query...",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🧠 *Analyzing your query...* Please wait a moment."
                        }
                    }
                ]
            )
            # Step 2: GPT + Jira processing
            response = await analyze_user_query_and_respond(text, access_token, cloud_id)
            print(response)
            # Step 3: Format final response in blocks
            await client.chat_update(
                channel=loading["channel"],
                ts=loading["ts"],
                text="🧠 JiraMate AI",  # fallback text for clients that don't support blocks
                blocks=response
            )
        except Exception as e:
            logger.error(f"GPT DM error: {e}")
            await client.chat_postMessage(
                channel=channel,
                text="⚠️ Something went wrong while answering your question.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⚠️ *Something went wrong while answering your question.*"
                        }
                    }
                ]
            )

@app.action("add_attachment")
async def handle_add_attachment(ack, body, client, logger):
    await ack()
    user_id = body["user"]["id"]
    data = await get_pending_ticket(user_id)
    if not data:
        logger.warning(f"No ticket context found for user {user_id}")
        return
    summary = data["state_values"].get("summary", {}).get("input_value", {}).get("value", "-")
    ticket_key=data['ticket_url'].split('/')[-1]
    await client.chat_update(
        channel=data["channel_id"],
        ts=data["message_ts"],
        text=f"🎟️ Jira Ticket Created: *<{data['ticket_url']}|{ticket_key}>*",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🎟️ Jira Ticket Created: *<{data['ticket_url']}|{ticket_key}>*"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Project:* {data['project_name']} ({data['project_key']})"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Issue Type:* {data['issue_name']}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Summary:* {summary}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Submitted:* {data['time']}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": "*Status:* Created ✅"
                    },
                    {
                        "type": "mrkdwn",
                        "text": "*Attachment:* 📎 Waiting for your file upload..."
                    }  
                ]
            }
        ]
    )

    await client.chat_postMessage(
        channel=user_id,
        text="📎 Please upload the file(s) you want to attach by replying here."
    )

@app.action("no_attachment")
async def handle_no_attachment(ack, body, client, logger):
    await ack()
    user_id = body["user"]["id"]
    data = await get_pending_ticket(user_id)
    if not data:
        return
    await finalize_ticket_no_attachment(user_id, data, client)
    logger.info(f"User {user_id} opted out of attaching a file.")

async def finalize_ticket_no_attachment(user_id, data, client):
    summary = data["state_values"].get("summary", {}).get("input_value", {}).get("value", "-")
    ticket_key = data['ticket_url'].split('/')[-1]

    updated_blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🎟️ Jira Ticket Created: *<{data['ticket_url']}|{ticket_key}>*"
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Project:* {data['project_name']} ({data['project_key']})"},
                {"type": "mrkdwn", "text": f"*Issue Type:* {data['issue_name']}"},
                {"type": "mrkdwn", "text": f"*Summary:* {summary}"},
                {"type": "mrkdwn", "text": f"*Submitted:* {data['time']}"},
                {"type": "mrkdwn", "text": "*Status:* Created ✅"},
                {"type": "mrkdwn", "text": "*Attachment:* _No attachment added through slack._"}
            ]
        }
    ]

    await client.chat_update(
        channel=data["channel_id"],
        ts=data["message_ts"],
        text=f"🎟️ Jira Ticket Created: *<{data['ticket_url']}|{ticket_key}>*",
        blocks=updated_blocks
    )
    await clear_pending_ticket(user_id)
    

@app.action(re.compile(r"overflow_menu_.*"))
async def handle_overflow_action(ack, body, action, client,logger):
    await ack()
    user_id = body["user"]["id"]
    token_info = await get_valid_jira_token(user_id,http_client)
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]
    trigger_id = body["trigger_id"]
    account_id=token_info["account_id"]
    selected_value = action["selected_option"]["value"]
    action_type, data = selected_value.split(":", 1)

    if action_type == "assign":
        if "|" in data:
            issue_key, current_assignee_id = data.split("|", 1)
        else:
            issue_key, current_assignee_id = data, None
        await open_assign_modal(client, trigger_id=body["trigger_id"], issue_key=issue_key, user_id=user_id,access_token=access_token,cloud_id=cloud_id,current_assignee_id=current_assignee_id)

    elif action_type == "change_status":
        issue_key = data
        await open_status_modal(client, trigger_id=body["trigger_id"], issue_key=issue_key, user_id=user_id,access_token=access_token,cloud_id=cloud_id,http_client=http_client)

    elif action_type == "comment":
        issue_key = data
        await open_comment_modal(client, trigger_id=trigger_id, issue_key=issue_key, user_id=user_id,access_token=access_token,cloud_id=cloud_id)

    elif action_type == "unwatch":
        issue_key = data
        await handle_unwatch(client, user_id=user_id, issue_key=issue_key,access_token=access_token,cloud_id=cloud_id,account_id=account_id)
    elif action_type=="summarize":
        issue_key=data
        await open_summary_modal(client,trigger_id=trigger_id,issue_key=issue_key,user_id=user_id,access_token=access_token,cloud_id=cloud_id,http_client=http_client)
    else:
        logger.warn(f"Unknown overflow option selected: {selected_value}")

@app.view("submit_status_update")
async def handle_status_submit(ack, body, client, view):
    metadata = json.loads(view["private_metadata"])
    issue_key = metadata["issue_key"]
    cloud_id = metadata["cloud_id"]
    access_token = metadata["token"]
    user_id = body["user"]["id"]
    selected_status_id = view["state"]["values"]["status_block"]["selected_status"]["selected_option"]["value"]
    await http_client.post(
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}/transitions",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json={"transition": {"id": selected_status_id}}
    )
    try:
        blocks = await build_home_view_for_user(user_id, client,access_token,cloud_id,http_client)
        await ack()
        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        print(f"⚠️ Failed to refresh home tab: {e}")
      
@app.view("submit_assignee_update")
async def handle_assignee_submit(ack, body, client, view):
    user_id = body["user"]["id"]
    metadata = json.loads(view["private_metadata"])
    issue_key = metadata["issue_key"]
    access_token = metadata["access_token"]
    cloud_id = metadata["cloud_id"]
    slack_user_id = view["state"]["values"]["assignee_block"]["selected_assignee"]["selected_option"]["value"]
    token_info=await get_valid_jira_token(slack_user_id,http_client)
    if not token_info:
        target_email=await resolve_user(slack_user_id,client,get_id="email")
        url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/user/search?query={target_email}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        response = await http_client.get(url, headers=headers)
        if response.status_code == 200:
            user = response.json()
            account_id= user[0]["accountId"]
        else:
            print("❌ Error:", response.status_code, response.text)
    else:
        account_id=token_info['account_id']
    await ack()
    assign_response = await http_client.put( 
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}/assignee",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"accountId": account_id}
    )
    if assign_response.status_code != 204:
        print(f"❌ Failed to assign {issue_key}: {assign_response.text}")
        return
    try:
        blocks = await build_home_view_for_user(user_id, client,access_token,cloud_id,http_client)
        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        print(f"⚠️ Failed to refresh home tab: {e}")

@app.options("selected_assignee")
async def load_user_options(ack, body, client):
    current_user_id = body["user"]["id"]
    metadata = json.loads(body["view"]["private_metadata"])
    current_assignee = metadata.get("current_assignee")
    try:
        user_info = await client.users_info(user=current_user_id)
        display_name = user_info["user"]["real_name"] or user_info["user"]["name"]
    except Exception:
        display_name = "You"
    options = []
    if current_user_id != current_assignee:
        options.append({
            "text": {"type": "plain_text", "text": f"{display_name} (Assign to me)"},
            "value": current_user_id
        })
    users = await resolve_user("", client, get_id="all")
    for u in users:
        if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT" or u.get("id")==current_user_id:
            continue
        uid = u["id"]
        name = u.get("real_name", u.get("name", ""))
        prefix = "✅ " if uid == current_assignee else ""
        options.append({
            "text": {"type": "plain_text", "text": f"{prefix}{name[:75]}"},
            "value": uid
        })
    await ack(options=options[:100])

@app.view("submit_comment_modal")
async def handle_comment_submit(ack, body, client, view):
    await ack()
    user_id = body["user"]["id"]
    metadata = json.loads(view["private_metadata"])
    issue_key = metadata["issue_key"]
    cloud_id = metadata["cloud_id"]
    access_token = metadata["access_token"]
    comment_text = view["state"]["values"]["comment_block"]["comment_input"]["value"]
    selected_users = view["state"]["values"]["mention_block"]["mentions"]["selected_options"]
    slack_user_ids = [opt["value"] for opt in selected_users]
    payload=json.dumps(await build_adf_comment(comment_text,slack_user_ids,client,access_token,cloud_id,http_client))
    response = await http_client.post( 
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}/comment",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        data=payload
    )
    if response.status_code != 201:
        print(f"❌ Failed to add comment to {issue_key}: {response.text}")
        return
    try:
        blocks = await build_home_view_for_user(user_id, client,access_token,cloud_id,http_client)
        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
    except Exception as e:
        print(f"⚠️ Failed to refresh home tab: {e}")

@app.options("mentions")
async def load_user(ack, body, client):
    current_user_id = body["user"]["id"]
    options = []
    users = await resolve_user("", client, get_id="all")
    for u in users:
        if u.get("id")==current_user_id:
            continue
        uid = u["id"]
        name = u.get("real_name", u.get("name", ""))
        options.append({
            "text": {"type": "plain_text", "text": f"{name[:75]}"},
            "value": uid
        })
    await ack(options=options[:100])

async def handle_unwatch(client, user_id, issue_key,access_token,cloud_id,account_id):
    try:
        unwatch_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}/watchers"

        response = await http_client.delete( 
            unwatch_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            },
            params={  # ✅ accountId as query param
                "accountId": account_id
            }
        )
        if response.status_code != 204:
            print(f"❌ Failed to unwatch issue {issue_key}: {response.text}")
            return
        blocks = await build_home_view_for_user(user_id, client,access_token,cloud_id,http_client)
        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})

    except Exception as e:
        print(f"❌ handle_unwatch failed: {e}")

@app.error
async def global_error_handler(error, body, logger):
    logger.error(f"Unhandled error: {error}")
    import traceback
    traceback.print_exc()

@fastapi_app.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)

@fastapi_app.get("/jira/oauth/callback", response_class=HTMLResponse)
async def jira_oauth_callback(request: Request):
    code = request.query_params.get("code")
    user_id = request.query_params.get("state")

    token_response = await http_client.post("https://auth.atlassian.com/oauth/token", json={
        "grant_type": "authorization_code",
        "client_id": os.getenv("JIRA_CLIENT_ID"),
        "client_secret": os.getenv("JIRA_CLIENT_SECRET"),
        "code": code,
        "redirect_uri": os.getenv("JIRA_REDIRECT_URI")
    })

    if token_response.status_code != 200:
        print("❌ Token exchange failed:", token_response.text)
        return templates.TemplateResponse("error.html", {"request": request, "bot_id": os.getenv("SLACK_BOT_USER_ID")})
    token_data = token_response.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token") 
    expires_in = token_data.get("expires_in", 3600)
    # Fetch cloud ID
    resource_response = await http_client.get( 
        "https://api.atlassian.com/oauth/token/accessible-resources",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if resource_response.status_code != 200:
        return "❌ Failed to retrieve accessible Jira resources."
    cloud_id = resource_response.json()[0]["id"]
    # Fetch user info
    user_resp = await http_client.get( 
    f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/myself",
    headers={"Authorization": f"Bearer {access_token}"}
    )
    user_details=user_resp.json()
    display_name = user_details.get("displayName", "unknown")
    account_id=user_details.get("accountId","unknown")
    # Save tokens
    await save_jira_token(user_id, access_token, refresh_token or "", expires_in, cloud_id,account_id,display_name)
    return templates.TemplateResponse("success.html", {"request": request, "display_name": display_name, "bot_id": os.getenv("SLACK_BOT_USER_ID")})

if __name__ == "__main__":
    asyncio.run(fetchUsers(app.client))
    import uvicorn
    uvicorn.run("app:fastapi_app", host="0.0.0.0", port=3000, reload=True)
    
