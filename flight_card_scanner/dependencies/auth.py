"""Role-based authorization dependency for FastAPI routes.

Provides a `require_role(min_role)` dependency factory that checks the
current user's role (resolved by the session middleware and attached to
`request.state.user`) against a minimum required role level.

Usage:
    from flight_card_scanner.dependencies.auth import Role, require_role

    @router.post("/api/admin/mode")
    async def set_mode(..., _=Depends(require_role(Role.DATA_ENTRY))):
        ...
"""

from enum import IntEnum

from fastapi import HTTPException, Request


class Role(IntEnum):
    """Access tiers in strict inclusive hierarchy: ADMIN > DATA_ENTRY > PUBLIC."""

    PUBLIC = 0
    DATA_ENTRY = 1
    ADMIN = 2


ROLE_MAP: dict[str, Role] = {
    "admin": Role.ADMIN,
    "data_entry": Role.DATA_ENTRY,
}


def require_role(min_role: Role):
    """FastAPI dependency factory for role-based access control.

    Returns a dependency function that:
    - Checks if the user is authenticated (attached to request.state.user
      by the session middleware).
    - If unauthenticated: redirects to /login?next=... for HTML requests,
      or raises 401 for API requests.
    - If authenticated but insufficient role: raises 403.
    - If authorized: returns the user object.

    Args:
        min_role: The minimum Role required to access the endpoint.

    Returns:
        An async FastAPI dependency function.
    """

    async def dependency(request: Request):
        user = getattr(request.state, "user", None)

        if user is None:
            # Unauthenticated
            if _is_api_request(request):
                raise HTTPException(
                    status_code=401,
                    detail="Authentication required",
                )
            else:
                next_url = str(request.url.path)
                raise HTTPException(
                    status_code=302,
                    headers={"Location": f"/login?next={next_url}"},
                )

        # Authenticated — check role level
        user_role = ROLE_MAP.get(user.role, Role.PUBLIC)
        if user_role < min_role:
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions",
            )

        return user

    return dependency


def _is_api_request(request: Request) -> bool:
    """Heuristic to distinguish API requests from HTML page requests.

    A request is considered an API request if:
    - The URL path starts with "/api/", OR
    - The Accept header contains "application/json"
    """
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept
