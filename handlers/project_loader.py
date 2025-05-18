import json
import requests
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_API_USER = os.getenv("JIRA_API_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
auth = (JIRA_API_USER, JIRA_API_TOKEN)
headers = {"Accept": "application/json"}


def fetch_and_save_projects():
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/createmeta"
    file_path = Path("projects.json")
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)

    if file_path.exists():
        last_modified = datetime.fromtimestamp(file_path.stat().st_mtime, timezone.utc)
        if last_modified > one_day_ago:
            print("✅ Projects are up to date. Skipping fetch.")
            return

    print("🌐 Fetching Projects from Jira...")
    response = requests.get(url, headers=headers, auth=auth)
    if response.status_code != 200:
        print(f"❌ Failed to fetch projects: {response.status_code} {response.text}")
        return

    projects = response.json().get("projects", [])
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2)
    print(f"✅ Saved {len(projects)} projects.")
    run_parallel_field_fetch(projects)


def fetch_and_save_field(project_key, issue_type_id):
    try:
        field_url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
        field_resp = requests.get(field_url, headers=headers, auth=auth, timeout=20)

        if field_resp.status_code == 200:
            fields = field_resp.json().get("fields", [])
            if isinstance(fields, list):
                fields = {f["key"]: f for f in fields if "key" in f}
            path = Path(f"fields/{project_key}")
            path.mkdir(parents=True, exist_ok=True)
            with open(path / f"{issue_type_id}.json", "w", encoding="utf-8") as f:
                json.dump(fields, f, indent=2)
            return True
        else:
            print(f"⚠️ Failed {project_key}:{issue_type_id} → {field_resp.status_code}")
            return False

    except Exception as e:
        print(f"❌ Error fetching {project_key}:{issue_type_id} → {e}")
        return False


def run_parallel_field_fetch(projects, max_workers=10):
    tasks = []
    for project in projects:
        key = project.get("key")
        if not key:
            continue
        for issuetype in project.get("issuetypes", []):
            issue_id = issuetype.get("id")
            if issue_id:
                tasks.append((key, issue_id))

    print(f"🚀 Starting parallel field fetch for {len(tasks)} issue types...")
    saved_projects = set()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_and_save_field, key, issue_id): (key, issue_id)
            for key, issue_id in tasks
        }

        for future in as_completed(futures):
            key, issue_id = futures[future]
            try:
                success=future.result()
                if success and key not in saved_projects:
                    print(f"📁 Saved fields for project: {key}")
                    saved_projects.add(key)
            except Exception as e:
                print(f"❌ Exception in task {key}:{issue_id} → {e}")

    print("🎉 Parallel field fetch complete.")

def load_projects():
    file_path = Path("projects.json")

    def fetch_and_load():
        fetch_and_save_projects()
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    if not file_path.exists():
        print("📁 projects.json missing — triggering fetch...")
        projects = fetch_and_load()
    else:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                projects = json.load(f)
                if not isinstance(projects, list) or not projects:
                    print("⚠️ Empty or invalid projects.json — re-fetching...")
                    projects = fetch_and_load()
        except Exception as e:
            print(f"❌ Error loading projects.json: {e} — triggering fetch.")
            projects = fetch_and_load()

    # 🔎 Indexes
    project_index = {}
    issue_type_index = {}

    for p in projects:
        key = p.get("key")
        if not key:
            continue
        project_index[key] = p
        for it in p.get("issuetypes", []):
            issue_type_id = it.get("id")
            issue_type_name = it.get("name")
            if issue_type_id and issue_type_name:
                issue_type_index[f"{key}:{issue_type_id}"] = issue_type_name

    return projects, project_index, issue_type_index

