from handlers.project_loader import load_projects
import json
from handlers.llm import generate_and_update_summary
from handlers.app_state import redis_client


projects,projects_index,issue_type_index=load_projects()
def plain_text(text):
    return {"type": "plain_text", "text": text}
def project_options():
    options = [
        {
            "text": plain_text(f"{p['name']} ({p['key']})"),
            "value": p["key"]
        }
        for p in projects
    ]
    return options

def issue_options(selected_project_key):
    project = projects_index.get(selected_project_key)
    if not project:
        raise Exception("Project not found!")
    options = [
        {
            "text": plain_text(issue_type["name"]),
            "value": issue_type["id"]
        }
        for issue_type in project["issuetypes"]
        if not issue_type.get("subtask")
    ]
    return options

def build_project_selection_modal():
    return {
    "type": "modal",
    "callback_id": "select_project_modal",
    "title": plain_text("Create Jira Ticket"),
    "close": plain_text("Cancel"),
    "blocks": [
        {
            "type": "section",
            "block_id": "project_block",
            "text": {
                "type": "mrkdwn",
                "text": "*Project:*"
            },
            "accessory": {
                "type": "static_select",
                "action_id": "project_selected",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select a project"
                },
                "options": project_options()
            }
        },
        {
            "type": "section",
            "block_id": "issue_block",
            "text": {
                "type": "mrkdwn",
                "text": "*Work Type:*"
            },
            "accessory": {
                "type": "static_select",
                "action_id": "issue_selected",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select work type"
                },
                "options": [
                    {
                        "text": plain_text("Select Work Type"),
                        "value": "worktype"
                    }
                ]
            }
        }
    ]
}

def build_issue_type_modal(selected_project_key):
    project=projects_index.get(selected_project_key)
    project_name = project["name"]
    return {
    "type": "modal",
    "callback_id": "select_project_modal",
    "private_metadata": json.dumps({
            "project_key": selected_project_key,
            "project_name": project_name
        }),
    "title": plain_text("Create Jira Ticket"),
    "close": plain_text("Cancel"),
    "blocks": [
        {
            "type": "section",
            "block_id": "project_block",
            "text": {
                "type": "mrkdwn",
                "text": "*Project:*"
            },
            "accessory": {
                "type": "static_select",
                "action_id": "project_selected",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select a project"
                },
                "options": project_options(),
                "initial_option": next(
            (opt for opt in project_options() if opt["value"] == selected_project_key),
            None)
            }
        },
        {
            "type": "section",
            "block_id": "issue_block",
            "text": {
                "type": "mrkdwn",
                "text": "*Work Type:*"
            },
            "accessory": {
                "type": "static_select",
                "action_id": "issue_selected",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select work type"
                },
                "options": issue_options(selected_project_key)
            }
        }
    ]
}

