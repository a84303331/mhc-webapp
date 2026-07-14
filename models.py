"""MHC WebApp — SQLAlchemy Models

使用者、設定、每日計數、反饋、Refresh Token。
"""

import uuid
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date,
    ForeignKey, CheckConstraint, Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


def generate_token() -> str:
    """產生 UUID token（驗證信、密碼重設用）"""
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    email_verified = Column(Boolean, default=False)
    email_verify_token = Column(String(64), default=generate_token)
    email_verify_token_expires = Column(DateTime, default=lambda: datetime.utcnow())  # 註冊時設 24h
    password_reset_token = Column(String(64), nullable=True)
    password_reset_token_expires = Column(DateTime, nullable=True)
    refresh_token = Column(String(64), nullable=True)  # JWT refresh token
    daily_limit = Column(Integer, default=3)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    # 關聯
    daily_counts = relationship("DailyQuestionCount", back_populates="user")
    feedbacks = relationship("Feedback", back_populates="user")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class DailyQuestionCount(Base):
    __tablename__ = "daily_question_counts"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    date = Column(Date, default=date.today, primary_key=True)
    count = Column(Integer, default=0, nullable=False)
    last_question_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="daily_counts")


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(100), nullable=False)  # 案例檔名（無副檔名）
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    insight = Column(Integer, CheckConstraint("insight BETWEEN 1 AND 5"))
    clarity = Column(Integer, CheckConstraint("clarity BETWEEN 1 AND 5"))
    actionability = Column(Integer, CheckConstraint("actionability BETWEEN 1 AND 5"))
    overall = Column(Integer, CheckConstraint("overall BETWEEN 1 AND 5"))
    reuse_intent = Column(Integer, CheckConstraint("reuse_intent BETWEEN 1 AND 5"))

    submitted_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="feedbacks")
