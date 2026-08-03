"""Auth router — login/logout UI and user management API.

Endpoints:
- GET /login — render login form
- POST /login — authenticate user, create session, set cookie
- GET /logout — invalidate session, clear cookie, redirect
- GET /register — render registration form
- POST /register — create inactive user pending admin approval
- GET /admin/users — user management HTML page (admin only)
- GET /api/admin/users — list all users as JSON (admin only)
- POST /api/admin/users — create a new user (admin only)
- PUT /api/admin/users/{user_id} — update user fields (admin only)
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flight_card_scanner.auth_models import User
from flight_card_scanner.auth_schemas import (
    CreateUserRequest,
    UpdateUserRequest,
    UserResponse,
)
from flight_card_scanner.dependencies.auth import Role, require_role
from flight_card_scanner.services.audit_service import log_action
from flight_card_scanner.services.auth_service import AuthService

logger = logging.getLogger(__name__)


def _is_api_request(request: Request) -> bool:
    """Heuristic: /api/ prefix or Accept: application/json."""
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept

router = APIRouter()

# Module-level state set by configure()
_auth_service: AuthService | None = None
_templates: Jinja2Templates | None = None
_session_middleware = None  # SessionMiddleware instance (for cookie operations)


def configure(
    auth_service: AuthService,
    templates: Jinja2Templates,
    session_middleware=None,
) -> None:
    """Initialize the auth router with its dependencies.

    Called during app lifespan startup.

    Args:
        auth_service: The AuthService instance for user/session operations.
        templates: Jinja2Templates instance for rendering HTML pages.
        session_middleware: The SessionMiddleware instance for cookie signing.
    """
    global _auth_service, _templates, _session_middleware
    _auth_service = auth_service
    _templates = templates
    _session_middleware = session_middleware


def _get_auth_service() -> AuthService:
    """Get the configured auth service (raises if not configured)."""
    if _auth_service is None:
        raise RuntimeError("Auth router not configured. Call configure() first.")
    return _auth_service


# ---------------------------------------------------------------------------
# Login / Logout endpoints (task 7.1)
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None):
    """Render the login form."""
    if _templates is None:
        raise RuntimeError("Auth router not configured.")
    return _templates.TemplateResponse(
        name="login.html",
        request=request,
        context={
            "request": request,
            "next": next or "/",
            "error": None,
            "current_user": getattr(request.state, "user", None),
        },
    )


@router.post("/login")
async def login_submit(request: Request):
    """Authenticate user, create session, set cookie, redirect."""
    if _auth_service is None or _templates is None or _session_middleware is None:
        raise RuntimeError("Auth router not configured.")

    form = await request.form()
    email = form.get("email", "")
    password = form.get("password", "")
    next_url = form.get("next", "/") or "/"

    # 1. Check rate limit
    is_limited, seconds_remaining = _auth_service.check_rate_limit(email)
    if is_limited:
        if _is_api_request(request):
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many failed login attempts",
                    "retry_after": seconds_remaining,
                },
            )
        return _templates.TemplateResponse(
            name="login.html",
            request=request,
            context={
                "request": request,
                "next": next_url,
                "error": f"Too many failed attempts. Try again in {seconds_remaining} seconds.",
                "current_user": None,
            },
            status_code=429,
        )

    # 2. Authenticate
    user = await _auth_service.authenticate(email, password)

    if user is None:
        # 3. On failure: record attempt, audit log, render form with error
        _auth_service.record_failed_attempt(email)
        log_action(
            actor=email.lower().strip(),
            action="login_failed",
            object_type="session",
            object_id="",
            details={"result": "failed"},
        )
        return _templates.TemplateResponse(
            name="login.html",
            request=request,
            context={
                "request": request,
                "next": next_url,
                "error": "Invalid email or password. Please try again.",
                "current_user": None,
            },
            status_code=401,
        )

    # 4. On success: reset rate limit, create session, set cookie, audit log, redirect
    _auth_service.reset_failed_attempts(email)

    client_ip = request.client.host if request.client else None
    token = await _auth_service.create_session(user.id, client_ip=client_ip)

    log_action(
        actor=user.email,
        action="login",
        object_type="session",
        object_id=token[:8],  # Only log partial token for security
        details={"result": "success"},
    )

    # Sign the token for cookie
    signed_token = _session_middleware.sign_token(token)
    cookie_header = _session_middleware.build_set_cookie_header(signed_token)

    response = RedirectResponse(url=next_url, status_code=303)
    response.headers["Set-Cookie"] = cookie_header
    return response


@router.get("/logout")
async def logout(request: Request):
    """Invalidate session, clear cookie, redirect to /login."""
    if _auth_service is None or _session_middleware is None:
        raise RuntimeError("Auth router not configured.")

    token = getattr(request.state, "session_token", None)
    user = getattr(request.state, "user", None)

    if token:
        await _auth_service.invalidate_session(token)

    # Audit log the logout
    actor = user.email if user else "anonymous"
    log_action(
        actor=actor,
        action="logout",
        object_type="session",
        object_id=token[:8] if token else "",
        details={"result": "success"},
    )

    # Build a clear-cookie response
    response = RedirectResponse(url="/login", status_code=303)
    clear_header = _session_middleware._build_clear_cookie_header()
    response.headers["Set-Cookie"] = clear_header
    return response


# ---------------------------------------------------------------------------
# Registration endpoints
# ---------------------------------------------------------------------------

# Valid role values for registration
_REGISTRATION_ROLES = {"flyer", "data_entry"}

# Email validation regex (basic)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_password_strength(password: str) -> str | None:
    """Return an error message if password is too weak, or None if acceptable."""
    if len(password) < 12:
        return "Password must be at least 12 characters long."
    has_upper = bool(re.search(r"[A-Z]", password))
    has_lower = bool(re.search(r"[a-z]", password))
    has_digit = bool(re.search(r"[0-9]", password))
    has_special = bool(re.search(r"[^A-Za-z0-9]", password))
    if not (has_upper and has_lower and (has_digit or has_special)):
        return "Password must contain uppercase, lowercase, and a digit or special character."
    return None


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Render the registration form."""
    if _templates is None:
        raise RuntimeError("Auth router not configured.")
    return _templates.TemplateResponse(
        name="register.html",
        request=request,
        context={
            "request": request,
            "error": None,
            "form_email": "",
            "form_name": "",
            "form_reason": "",
            "form_role": "flyer",
            "current_user": getattr(request.state, "user", None),
        },
    )


