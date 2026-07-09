# Implementation Plan: Auth and Audit

## Overview

This plan implements session-based authentication, role-based authorization (admin / data_entry / public), and structured audit logging for the Flight Card Scanner application. The implementation integrates with the existing FastAPI + SQLAlchemy async + Jinja2 stack using a second SQLite database for auth data, Starlette middleware for session resolution, FastAPI dependencies for role enforcement, and Python's logging module for audit output.

## Tasks

- [x] 1. Configuration and foundation
  - [x] 1.1 Extend AppConfig with auth fields and update config loader
    - Add `auth_db_path`, `session_timeout_hours`, and `audit_log_path` fields to the `AppConfig` dataclass in `flight_card_scanner/config.py`
    - Add `effective_audit_log_path` property that defaults to `{event_data_path}/audit.log`
    - Update `load_config()` to parse and validate the new fields from config.json
    - Validate `session_timeout_hours` is a number in range [0.25, 8], default 8
    - Resolve `auth_db_path` relative to config dir (default `./auth.db`)
    - _Requirements: 7.1, 7.4, 7.5_

  - [x] 1.2 Add session secret validation to startup
    - Read `FCS_SESSION_SECRET` from environment in the lifespan startup
    - Refuse to start (log error + `sys.exit(1)`) if the variable is missing, empty, whitespace-only, or fewer than 16 characters
    - _Requirements: 2.5, 7.2, 7.3_

  - [x] 1.3 Write property test for configuration validation (Property 10)
    - **Property 10: Configuration Validation**
    - Generate invalid secrets (empty, short, whitespace) and invalid timeout values (non-numeric, out of range) and verify startup refusal
    - **Validates: Requirements 7.3, 7.4**

- [x] 2. Auth database and models
  - [x] 2.1 Create auth database module (`flight_card_scanner/auth_database.py`)
    - Implement `init_auth_engine(db_path)` creating a separate async engine and session factory
    - Implement `get_auth_db()` FastAPI dependency yielding an auth AsyncSession
    - Implement `create_auth_tables(engine)` to create the schema
    - Follow the pattern established in `database.py`
    - _Requirements: 1.1, 1.5_

  - [x] 2.2 Create auth models (`flight_card_scanner/auth_models.py`)
    - Define `AuthBase` declarative base (separate from event DB `Base`)
    - Define `User` model: id (PK autoincrement), email (unique, max 254), display_name (max 256), password_hash, role ("admin"|"data_entry"), active (default True), created_at
    - Define `Session` model: id (token_urlsafe(32) PK), user_id (FK), created_at, last_active, is_valid (default True), client_ip
    - Add indexes on sessions.user_id and sessions.last_active
    - _Requirements: 1.2, 1.5, 2.11_

- [x] 3. Auth service
  - [x] 3.1 Implement auth service (`flight_card_scanner/services/auth_service.py`)
    - Implement `create_user()`: hash password with argon2id, normalize email to lowercase, insert user
    - Implement `authenticate()`: verify credentials; always run argon2 verify (even for non-existent emails) to prevent timing attacks
    - Implement `create_session()`: generate token via `secrets.token_urlsafe(32)`, store with client_ip
    - Implement `validate_session()`: check idle expiry, Hard_Max_Lifetime (8h admin / 120h data_entry), IP binding (strict admin / soft data_entry), update last_active
    - Implement `invalidate_session()` and `invalidate_user_sessions()`
    - Implement rate limiting: in-memory dict, 5 attempts per 15-min sliding window, with `check_rate_limit()`, `record_failed_attempt()`, `reset_failed_attempts()`
    - _Requirements: 1.3, 1.4, 2.2, 2.8, 2.11, 2.12, 2.13, 8.1, 8.2, 8.3, 8.4, 8.5, 8.7, 8.8, 8.9_

  - [x] 3.2 Write property test for password hashing round-trip (Property 1)
    - **Property 1: Password Hashing Round-Trip**
    - Generate valid password strings (8-128 chars), hash with argon2id, verify original succeeds, verify different password fails
    - **Validates: Requirements 1.3, 8.1**

  - [x] 3.3 Write property test for case-insensitive email identity (Property 2)
    - **Property 2: Case-Insensitive Email Identity**
    - Generate email strings, verify case-variants are treated as same identity
    - **Validates: Requirements 1.4**

  - [x] 3.4 Write property test for rate limiting enforcement (Property 8)
    - **Property 8: Rate Limiting Enforcement**
    - Generate sequences of failed attempts, verify lockout after 5 within 15-min window, verify reset on success
    - **Validates: Requirements 8.2, 8.3, 8.7**

  - [x] 3.5 Write property test for admin strict IP binding (Property 11)
    - **Property 11: Admin Strict IP Binding**
    - Generate admin sessions with recorded IP, verify different IP invalidates session
    - **Validates: Requirements 8.8**

  - [x] 3.6 Write property test for data entry soft IP binding (Property 12)
    - **Property 12: Data Entry Soft IP Binding**
    - Generate data_entry sessions with IP changes, verify session survives and audit entry is written
    - **Validates: Requirements 8.9, 6.6**

  - [x] 3.7 Write property test for hard max lifetime enforcement (Property 13)
    - **Property 13: Hard Max Lifetime Enforcement**
    - Generate sessions at various ages per role, verify expiry at boundary (8h admin, 120h data_entry)
    - **Validates: Requirements 2.12, 2.13, 8.10**

