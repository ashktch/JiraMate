# 🧠 JiraMate — Smart Slack Bot for Jira Ticketing

**JiraMate** is an intelligent Slack bot that empowers users to create Jira tickets _without leaving Slack_. It streamlines issue creation, avoids duplicates using AI-powered summaries of similar past tickets, and even lets users summarize tickets, attach files, assign teammates, and comment — all inside Slack.

> _"Don't just create tickets — create smart ones."_ 💡

---

## 🚀 Features

- 🔐 **Jira OAuth2 Login** via `/createticket` or home screen
- 🎟️ **Interactive Modal UI** to create tickets with custom fields
- 🧠 **AI-Powered Duplicate Detection** — prevent redundancy with summaries of similar tickets
- 🏠 **Home Tab Dashboard** showing assigned & watched issues
- 📎 **Slack File Upload Integration** — attach files to Jira issues directly from Slack
- 🧑‍🤝‍🧑 **Summarize, Assign, Comment, and Change Status** directly from Slack
- 🔁 **Refresh & Admin Commands** like `/refreshusers` and `/resetjira`
- 🤖 **Personal AI Agent in Slack DM** — Chat with JiraMate about tickets
- 🧠 Powered by **OpenAI GPT** for summaries and DM responses

---

## 📦 Project Structure

```
.
├── app.py                    # FastAPI + Slack Bolt app entry point
├── docker-compose.yml        # Docker setup for app + Redis + DB
├── Dockerfile                # Container definition
├── handlers/                 # All bot logic (modals, LLM, token handling)
│   ├── app_state.py          # Redis + GPT client setup
│   ├── jira_client.py        # Jira API logic (create, search, etc.)
│   ├── jira_token_store.py   # Token caching (Redis, Postgres, memory)
│   ├── jira_models.py        # Encrypted token DB models
│   ├── llm.py                # GPT-powered logic for summaries + DM chat agent
│   ├── modal_builder.py      # Slack modal UI generation
│   ├── userfetch.py          # Slack user resolution and caching
│   └── project_loader.py     # Loads and caches project metadata
├── init_db.py                # Initializes Database
├── fields/                   # Cached field metadata from Jira
├── templates/                # OAuth success and error pages
├── projects.json             # Jira project/issuetype list
└── requirements.txt          # Python dependencies
```

---

## ⚙️ Setup Instructions

### 1. Clone the Repo

```bash
git clone https://github.com/ashktch/jiramate.git
cd jiramate
```
### 2. Create a Slack App (via Manifest)

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps)
2. Click **"Create New App"** → From manifest
3. Select your workspace
4. Paste the contents of [`appmanifest.json`](./appmanifest.json) > _Make sure to replace all the links with your own endpoint._
5. After creating:
   - Note your `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET`
6. Install the app to your workspace

### 3. Create a `.env` File

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_ADMIN_USERS=U12345678,U98765432
ADMIN_LOG_CHANNEL=C12345678

JIRA_CLIENT_ID=...
JIRA_CLIENT_SECRET=...
JIRA_REDIRECT_URI=https://yourdomain.com/jira/oauth/callback
JIRA_DOMAIN=your-domain.atlassian.net

OPENAI_API_KEY=sk-...         # OR

REDIS_URL=redis://redis:6379
JIRA_TOKEN_SECRET=...         # A Fernet secret key (keep safe!)
DATABASE_URL=postgresql://user:pass@postgres:5432/jira_tokens
```

### 3. Run With Docker 🐳

```bash
docker-compose up --build -d
```

To watch logs:

```bash
docker logs -f JiraMate
```

---

## 💬 Slash Commands

| Command             | Description                                      |
| ------------------- | ------------------------------------------------ |
| `/createticket`     | Start ticket creation flow                       |
| `/summarize AT-123` | Summarize an existing Jira ticket using AI       |
| `/resetjira`        | 🔒 Admin only — clear all token DB & Redis cache |
| `/refreshusers`     | 🔄 Admin only — refresh Slack user cache         |
| `/jiratoken @user`  | 🔒 Admin — inspect Jira token info for a user    |

---

## 🧠 Personal AI Agent in Slack DM

- 💬 Talk to JiraMate directly in **Bot's DM**
- 🔎 Automatically pulls Jira ticket data based on your question
- 🧠 Powered by **OpenAI GPT** to summarize, assign, and suggest

> _E.g. "What about CP-15034?" or "Who is working on Indigo bugs?"_

---

## 🛠️ Development Tips

- Run locally with `uvicorn app:fastapi_app --reload`
- Use `ngrok` or `cloudflared` to expose your `/slack/events` endpoint
- Logs show project load and GPT activity clearly

---

## 🔐 Security Notes

- Tokens are **Fernet-encrypted** before storage
- OAuth tokens cached in **Redis + memory**

---

## 📄 License

This project is licensed under the [MIT License](./LICENSE).  
© 2025 ashktch. See the LICENSE file for details.
