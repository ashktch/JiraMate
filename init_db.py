from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import inspect
from handlers.jira_models import Base, engine

inspector = inspect(engine)
existing_tables = inspector.get_table_names()

if "jira_tokens" in existing_tables:
    print("⚠️ Table 'jira_tokens' already exists — skipping creation.")
else:
    Base.metadata.create_all(bind=engine)
    print("✅ Table 'jira_tokens' created.")