- [x] 4. Audit service
  - [x] 4.1 Implement audit service (`flight_card_scanner/services/audit_service.py`)
    - Implement `init_audit_logger(log_path)` configuring a dedicated "audit" logger with FileHandler (append mode, no propagate)
    - Implement `log_action(actor, action, object_type, object_id, details)` writing JSON Lines
    - Ensure fire-and-forget: catch all exceptions, log to app logger, never block request
    - Never log plaintext passwords
    - _Requirements: 6.1, 6.2, 6.3, 6.7, 6.8, 6.9, 6.10, 8.6_

  - [x] 4.2 Write property test for audit log integrity (Property 7)
    - **Property 7: Audit Log Integrity**
    - Generate random actions, verify each produces exactly one parseable JSON line with valid ISO 8601 timestamp, correct actor/action/object_type/object_id, and no plaintext passwords
    - **Validates: Requirements 6.3, 6.4, 6.5, 6.6, 8.6**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Session middleware and role dependency
  - [x] 6.1 Implement session middleware (`flight_card_scanner/middleware/session_middleware.py`)
    - Create `SessionMiddleware` class (ASGI middleware)
    - Decode signed cookie using `itsdangerous`
    - Call `auth_service.validate_session()` with client_ip
    - Attach user (or None) to `request.state.user`
    - Schedule cookie clearing if session is invalid/expired
    - Set cookie attributes: HttpOnly, SameSite=Lax, Secure if SSL configured
    - _Requirements: 2.4, 2.6, 2.8, 2.9_

  - [x] 6.2 Implement role dependency (`flight_card_scanner/dependencies/auth.py`)
    - Define `Role` IntEnum (PUBLIC=0, DATA_ENTRY=1, ADMIN=2) and ROLE_MAP
    - Implement `require_role(min_role)` dependency factory
    - For unauthenticated: redirect to /login?next=... for HTML, 401 for API
    - For insufficient role: 403 for both HTML and API
    - Implement `_is_api_request()` heuristic (/api/ prefix or Accept: application/json)
    - _Requirements: 3.1, 3.7, 3.8, 3.9_

  - [x] 6.3 Write property test for role hierarchy access control (Property 4)
    - **Property 4: Role Hierarchy Access Control**
    - Generate (role, min_required_role) pairs, verify access is permitted iff user_role >= min_role
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**

- [x] 7. Auth router (login/logout/user management)
  - [x] 7.1 Create login/logout endpoints (`flight_card_scanner/routers/auth.py`)
    - Implement `GET /login` rendering login form template
    - Implement `POST /login`: rate limit check → authenticate → create session → set cookie → redirect (or render error)
    - Implement `GET /logout`: invalidate session → clear cookie → redirect to /login
    - Audit log login/logout/login_failed events
    - Generic error message on failure (no user enumeration)
    - _Requirements: 2.1, 2.2, 2.3, 2.7, 2.10, 6.5, 8.5_

  - [x] 7.2 Create user management API endpoints in auth router
    - Implement `GET /admin/users` (HTML page, admin only)
    - Implement `GET /api/admin/users` (JSON list, admin only)
    - Implement `POST /api/admin/users` (create user, admin only)
    - Implement `PUT /api/admin/users/{user_id}` (update user, admin only)
    - Reject self-demotion and self-deactivation
    - Invalidate all sessions on user deactivation
    - Handle duplicate email (409), user not found (404), validation errors (422)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10_

  - [x] 7.3 Write property test for user CRUD correctness (Property 5)
    - **Property 5: User CRUD Correctness**
    - Generate valid user creation requests, verify round-trip returns same email (lowercased), display_name, role, with non-plaintext password_hash and active=True
    - **Validates: Requirements 5.2, 5.3**

  - [x] 7.4 Write unit tests for session invalidation on deactivation (Property 6)
    - **Property 6: Session Invalidation on Deactivation**
    - Create user with N active sessions, deactivate user, verify all sessions invalidated
    - **Validates: Requirements 5.5**

  - [x] 7.5 Write unit tests for no user enumeration (Property 9)
    - **Property 9: No User Enumeration**
    - Verify identical response body for existing vs non-existing emails, and timing within 100ms
    - **Validates: Requirements 2.3, 8.5**

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Application wiring and startup integration
  - [x] 9.1 Wire auth into application lifespan (`flight_card_scanner/main.py`)
    - Initialize auth engine and create tables in lifespan startup
    - Initialize audit logger with configured path
    - Validate `FCS_SESSION_SECRET` and add session middleware to the app
    - Auto-create default admin from `FCS_ADMIN_EMAIL`/`FCS_ADMIN_PASSWORD` if no admin exists
    - Log warning if no admin exists and env vars are absent
    - Include the new auth router
    - _Requirements: 1.5, 1.6, 1.7, 2.5, 6.2_

  - [x] 9.2 Add `require_role()` dependencies to existing routers
    - Protect `scan.py` endpoints: `POST /api/scan` requires DATA_ENTRY
    - Protect `admin.py` endpoints: all mutating endpoints require DATA_ENTRY; `DELETE /api/admin/record/{id}` requires ADMIN
    - Keep `review.py` GET endpoints accessible to PUBLIC (list, detail, queue pages)
    - Keep `reports.py` GET endpoints accessible to PUBLIC
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 9.3 Add audit logging calls to existing routers
    - Audit record creation in `scan.py` (POST /api/scan)
    - Audit record updates in `admin.py` (PUT /api/admin/record/{id}) with old/new field changes
    - Audit record deletion in `admin.py` (DELETE /api/admin/record/{id})
    - Audit extraction triggers and requeue actions
    - _Requirements: 6.3, 6.4_

