import asyncio
import os,json
from handlers.userfetch import resolve_user
from handlers.jira_token_store import get_valid_jira_token
from pathlib import Path
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")


def extract_adf_text(adf):
    if not isinstance(adf, dict):
        return ""
    text = ""
    for block in adf.get("content", []):
        for inline in block.get("content", []):
            text += inline.get("text", "") + " "
    return text.strip()
# --- Fetching Fields ---
async def fetch_issue_fields(slack_user_id,project_key, issue_type_id,http_client):
    path = Path(f"fields/{project_key}/{issue_type_id}.json")
    if path.exists():
        with await asyncio.to_thread(open, path, "r") as f:
            return json.load(f)
        
    else:
        token_info = await get_valid_jira_token(slack_user_id,http_client)
        access_token = token_info["access_token"]
        cloud_id = token_info["cloud_id"]

        url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }

        response = await http_client.get(url, headers=headers)

        if response.status_code == 200:
            fields=response.json().get("fields", [])
            if isinstance(fields, list):
                fields = {f["key"]: f for f in fields if "key" in f}
            return fields
        else:
            print(f"Error fetching fields: {response.status_code} {response.text}")
            return []
# --- Building Payload ---
def build_jira_payload_from_submission(state_values, project_key, issue_type_id):
    fields_payload = {
        "project": {"key": project_key},
        "issuetype": {"id": issue_type_id}
    }
    ADF_FIELDS = {"description"} 
    for block_ids, block_data in state_values.items():
        block_id=block_ids.split("|")[0]
        if block_id in ["project_block","issue_block"]:
            continue
        action_id, user_input = list(block_data.items())[0]
        if not user_input:
            continue
        input_value = user_input.get("value") or user_input.get("selected_option") or user_input.get("selected_options")
        if block_id in ADF_FIELDS and isinstance(input_value, str):
            fields_payload[block_id] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": input_value}]
                    }
                ]
            }
        elif isinstance(input_value, dict):
            fields_payload[block_id] = {"id": input_value.get("value")}
        elif isinstance(input_value, list):
            fields_payload[block_id] = [{"id": opt.get("value")} for opt in input_value]
        else:
            fields_payload[block_id] = input_value
    assignee = fields_payload.get("assignee")
    if (
        assignee in (None, "null")
        or (isinstance(assignee, dict) and assignee.get("id") in (None, "", "null"))
    ):
        fields_payload.pop("assignee", None)
    return {"fields": fields_payload}

def extract_comment_text(comment):
    body = comment.get("body", {})
    parts = []

    for para in body.get("content", []):
        for token in para.get("content", []):
            if token.get("type") == "text":
                parts.append(token.get("text", ""))
            elif token.get("type") == "mention":
                parts.append(token.get("attrs", {}).get("text", ""))
    return " ".join(parts).strip()

async def search_similar_tickets(slack_user_id,summary, project_key, issue_type_name,http_client):
    token_info = await get_valid_jira_token(slack_user_id,http_client)
    if not token_info:
        raise Exception("Jira account not connected")
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]
    jql = f'''
        project = {project_key} AND
        issuetype = "{issue_type_name}" AND
        summary ~ "{summary}" 
        ORDER BY created DESC
    '''.strip()

    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    response = await http_client.get(url, headers=headers, params={
        "jql": jql,
        "fields": "summary,status,comment,description",
        "maxResults": 5
    })

    if response.status_code != 200:
        print(f"Error searching tickets: {response.status_code} {response.text}")
        return []

    issues = response.json().get("issues", [])
    results = []

    for issue in issues:
        fields = issue.get("fields", {})
        comments = fields.get("comment", {}).get("comments", [])
        all_comments_text= "\n".join(
            f"- {c.get('author', {}).get('displayName', 'Someone')}: {extract_comment_text(c)}"
            for c in comments
        )
        plain_desc = extract_adf_text(fields.get("description"))

        results.append({
            "key": issue["key"],
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", ""),
            "description": plain_desc.strip(),
            "last_comment":  all_comments_text.strip()
        })
    return results

# --- Ticket Creation ---
async def create_jira_ticket(slack_user_id, payload,http_client):
    token_info = await get_valid_jira_token(slack_user_id,http_client)
    if not token_info:
        raise Exception("Jira account not connected. Please run /connectjira")
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]

    response = await http_client.post( 
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        },
        json=payload
    )

    if response.status_code >= 300:
        raise Exception(f"Jira API Error: {response.status_code} {response.text}")

    issue_key = response.json()["key"]
    return f"https://{JIRA_DOMAIN}/browse/{issue_key}"

async def attach_file_to_ticket(slack_user_id,issue_key, filename, file_bytes,http_client):
    token_info = await get_valid_jira_token(slack_user_id,http_client)
    if not token_info:
        raise Exception("Jira account not connected. Please run /connectjira")
    access_token = token_info["access_token"]
    cloud_id = token_info["cloud_id"]
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}/attachments"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Atlassian-Token": "no-check"
    }
    files = {
        "file": (filename, file_bytes)
    }

    response = await http_client.post(url, headers=headers, files=files)

    if response.status_code in (200, 201):
        return True
    else:
        print(f"File attachment failed: {response.status_code} {response.text}")
        return False

