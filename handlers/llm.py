import json,os,re
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
from handlers.app_state import gptclient,http_client
from handlers.jira_client import extract_comment_text

async def generate_and_update_summary(client, view_id, metadata_json,http_client,channel=None,ts=None):
    metadata = json.loads(metadata_json)
    issue_key = metadata["issue_key"]
    access_token = metadata["access_token"]
    cloud_id = metadata["cloud_id"]
    user_id = metadata["user_id"]

    issue_resp = await http_client.get(
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}",
        headers={"Authorization": f"Bearer {access_token}"}
    )

    if issue_resp.status_code != 200:
        summary_text = "❌ Failed to fetch issues/comments."
    else:
        issue_data = issue_resp.json()
        summary = issue_data["fields"]["summary"]
        description = ""
        desc_field = issue_data["fields"].get("description")
        if desc_field and isinstance(desc_field.get("content"), list):
            for block in desc_field["content"]:
                for item in block.get("content", []):
                    if "text" in item:
                        description += item["text"] + " "
        description = description.strip() or "No description available."
        comments_data = issue_data["fields"].get("comment", {}).get("comments", [])
        comments_text = "\n".join(
            f"- {c.get('author', {}).get('displayName', 'Someone')}: {extract_comment_text(c)}"
            for c in comments_data
        )
        prompt = f"""
Jira Issue:
- Title: `{summary}`
- Description: `{description or "N/A"}`
- Comments:{comments_text}
"""
        try:
            response = await gptclient.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """
            You are a Slack-integrated Jira bot. Format your output consistently.

- Use Slack-compatible markdown only.
- The first line must always wrap title and description with ` `: `<title> - <description>`
- Write 3–5 lines of summary.
- Then end with: `↳ _Suggested Resolution:_ <recommendation/suggestion>`
            """
                    },
                    {
                        "role": "user",
                        "content": prompt}
                ],
                temperature=0.5,
                max_tokens=1000
            )
            summary_text=response.choices[0].message.content.strip()
        except Exception as e:
            summary_text = f"❌ Summary generation failed: {e}"

    # Update modal with summary
    try:
        if view_id == "slash-command-view":
            await client.chat_postMessage(
                channel=channel,
                ts=ts,
                text=f"📋 Summary for *<https://{JIRA_DOMAIN}/browse/{issue_key}|{issue_key}>*:\n{summary_text}"
            )
        else:
            await client.views_update(
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": f"Summary for {issue_key}"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"{summary_text}"}
                        }
                    ]
                }
            )
    except Exception as e:
        if "not_found" in str(e):
            print("⚠️ Summary modal was closed before update. Skipping update.")
        else:
            raise

def gptprompt(current_title, current_description, past_issues):
    prompt = f"""
### Current Ticket
- Title: {current_title}
- Description: {current_description or "No description provided."}

### Related Past Tickets (via JQL):
"""

    for issue in past_issues:
        prompt += f"""
---
Ticket: {issue['key']}
Summary: {issue.get('summary', 'No summary')}
Description: {issue.get('description', 'No description')}
Status: {issue.get('status', 'Unknown')}
Comment: {issue.get('last_comment', 'No recent comments')}
"""

    return prompt.strip()

async def analyze_user_query_and_respond(user_input: str, access_token: str, cloud_id: str):
    jql_prompt = f"""
User query:
"{user_input}"
"""

    jql_resp = await gptclient.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": """
    You are JiraMate — a smart assistant trained to interpret natural language questions and generate precise JQL (Jira Query Language) queries for use with Jira Cloud.