async def build_ticket_fields_modal(fields, project_key, project_name, issue_type_id):
    issue_name = issue_type_index.get(f"{project_key}:{issue_type_id}")
    SKIPPED_FIELDS = {"project", "issuetype", "summary", "priority", "project_block", "issue_block","reporter"}
    project_opts = project_options()
    issue_opts = issue_options(project_key)

    blocks = [
        {
            "type": "section",
            "block_id": "project_block",
            "text": {"type": "mrkdwn", "text": "*Project:*"},
            "accessory": {
                "type": "static_select",
                "action_id": "project_selected",
                "placeholder": plain_text("Select a project"),
                "options": project_opts,
                "initial_option": next((opt for opt in project_opts if opt["value"] == project_key), None)
            }
        },
        {
            "type": "section",
            "block_id": "issue_block",
            "text": {"type": "mrkdwn", "text": "*Work Type:*"},
            "accessory": {
                "type": "static_select",
                "action_id": "issue_selected",
                "placeholder": plain_text("Select work type"),
                "options": issue_opts,
                "initial_option": issue_opts[0] if issue_opts else None
            }
        },
        {
            "type": "input",
            "block_id": "summary",
            "element": {
                "type": "plain_text_input",
                "action_id": "input_value",
                "placeholder": plain_text("Enter the ticket title")
            },
            "label": plain_text("Title / Summary")
        },
        {
            "type": "input",
            "block_id": "description",
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": "input_value",
                "multiline": True,
                "placeholder": plain_text("Enter a description (optional)")
            },
            "label": plain_text("Description")
        },
        {
                "type": "input",
                "block_id": f"assignee|{project_key}",
                "optional": True,
                "element": {
                    "type": "external_select",
                    "action_id": "assignee",
                    "placeholder": plain_text("Search for a user"),
                    "min_query_length": 3,
                    "initial_option":{"text": {"type": "plain_text", "text": "Unassigned"}, "value": "null"}
                },
                "label": plain_text("Assignee") 
        },
        {
            "type": "input",
            "block_id": "priority",
            "element": {
                "type": "static_select",
                "action_id": "input_value",
                "options": [
                    {"text": plain_text("Highest-P0"), "value": "1"},
                    {"text": plain_text("High-P1"), "value": "2"},
                    {"text": plain_text("Medium-P2"), "value": "3"},
                    {"text": plain_text("Low-P3"), "value": "4"}
                ],
                "initial_option":{"text": plain_text("Medium-P2"), "value": "3"},
                "placeholder": plain_text("Select Priority")
            },
            "label": plain_text("Priority")
        }
    ]

    for field_key, field in fields.items():
        if field_key in SKIPPED_FIELDS:
            continue
        if not field.get("required", False):
            continue
        field_name = field.get("name", field_key)
        schema = field.get("schema", {})
        field_type = schema.get("type")
        custom_type = schema.get("custom")
        allowed_values = field.get("allowedValues", [])

        # Multi-select
        if field_type == "array" and allowed_values:
            if len(allowed_values) > 100:
                cache_key = f"external_fields:{field_key}"
                exists = await redis_client.exists(cache_key)
                if not exists:
                    await redis_client.set(cache_key, json.dumps(allowed_values), ex=3600)
                blocks.append({
                    "type": "input",
                    "block_id": field_key,
                    "element": {
                        "type": "multi_external_select",
                        "action_id": "input_value",
                        "min_query_length": 0,
                        "placeholder": plain_text("Search and select")
                    },
                    "label": plain_text(field_name)
                })
            else:
                blocks.append({
                    "type": "input",
                    "block_id": field_key,
                    "element": {
                        "type": "multi_static_select",
                        "action_id": "input_value",
                        "options": [
                            {
                                "text": plain_text(opt.get("name") or opt.get("value")),
                                "value": opt.get("id") or opt.get("value")
                            } for opt in allowed_values
                        ]
                    },
                    "label": plain_text(field_name)
                })

        # Single select
        elif field_type == "option" and allowed_values:
            blocks.append({
                "type": "input",
                "block_id": field_key,
                "element": {
                    "type": "static_select",
                    "action_id": "input_value",
                    "options": [
                        {
                            "text": plain_text(opt.get("value", "")),
                            "value": str(opt.get("id"))
                        } for opt in allowed_values
                    ]
                },
                "label": plain_text(field_name)
            })

        # Text fields
        elif field_type == "string":
            multiline = custom_type == "com.atlassian.jira.plugin.system.customfieldtypes:textarea"
            blocks.append({
                "type": "input",
                "block_id": field_key,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "input_value",
                    "multiline": multiline
                },
                "label": plain_text(field_name)
            })

        # Date picker
        elif field_type == "datetime" or field_type == "date":
            blocks.append({
                "type": "input",
                "block_id": field_key,
                "element": {
                    "type": "datepicker",
                    "action_id": "input_value",
                    "placeholder": plain_text("Select date")
                },
                "label": plain_text(field_name)
            })
        elif "autoCompleteUrl" in field and field_type=="assignee":
            blocks.append({
                "type": "input",
                "block_id": f"field_key|{project_key}",
                "element": {
                    "type": "external_select",
                    "action_id": "input_value",
                    "placeholder": plain_text("Search for a user"),
                    "min_query_length": 1
                },
                "label": plain_text(field_name)
            })
        # Fallback
        else:
            blocks.append({
                "type": "input",
                "block_id": field_key,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "input_value",
                    "placeholder": plain_text(f"Enter {field_name}")
                },
                "label": plain_text(f"{field_name} (untyped)")
            })

    return {
        "type": "modal",
        "callback_id": "submit_ticket_modal",
        "private_metadata": json.dumps({
            "project_key": project_key,
            "project_name": project_name,
            "issue_type_id": issue_type_id,
            "issue_name": issue_name
        }),
        "title": plain_text("Create Jira Ticket"),
        "submit": plain_text("Submit/Search"),
        "close": plain_text("Cancel"),
        "blocks": blocks
    }