async def build_home_view_for_user(user_id, client,access_token,cloud_id,http_client):
    greeting = {
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text": f"""👋 *Hey there!*

Welcome back to *JiraMate* — your personal Slack companion for managing Jira issues.

Let’s get things moving 🚀"""
    },"accessory": {
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": "🔁 Refresh",
            "emoji": True
        },
        "action_id": "refresh_home"
    }
}


    blocks = [greeting, {
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text":f"\n\n" 
    }}]
    assigned, watching = await asyncio.gather(
    fetch_assigned_issues(access_token, cloud_id, user_id, client,http_client),
    fetch_watching_issues(access_token, cloud_id, user_id, client,http_client)
    )
    if assigned:
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🧑‍💻 Assigned to You"}})
        blocks.append({"type": "divider"}) 
        for issue in assigned[:3]:
            blocks += ticket_block(issue)
        if len(assigned)>3:
            blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"🔗 View {len(assigned)-3} more in Jira",
                        "emoji": True
                    },
                    "url": f"https://{JIRA_DOMAIN}/issues/?jql=assignee=currentUser()%20AND%20statusCategory!=Done%20ORDER%20BY%20updated%20DESC",
                    "action_id": "jiralink" 
                }
            ]
        })
    elif watching:
        blocks.append({
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text":
            f"🎉 Looks like you have no assigned Jira issues right now!\n\n"
            f"Stay awesome ✨ — or check in directly on your Jira board: 🔗 <https://{JIRA_DOMAIN}/jira/your-work|Open Jira>"

        
    }
})
    if watching:
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "👁️ Watching"}})
        blocks.append({"type": "divider"}) 
        for issue in watching[:3]:
            blocks += ticket_block(issue)
        if len(watching)>3:
            blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"🔗 View {len(watching)-3} more in Jira",
                        "emoji": True
                    },
                    "url": f"https://{JIRA_DOMAIN}/issues/?jql=issue%20in%20watchedIssues()%20AND%20(assignee%20is%20EMPTY%20OR%20assignee%20!=%20currentUser())%20AND%20statusCategory%20!=%20Done%20ORDER%20BY%20updated%20DESC",  # or build dynamic link
                    "action_id": "jiralink"
                }
            ]
        })
    elif assigned:
        blocks.append({
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text":
            f"🎉 Looks like you are not watching any Jira issues right now!\n\n"
            f"Stay awesome ✨ — or check in directly on your Jira board: 🔗 <https://{JIRA_DOMAIN}/jira/your-work|Open Jira>"
    }
})
    if not watching and not assigned:
        blocks.append({
    "type": "section",
    "text": {
        "type": "mrkdwn",
        "text": 
            f"🎉 *No assigned or watched issues found!*\n\n"
            f"You're all caught up — go grab a coffee ☕️ or get ahead with a new ticket.\n\n"
            f"Want to check your Jira board? 🔗 <https://{JIRA_DOMAIN}/jira/your-work|Open Jira>"
        
    },
    "accessory": {
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": "➕ Create Ticket",
            "emoji": True
        },
        "action_id": "create_ticket_button"
    }
})

    return blocks

async def fetch_assigned_issues(access_token, cloud_id, slack_user_id, client,http_client):
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    jql = "assignee = currentUser() and statusCategory != Done ORDER BY updated DESC"

    response = await http_client.get(url, headers=headers, params={
        "jql": jql,
        "fields": "summary,description,status,issuetype,assignee,priority",
        "maxResults": 5
    })
    if response.status_code != 200:
        print(f"❌ Failed to fetch assigned issues: {response.text}")
        return []
    issues = []
    for issue in response.json().get("issues", []):
        fields = issue["fields"]
        # Description (safe extraction from ADF)
        try:
            desc = fields.get("description", {})
            desc_text = desc.get("content", [{}])[0].get("content", [{}])[0].get("text", "")
        except Exception:
            desc_text = ""

        # Assignee Slack info
        assignee_display = "Unassigned"
        assignee_image = "https://cdn-icons-png.flaticon.com/512/149/149071.png"  # fallback
        assignee_id = None

        if fields.get("assignee"):
            assignee_display = fields["assignee"].get("displayName")
            assignee_image=fields["assignee"]["avatarUrls"]["48x48"]
        issues.append({
            "key": issue["key"],
            "summary": fields["summary"],
            "description": desc_text,
            "status": fields["status"]["name"],
            "type": fields["issuetype"]["name"],
            "type_icon": type_icon(fields["issuetype"]["name"]),
            "assignee": assignee_display,
            "assignee_pic": assignee_image,
            "assignee_id": assignee_id,
            "priority": fields["priority"]["name"] if fields.get("priority") else "N/A",
            "priority_icon": priority_emoji(fields.get("priority", {}).get("name", ""))
        })

    return issues

async def fetch_watching_issues(access_token, cloud_id, slack_user_id, client,http_client):
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    jql = "watcher = currentUser() and (assignee is EMPTY OR assignee != currentuser()) and statusCategory != Done ORDER BY updated DESC"

    response = await http_client.get(url, headers=headers, params={
        "jql": jql,
        "fields": "summary,description,status,issuetype,assignee,priority",
    })
    if response.status_code != 200:
        print(f"❌ Failed to fetch watched issues: {response.text}")
        return []

    issues = []
    for issue in response.json().get("issues", []):
        fields = issue["fields"]
        try:
            desc = fields.get("description", {})
            desc_text = desc.get("content", [{}])[0].get("content", [{}])[0].get("text", "")
        except Exception:
            desc_text = ""
        assignee_display = "Unassigned"
        assignee_image = "https://cdn-icons-png.flaticon.com/512/149/149071.png"
        assignee_id = None
        if fields.get("assignee"):
            assignee_display = fields["assignee"].get("displayName")
            assignee_image=fields["assignee"]["avatarUrls"]["48x48"]
        issues.append({
            "key": issue["key"],
            "summary": fields["summary"],
            "description": desc_text,
            "status": fields["status"]["name"],
            "type": fields["issuetype"]["name"],
            "type_icon": type_icon(fields["issuetype"]["name"]),
            "assignee": assignee_display,
            "assignee_pic": assignee_image,
            "assignee_id": assignee_id,
            "priority": fields["priority"]["name"] if fields.get("priority") else "N/A",
            "priority_icon": priority_emoji(fields.get("priority", {}).get("name", ""))
        })
    return issues

def ticket_block(issue):
    if not isinstance(issue, dict):
        print("❌ Invalid issue passed to ticket_block:", issue)
        return []
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<https://{JIRA_DOMAIN}/browse/{issue['key']}|{issue['key']}>* – {issue['summary']}\n"
                    f"{issue['description']}" 
                )
            },
            "accessory": {
                "type": "overflow",
                "action_id": f"overflow_menu_{issue['key']}",
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "🧠 Summarize"},
                        "value": f"summarize:{issue['key']}"
                    },
                    {
                        "text": {"type": "plain_text", "text": "🔁 Change Status"},
                        "value": f"change_status:{issue['key']}"
                    },
                    {
                        "text": {"type": "plain_text", "text": "🗨️ Comment"},
                        "value": f"comment:{issue['key']}"
                    },
                    {
                        "text": {"type": "plain_text", "text": "👤 Assign"},
                        "value": f"assign:{issue['key']}|{issue['assignee_id'] or ''}"
                    },
                    {
                        "text": {"type": "plain_text", "text": "👁️ Unwatch"},
                        "value": f"unwatch:{issue['key']}"
                    }
                ]
            }
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Status:* {issue['status']}"},
                {"type": "image", "image_url": issue["type_icon"], "alt_text": issue["type"]},
                {"type": "mrkdwn", "text": f"*Type:* {issue['type']}"},
                {"type": "image", "image_url": issue["assignee_pic"], "alt_text": issue["assignee"]},
                {"type": "mrkdwn", "text": f"*Assignee:* {issue['assignee']}"},
                {"type": "mrkdwn", "text": f"*Priority:* {issue['priority_icon']} {issue['priority']}"}
            ]
        },
        {"type": "divider"}
    ]

def priority_emoji(priority):
    return {
        "highest": "🚨",       
        "highest-p0": "🚨",
        "high": "🔴",             
        "high-p1": "🔴",
        "medium": "🟠",         
        "medium-p2": "🟠",
        "low": "🟢",               
        "low-p3": "🟢",
        "lowest": "⚪️"          
    }.get(priority.lower(), "❔")

def type_icon(type_name: str) -> str:
    url = f"https://product-integrations-cdn.atl-paas.net/jira-issuetype/{type_name.lower()}.png"
    return url

async def build_adf_comment(comment_text, slack_user_ids, client,access_token,cloud_id,http_client):
    tokens = []
    if slack_user_ids:
        for uid in slack_user_ids:
            try:
                token_info = await get_valid_jira_token(uid,http_client)
                if not token_info:
                    target_email=await resolve_user(uid,client,get_id="email")
                    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/user/search?query={target_email}"
                    headers = {
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json"
                    }
                    response = await http_client.get(url, headers=headers)
                    if response.status_code == 200:
                        user = response.json()
                        account_id= user[0]["accountId"]
                        display_name=user[0]["displayName"]
                    else:
                        print("❌ Error:", response.status_code, response.text)
                else:
                    account_id = token_info['account_id']
                    display_name = token_info['display_name']
                if account_id:
                    tokens.append({
                        "type": "mention",
                        "attrs": {
                            "id": account_id,
                            "text": f"@{display_name}",
                            "userType": "DEFAULT"
                        }
                    })
                    tokens.append({"type": "text", "text": " "}) 
            except Exception as e:
                print(f"❌ Failed to resolve mention for {uid}:", e)
    tokens.append({
        "type": "text",
        "text": comment_text
    })
    return {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": tokens
                }
            ]
        }
    }