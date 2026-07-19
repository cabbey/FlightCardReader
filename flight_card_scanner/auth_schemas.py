"""Pydantic request/response schemas for auth and user management endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CreateUserRequest(BaseModel):
    """Request body for creating a new user account."""

    email: str = Field(max_length=254)
    display_name: str = Field(max_length=100)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["admin", "data_entry"]


class UpdateUserRequest(BaseModel):
    """Request body for updating an existing user account.

    All fields are optional — only provided fields are updated.
    """

    display_name: str | None = Field(None, max_length=100)
    role: Literal["admin", "data_entry"] | None = None
    active: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=128)


class UserResponse(BaseModel):
    """Response schema for user data (never includes password)."""

    id: int
    email: str
    display_name: str
    role: str
    active: bool
    created_at: datetime