async def open_status_modal(client, trigger_id, issue_key, user_id,access_token,cloud_id,http_client):
    # Fetch available transitions
    response =await http_client.get(
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue_key}/transitions",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
    )
    if response.status_code != 200:
        print(f"❌ Failed to fetch transitions for {issue_key}")
        return

    transitions = response.json().get("transitions", [])
    if not transitions:
        print(f"⚠️ No transitions found for {issue_key}")
        return
    options = [
        {"text": {"type": "plain_text", "text": t["name"]}, "value": t["id"]}
        for t in transitions
    ]

    # Open the modal with dropdown
    await client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "submit_status_update",
            "private_metadata": json.dumps({
                    "issue_key": issue_key,
                    "cloud_id": cloud_id,
                    "token": access_token}),
            "title": {"type": "plain_text", "text": "Change Status"},
            "submit": {"type": "plain_text", "text": "Update"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "status_block",
                    "element": {
                        "type": "static_select",
                        "action_id": "selected_status",
                        "placeholder": {"type": "plain_text", "text": "Choose a status"},
                        "options": options
                    },
                    "label": {"type": "plain_text", "text": "New Status"}
                }
            ]
        }
    )

async def open_assign_modal(client, trigger_id, issue_key, user_id,access_token,cloud_id,current_assignee_id):
    assignee_id=current_assignee_id
    await client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "submit_assignee_update",
            "private_metadata": json.dumps({
                            "issue_key": issue_key,
                            "cloud_id": cloud_id,
                            "access_token": access_token,
                            "current_assignee": assignee_id
                        }),
            "title": {"type": "plain_text", "text": "Assign Ticket"},
            "submit": {"type": "plain_text", "text": "Assign"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "assignee_block",
                    "element": {
                        "type": "external_select",
                        "action_id": "selected_assignee",
                        "min_query_length": 0,
                        "placeholder": {"type": "plain_text", "text": "Choose a user"}
                    },
                    "label": {"type": "plain_text", "text": "Assignee"}
                }
            ]
        }
    )

async def open_comment_modal(client, trigger_id, issue_key, user_id,access_token,cloud_id):
    modal = {
        "type": "modal",
        "callback_id": "submit_comment_modal",
        "private_metadata": json.dumps({
            "issue_key": issue_key,
            "cloud_id": cloud_id,
            "access_token": access_token,
        }),
        "title": {"type": "plain_text", "text": "Add Comment"},
        "submit": {"type": "plain_text", "text": "Add"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "mention_block",
                "optional":True,
                "element": {
                    "type": "multi_external_select",
                    "action_id": "mentions",
                    "min_query_length": 0,
                    "placeholder": plain_text("Mention user(s)")
                },
                "label": {
                    "type": "plain_text",
                    "text": "Mentions"
                }
            },
            {
                "type": "input",
                "block_id": "comment_block",
                "label": {"type": "plain_text", "text": "Your Comment"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "comment_input",
                    "multiline": True
                }
            },
            
        ]
    }
    await client.views_open(trigger_id=trigger_id, view=modal)

async def open_summary_modal(client, trigger_id, issue_key, user_id,access_token,cloud_id,http_client):
    metadata = json.dumps({
        "issue_key": issue_key,
        "access_token": access_token,
        "cloud_id": cloud_id,
        "user_id": user_id
    })
    result=await client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "summarize_modal",
            "private_metadata": metadata,
            "title": {"type": "plain_text", "text": "Summarizing Issue"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 Fetching summary for *{issue_key}*..."}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Please wait while JiraMate analyzes the issue and its comments."}
                ]}
            ]
        }
    )
    view_id = result["view"]["id"]
    await generate_and_update_summary(client, view_id, metadata,http_client)
