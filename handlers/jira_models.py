from sqlalchemy import Column, String, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from cryptography.fernet import Fernet
import os

Base = declarative_base()

# Encryption setup
JIRA_TOKEN_SECRET = os.getenv("JIRA_TOKEN_SECRET")
fernet = Fernet(JIRA_TOKEN_SECRET.encode())

class JiraToken(Base):
    __tablename__ = "jira_tokens"
    slack_user_id = Column(String, primary_key=True)
    account_id=Column(String, nullable=False)
    display_name=Column(String, nullable=False)
    encrypted_token = Column(String, nullable=False)
    encrypted_refresh_token = Column(String, nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    cloud_id = Column(String, nullable=False)
    connected_at = Column(DateTime(timezone=True))

    def get_token(self):
        return fernet.decrypt(self.encrypted_token.encode()).decode()

    def set_token(self, token):
        self.encrypted_token = fernet.encrypt(token.encode()).decode()

    def get_refresh_token(self):
        return fernet.decrypt(self.encrypted_refresh_token.encode()).decode()

    def set_refresh_token(self, token):
        self.encrypted_refresh_token = fernet.encrypt(token.encode()).decode()

# DB setup
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