@router.post("/register")
async def register_submit(request: Request):
    """Validate registration form and create inactive user."""
    if _auth_service is None or _templates is None:
        raise RuntimeError("Auth router not configured.")

    form = await request.form()
    email = form.get("email", "").strip()
    display_name = form.get("display_name", "").strip()
    password = form.get("password", "")
    confirm_password = form.get("confirm_password", "")
    reason = form.get("reason", "").strip()
    requested_role = form.get("requested_role", "").strip()

    def _render_error(error_msg: str):
        return _templates.TemplateResponse(
            name="register.html",
            request=request,
            context={
                "request": request,
                "error": error_msg,
                "form_email": email,
                "form_name": display_name,
                "form_reason": reason,
                "form_role": requested_role,
                "current_user": getattr(request.state, "user", None),
            },
            status_code=400,
        )

    # Server-side validation
    if not email or not _EMAIL_RE.match(email):
        return _render_error("A valid email address is required.")

    if not display_name:
        return _render_error("Name is required.")

    password_error = _validate_password_strength(password)
    if password_error:
        return _render_error(password_error)

    if password != confirm_password:
        return _render_error("Passwords do not match.")

    if not reason:
        return _render_error("Reason for requesting access is required.")

    if requested_role not in _REGISTRATION_ROLES:
        return _render_error("Please select a valid role.")

    # Create the user with active=False
    from argon2 import PasswordHasher

    hasher = PasswordHasher()
    normalized_email = email.lower().strip()
    password_hash = hasher.hash(password)

    async with _auth_service._session_factory() as db:
        # Check for duplicate email
        existing = await db.execute(
            select(User).where(User.email == normalized_email)
        )
        if existing.scalar_one_or_none() is not None:
            return _render_error("An account with this email already exists.")

        user = User(
            email=normalized_email,
            display_name=display_name,
            password_hash=password_hash,
            role=requested_role,
            active=False,
            reason=reason,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # --- Auto-approval: check known flier rosters ---
    auto_approved = False
    linked_name = None
    try:
        event_manager = getattr(getattr(request.app, "state", None), "event_manager", None)
        if event_manager is not None:
            from datetime import date as date_type

            from flight_card_scanner.services.flier_match_service import FlierMatchService

            today = date_type.today()
            events_dict = event_manager.events

            # Determine qualifying events:
            # 1. The most recently completed event (end < today, max end date)
            # 2. All future/current events (end >= today)
            most_recent_completed = None
            most_recent_end = None
            qualifying_events = []

            for _slug, info in events_dict.items():
                event_cfg = info.event_config
                if event_cfg.event_date_range is None:
                    continue
                end_date = event_cfg.event_date_range.end
                if end_date < today:
                    # Completed event - track the most recent one
                    if most_recent_end is None or end_date > most_recent_end:
                        most_recent_end = end_date
                        most_recent_completed = info
                else:
                    # Future or current event
                    qualifying_events.append(info)

            if most_recent_completed is not None:
                qualifying_events.append(most_recent_completed)

            # Check each qualifying event for email match
            for info in qualifying_events:
                event_cfg = info.event_config
                if not event_cfg.known_fliers_path:
                    continue
                if not event_cfg.known_fliers_path.exists():
                    continue

                svc = FlierMatchService(
                    known_fliers_path=event_cfg.known_fliers_path
                )
                svc.load()
                if not svc.enabled:
                    continue

                matched_row = svc.find_by_email(normalized_email)
                if matched_row is not None:
                    # Get the roster name from the matched row
                    linked_name = matched_row.get(svc._col_name, "").strip()
                    auto_approved = True
                    break

            if auto_approved and linked_name:
                async with _auth_service._session_factory() as db:
                    result = await db.execute(
                        select(User).where(User.email == normalized_email)
                    )
                    user_to_update = result.scalar_one_or_none()
                    if user_to_update is not None:
                        user_to_update.active = True
                        user_to_update.role = "flyer"
                        user_to_update.linked_flier_name = linked_name
                        await db.commit()

    except Exception:
        # If auto-approval fails for any reason, just skip it.
        # The user remains inactive pending admin approval.
        logger.debug("Auto-approval check failed, skipping", exc_info=True)

    log_action(
        actor=normalized_email,
        action="registered",
        object_type="user",
        object_id="",
        details={"role": requested_role, "reason": reason},
    )

    if auto_approved:
        log_action(
            actor=normalized_email,
            action="auto_approved",
            object_type="user",
            object_id="",
            details={"linked_flier_name": linked_name},
        )

    # Redirect to login with success message
    return RedirectResponse(
        url="/login?registered=1",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# User Management endpoints (task 7.2)
# ---------------------------------------------------------------------------


@router.get(
    "/admin",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def admin_dashboard(request: Request):
    """Render the admin dashboard page with extraction controls and management links."""
    if _templates is None:
        raise RuntimeError("Auth router not configured.")

    from flight_card_scanner.routers.admin import get_extraction_service

    extraction_service = get_extraction_service()
    current_mode = extraction_service.mode.value

    return _templates.TemplateResponse(
        name="main_admin.html",
        request=request,
        context={
            "request": request,
            "current_mode": current_mode,
            "current_user": getattr(request.state, "user", None),
        },
    )


@router.get(
    "/admin/users",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def users_page(request: Request):
    """Render the user management page listing all users."""
    if _templates is None or _auth_service is None:
        raise RuntimeError("Auth router not configured.")

    async with _auth_service._session_factory() as db:
        result = await db.execute(select(User).order_by(User.id))
        users = list(result.scalars().all())

    return _templates.TemplateResponse(
        name="users.html",
        request=request,
        context={
            "request": request,
            "users": users,
            "current_user": getattr(request.state, "user", None),
        },
    )


@router.get(
    "/api/admin/users",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def list_users():
    """Return all users as JSON."""
    auth_service = _get_auth_service()

    async with auth_service._session_factory() as db:
        result = await db.execute(select(User).order_by(User.id))
        users = list(result.scalars().all())

    return [
        UserResponse(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            active=u.active,
            created_at=u.created_at,
        )
        for u in users
    ]


@router.post(
    "/api/admin/users",
    dependencies=[Depends(require_role(Role.ADMIN))],
    status_code=201,
)
async def create_user(body: CreateUserRequest, request: Request):
    """Create a new user account.

    Returns 409 if the email is already in use.
    Returns 422 if validation fails (handled by Pydantic automatically).
    """
    auth_service = _get_auth_service()
    current_user = getattr(request.state, "user", None)

    try:
        user = await auth_service.create_user(
            email=body.email,
            display_name=body.display_name,
            password=body.password,
            role=body.role,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="A user with this email already exists.",
        )

    # Update the display name cache
    dns = getattr(request.app.state, "display_name_service", None)
    if dns:
        dns.update(user.email, user.display_name)

    # Audit log the user creation
    log_action(
        actor=current_user.email if current_user else "anonymous",
        action="created",
        object_type="user",
        object_id=user.id,
        details={
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
        },
    )

    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        active=user.active,
        created_at=user.created_at,
    )


@router.put(
    "/api/admin/users/{user_id}",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def update_user(user_id: int, body: UpdateUserRequest, request: Request):
    """Update user fields (display_name, role, active, password).

    - Rejects self-demotion (admin changing own role)
    - Rejects self-deactivation (admin deactivating themselves)
    - Invalidates all sessions when a user is deactivated
    - Returns 404 if user_id not found
    """
    auth_service = _get_auth_service()
    current_user = getattr(request.state, "user", None)

    # Self-modification checks
    if current_user and current_user.id == user_id:
        if body.role is not None and body.role != current_user.role:
            raise HTTPException(
                status_code=400,
                detail="Self-demotion is not allowed. You cannot change your own role.",
            )
        if body.active is not None and body.active is False:
            raise HTTPException(
                status_code=400,
                detail="Self-deactivation is not allowed. You cannot deactivate your own account.",
            )

    async with auth_service._session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if user is None:
            raise HTTPException(
                status_code=404,
                detail="User not found.",
            )

        # Track changes for audit log
        changes: dict = {}

        if body.display_name is not None and body.display_name != user.display_name:
            changes["display_name"] = {"old": user.display_name, "new": body.display_name}
            user.display_name = body.display_name

            # Update the display name cache
            dns = getattr(request.app.state, "display_name_service", None)
            if dns:
                dns.update(user.email, body.display_name)

        if body.role is not None and body.role != user.role:
            changes["role"] = {"old": user.role, "new": body.role}
            user.role = body.role

        if body.active is not None and body.active != user.active:
            changes["active"] = {"old": user.active, "new": body.active}
            user.active = body.active

        if body.password is not None:
            from argon2 import PasswordHasher
            hasher = PasswordHasher()
            user.password_hash = hasher.hash(body.password)
            changes["password"] = {"old": "***", "new": "***"}

        await db.commit()
        await db.refresh(user)

    # Invalidate all sessions if the user was deactivated
    if body.active is False and "active" in changes:
        await auth_service.invalidate_user_sessions(user_id)

    # Audit log the update
    if changes:
        action = "updated"
        # If the user was deactivated, use a more specific description in details
        if "active" in changes and not changes["active"]["new"]:
            action = "updated"

        log_action(
            actor=current_user.email if current_user else "anonymous",
            action=action,
            object_type="user",
            object_id=user.id,
            details={"changes": changes},
        )

    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        active=user.active,
        created_at=user.created_at,
    )