- [x] 10. Template modifications
  - [x] 10.1 Create login page template (`flight_card_scanner/templates/login.html`)
    - Email and password form with error display area
    - Rate limit message display with seconds remaining
    - Support `next` parameter for post-login redirect
    - _Requirements: 2.1_

  - [x] 10.2 Create user management page template (`flight_card_scanner/templates/users.html`)
    - List all users with email, display_name, role, active status
    - Forms for creating and editing users
    - _Requirements: 5.1_

  - [x] 10.3 Update base template with auth context
    - Add navigation bar: login link (unauthenticated) or display_name + logout link (authenticated)
    - Inject `current_user` into template context from `request.state.user`
    - _Requirements: 4.1, 4.4, 4.5_

  - [x] 10.4 Update existing templates with conditional rendering
    - Hide edit/delete/scan/queue/trigger/requeue/mode-switch elements for unauthenticated users
    - Hide delete and user management elements for data_entry users
    - Respect read_only mode: hide all mutating elements regardless of role
    - Use server-side exclusion (not CSS hiding)
    - _Requirements: 4.2, 4.3, 4.6, 4.7_

- [x] 11. Install new dependencies
  - [x] 11.1 Install argon2-cffi and itsdangerous packages
    - Run `.venv/bin/pip install argon2-cffi itsdangerous`
    - These are required for password hashing and cookie signing respectively
    - _Requirements: 1.3, 2.4_

- [-] 12. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Integration tests
  - [x] 13.1 Write integration tests for full auth flow
    - Test login → access protected page → logout flow
    - Test rate limiting through actual HTTP requests
    - Test read-only mode interaction with auth
    - Test middleware ordering (read_only runs before auth)
    - Test cookie attributes (HttpOnly, SameSite, Secure)
    - Test default admin creation at startup
    - _Requirements: 2.1, 2.2, 2.6, 2.7, 8.2, 1.6, 1.7_

  - [x] 13.2 Write integration tests for audit log output
    - Test audit log file written correctly across multiple actions
    - Verify JSON Lines format with correct fields
    - Verify no plaintext passwords in audit entries
    - _Requirements: 6.3, 6.4, 6.5, 6.7, 8.6_

  - [x] 13.3 Write property test for session lifecycle validity (Property 3)
    - **Property 3: Session Lifecycle Validity**
    - Generate sessions with varying activity patterns, verify validity within idle timeout AND Hard_Max_Lifetime, verify expiry after either limit
    - **Validates: Requirements 2.2, 2.7, 2.8, 2.9, 2.12, 2.13**

- [-] 14. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- Task 11 (dependency installation) should be run early but is listed after router implementation since the code won't execute until wiring is complete
- The audit service (task 4) is independent of the auth service and can be built in parallel
- All Python commands must use `.venv/bin/python` or `.venv/bin/pytest` per project conventions

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "11.1"] },
    { "id": 1, "tasks": ["1.3", "2.1", "2.2", "4.1"] },
    { "id": 2, "tasks": ["3.1", "4.2"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "6.1", "6.2"] },
    { "id": 4, "tasks": ["6.3", "7.1", "7.2"] },
    { "id": 5, "tasks": ["7.3", "7.4", "7.5", "9.1"] },
    { "id": 6, "tasks": ["9.2", "9.3", "10.1", "10.2"] },
    { "id": 7, "tasks": ["10.3", "10.4"] },
    { "id": 8, "tasks": ["13.1", "13.2", "13.3"] }
  ]
}
```