Your task is to: Convert the user query into a JQL that GPT-4o will use to fetch and answer, If the user doesnt ask Jira Related Question, keep the jql empty.
If the user query references a specific issue key like `proj-123`, `ABC-42`, or any `Abc-###` pattern, you must use `issue = ABC-123`.\n"
### Output Format
Respond ONLY with a valid JSON object, like this:
{
  "jql": "your JQL string here",
  "explanation": "A human-readable summary of what the query does."
}
- ⚠️ Do NOT return anything else (no markdown, no commentary, no code blocks).
- ⚠️ Your output MUST be a valid JSON object that can be parsed.
- ⚠️ Use escaped double quotes and avoid newlines inside JSON.


    """
                    },
                    {
                        "role": "user",
                        "content": jql_prompt}
                ],
        temperature=0.4,
        max_tokens=400
    )
    try:
        content = jql_resp.choices[0].message.content.strip()
        parsed = json.loads(content)
    except Exception as e:
        print(f"GPT JQL parse error: {e}")
        return [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": "❌ I couldn’t understand your request. Please rephrase or try again."}
        }]               
    jql = parsed.get("jql", "")
    explanation = parsed.get("explanation", "")
    if not jql:
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "⚠️ I couldn’t convert your request into a Jira search, Please ask *Jira Related Questions* only!"}
            }
        ]

    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    params = {
        "jql": jql,
        "fields": "summary,status,description,comment,assignee,priority,issuetype,created,updated,project",
        "maxResults": 5
    }
    resp = await http_client.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"❌ Sorry, I couldn't process your Jira search. Please check your request or try rephrasing it."}
            }
        ]
    issues = resp.json().get("issues", [])
    if not issues:
        return [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🧐 {explanation}\n\nNo matching issues found."}
            }
        ]
    formatted = ""
    for issue in issues:
        key = issue["key"]
        fields = issue["fields"]
        # Initialize individual values
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name")
        assignee_data = fields.get("assignee")
        assignee = assignee_data.get("displayName") if assignee_data else None
        priority = fields.get("priority", {}).get("name")
        created = fields.get("created", "")
        updated = fields.get("updated", "")
        issuetype = fields.get("issuetype", {}).get("name")
        project = fields.get("project", {}).get("name")
        # Extract description
        description = ""
        desc_field = fields.get("description", "")
        if desc_field and isinstance(desc_field.get("content"), list):
            for block in desc_field["content"]:
                for item in block.get("content", []):
                    if "text" in item:
                        description += item["text"] + " "
        description = description.strip()
        # Extract comments
        comments_data = fields.get("comment", {}).get("comments", [])
        comments_text = "\n".join(
            f"- {c.get('author', {}).get('displayName', 'Someone')}: {extract_comment_text(c)}"
            for c in comments_data
        )
        # Start formatting
        formatted += f"\n<{JIRA_DOMAIN}/browse/{key}|*{key}*>"
        if summary:
            formatted += f": {summary}"
        if project:
            formatted += f"\n• *Project:* {project}"
        status_line = []
        if status:
            status_line.append(f"*Status:* {status}")
        if issuetype:
            status_line.append(f"*Type:* {issuetype}")
        if priority:
            status_line.append(f"*Priority:* {priority}")
        if status_line:
            formatted += f"\n• {' | '.join(status_line)}"
        if assignee:
            formatted += f"\n• *Assignee:* {assignee}"
        if created or updated:
            date_parts = []
            if created:
                date_parts.append(f"*Created:* {created[:10]}")
            if updated:
                date_parts.append(f"*Updated:* {updated[:10]}")
            formatted += f"\n• {' | '.join(date_parts)}"
        if description:
            formatted += f"\n• *Description:* {description}"
        else:
            formatted += "\n• *Description:* No description available."
        if comments_text:
            formatted += f"\n• *Comments:*\n{comments_text}"
        formatted += "\n" 
    summary_prompt = f"""
Ticket Data:
{formatted}
--- 
{user_input}
The ticket data is from the user query.
Give response in Markdown format NOT Slack Markdown.
"""

    summary_resp = await gptclient.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": """
    You are JiraMate — a smart Jira assistant integrated into Slack.
Your role is to answer the user's Jira-related question using the provided ticket data. Do not summarize unless the summary is necessary to respond to the specific question.
Your response must follow these interaction rules:
---
## 🎯 RESPONSE STRATEGY
    -First, understand the user's question — whether it's asking for status, ownership, history, recommendations, or your opinion.
    -Answer the question directly and specifically based on the ticket data.
    -If information is missing or unclear, explain that clearly and suggest a logical next step.
    - If they ask about **status**, provide only the status.
    - If they ask **who**, **when**, or **what**, respond to exactly that.
    - If no data is found, explain clearly and suggest next steps.
    - Avoid vague intros like “Here’s what I found” unless it adds value.
    - NEVER return JSON, code blocks, or raw technical output.
    - Write in a friendly, professional tone that fits inside Slack.
    - Strictly Respond in markdown only!
    """
                    },
                    {
                        "role": "user",
                        "content": summary_prompt}
                ],
        temperature=0.6,
        max_tokens=1000
    )

    message = summary_resp.choices[0].message.content.strip()
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": markdown_to_slack(message)}
        }
    ]

def markdown_to_slack(text):
    if not text:
        return ""
    # Convert bold: **text** or __text__ → *text*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.*?)__", r"*\1*", text)
    # Convert italics: *text* or _text_ → _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?<!\*)", r"_\1_", text)  # avoid bold conflict
    text = re.sub(r"_(.*?)_", r"_\1_", text)
    # Convert strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.*?)~~", r"~\1~", text)
    # Convert inline code: `code` → `code`
    text = re.sub(r"`(.*?)`", r"`\1`", text)
    # Convert markdown links [text](url) → <url|text>
    def replace_md_links(match):
        label, url = match.groups()
        return f"<{url}|{label}>"
    text = re.sub(r"\[(.*?)\]\((.*?)\)", replace_md_links, text)
    # Fix malformed Slack link formats
    # Remove broken double brackets <<...|...>>
    text = re.sub(r"<<(.*?)\|(.*?)>>", r"<\1|\2>", text)
    # Remove duplicated Slack-style pipe: <url|label>|url → <url|label>
    text = re.sub(r"<([^|>]+)\|[^>]+>\|\1", r"<\1|\1>", text)
    # Optional: strip nested bolds inside Slack links
    text = re.sub(r"<([^|]+)\|\*(.*?)\*>", r"<\1|\2>", text)
    # Remove unsupported markdown like headings and code blocks
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```.*?```", "[code block removed]", text, flags=re.DOTALL)
    return text.strip()

