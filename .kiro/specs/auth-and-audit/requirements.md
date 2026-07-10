# Requirements Document

## Introduction

This feature adds authentication, role-based authorization, and audit logging to the Flight Card Scanner application. The system will support three user tiers (admin, data_entry, public/unauthenticated) with access controls enforced on both HTML pages and API endpoints. User credentials are stored in a separate SQLite database (independent of the per-event data database) to allow reuse across events. All mutating actions are recorded in a structured audit log on disk.

### Research Summary: Auth Approach for This Stack

Based on research into current best practices for FastAPI + Jinja2 server-rendered applications:

- **Session-based authentication with signed HTTP-only cookies** is the recommended approach for this stack. Unlike JWT, sessions work naturally with server-rendered HTML (no client-side token management needed), and Starlette's middleware provides signed cookie support via `itsdangerous`. Session data stays server-side (in the auth database), with only a signed session ID in the cookie.
- **Argon2id** is the OWASP-recommended password hashing algorithm (2026 default). It is memory-hard, resistant to GPU attacks, and available via the `argon2-cffi` Python package. Bcrypt is an acceptable fallback.
- **Simple role-based dependencies** in FastAPI are sufficient for 3 fixed roles — no need for a heavyweight RBAC library. A `require_role(min_role)` dependency pattern cleanly maps the tier hierarchy.
- **Separate SQLite database** is well-supported by SQLAlchemy async — just a second engine/session factory initialized at startup alongside the existing event database.

## Glossary

- **Auth_System**: The authentication and authorization subsystem, including session management, password verification, and role enforcement.
- **Auth_Database**: A SQLite database file separate from the event database, storing user accounts, hashed passwords, roles, and session records.
- **Audit_Logger**: A subsystem that writes structured action logs to a dedicated file on disk, independent of web server access logs.
- **User**: A registered account in the Auth_Database with an email address, hashed password, and assigned role.
- **Role**: One of three access tiers — `admin`, `data_entry`, or `public` (unauthenticated).
- **Session**: A server-side record associating a signed cookie token with an authenticated user, stored in the Auth_Database. Each session records the client IP address at creation time.
- **Actor**: The user (identified by email) who performed an action, or "anonymous" for unauthenticated access.
- **Hard_Max_Lifetime**: A fixed, non-configurable maximum duration for a session measured from creation time, regardless of activity. Differs by role: 8 hours for admin, 120 hours (5 days) for data_entry.
- **IP_Binding**: A policy that ties a session to the client IP address recorded at session creation. Strict binding (admin) invalidates the session on IP change; soft binding (data_entry) logs the change but keeps the session valid.

## Requirements

### Requirement 1: User Account Storage

**User Story:** As an admin, I want user accounts stored in a separate database from event data, so that user credentials persist across event database rotations.

#### Acceptance Criteria

1. THE Auth_System SHALL store user accounts in the Auth_Database, a SQLite file whose path is specified by the `auth_db_path` key in config.json, resolved relative to the config file directory (consistent with existing path resolution), independent of the event_data_path.
2. THE Auth_Database SHALL store for each User: email address (unique, case-insensitive, maximum 254 characters), display name (maximum 256 characters), hashed password, role (one of: "admin", "data_entry"), created_at timestamp, and active status (boolean, default true).
3. THE Auth_System SHALL hash passwords using Argon2id via the `argon2-cffi` library with default parameters.
4. THE Auth_System SHALL treat email addresses as case-insensitive for uniqueness and login matching by comparing the lowercase form of the address.
5. WHEN the application starts and the Auth_Database file does not exist, THE Auth_System SHALL create the database and its schema automatically.
6. WHEN no user with role "admin" exists in the Auth_Database at startup and both environment variables `FCS_ADMIN_EMAIL` and `FCS_ADMIN_PASSWORD` are present and non-empty, THE Auth_System SHALL create a default admin account with role "admin" and active status true using those credentials.
7. IF either `FCS_ADMIN_EMAIL` or `FCS_ADMIN_PASSWORD` is absent or empty and no user with role "admin" exists, THEN THE Auth_System SHALL log a warning indicating that no default admin was created and continue startup without creating a default admin.

### Requirement 2: Session-Based Authentication

**User Story:** As a user, I want to log in with my email and password, so that the system recognizes me across requests.

#### Acceptance Criteria

1. THE Auth_System SHALL provide a login page at GET /login with an email and password form, where the email field accepts up to 254 characters and the password field accepts up to 128 characters.
2. WHEN a user submits valid credentials to POST /login, THE Auth_System SHALL create a session record in the Auth_Database and set a signed HTTP-only cookie containing the session ID.
3. WHEN a user submits invalid credentials to POST /login, THE Auth_System SHALL return the login page with a generic error message indicating that login failed, without revealing whether the email or password was incorrect.
4. THE Auth_System SHALL sign session cookies using `itsdangerous` with a secret key configured via the `FCS_SESSION_SECRET` environment variable.
5. IF the `FCS_SESSION_SECRET` environment variable is not set at application startup, THEN THE Auth_System SHALL refuse to start and log an error message indicating the missing configuration.
6. THE Auth_System SHALL set session cookies with the HttpOnly flag and the SameSite=Lax attribute. IF SSL is configured (ssl_certfile and ssl_keyfile are set), THEN THE Auth_System SHALL also set the Secure flag on session cookies.
7. WHEN a user visits GET /logout, THE Auth_System SHALL invalidate the session record in the Auth_Database, clear the session cookie, and redirect to the login page regardless of whether the session was still valid.
8. THE Auth_System SHALL expire sessions after a configurable idle timeout (default: 8 hours), where idle time is measured from the most recent authenticated request made with that session.
9. WHEN a request contains a session cookie referencing an expired or invalid session, THE Auth_System SHALL clear the invalid cookie and treat the request as unauthenticated.
10. WHEN a request to a protected route contains no session cookie, THE Auth_System SHALL redirect the request to GET /login for HTML pages or return HTTP 401 for API endpoints.
11. WHEN a session is created, THE Auth_System SHALL record the client IP address of the request in the session record in the Auth_Database.
12. THE Auth_System SHALL enforce a Hard_Max_Lifetime per role: 8 hours for admin sessions and 120 hours (5 days) for data_entry sessions, measured from session creation time regardless of activity.
13. WHEN a request is made using a session that has exceeded its role-specific Hard_Max_Lifetime, THE Auth_System SHALL invalidate the session, clear the session cookie, and treat the request as unauthenticated.

### Requirement 3: Role-Based Authorization — Three Tiers

**User Story:** As a system operator, I want three distinct access tiers, so that users can only perform actions appropriate to their role.

#### Acceptance Criteria

1. THE Auth_System SHALL enforce three roles with a strict inclusive hierarchy: admin > data_entry > public, where each higher role inherits all permissions of the roles below it.
2. WHILE a user has the `admin` role, THE Auth_System SHALL permit access to all endpoints and pages, including user account management (creating users, changing user roles, and deactivating users).
3. WHILE a user has the `data_entry` role, THE Auth_System SHALL permit: scanning new cards (GET /scan, POST /api/scan), editing existing records (PUT /api/admin/record/{id}), triggering extraction (POST /api/admin/trigger, POST /api/admin/extract/{id}), requeuing records (POST /api/admin/requeue, POST /api/admin/requeue/{id}), switching extraction mode (POST /api/admin/mode), motor search/select (POST /api/admin/record/{id}/motor/{index}/search, POST /api/admin/record/{id}/motor/{index}/select), and GET-method access to all other pages and API endpoints.
4. WHILE a user has the `data_entry` role, THE Auth_System SHALL deny: deleting records (DELETE /api/admin/record/{id}), creating users, changing user roles, and deactivating users.
5. WHILE a request is unauthenticated (public), THE Auth_System SHALL permit only: GET requests to the list page (/), record detail pages (/record/{id}), reports (/reports/), the queue page (/queue), the login page (/login), and static assets.
6. WHILE a request is unauthenticated (public), THE Auth_System SHALL deny all POST, PUT, PATCH, and DELETE requests to API endpoints (except POST /login).
7. WHEN an unauthenticated user attempts to access a protected HTML page, THE Auth_System SHALL respond with an HTTP 302 redirect to the login page, including the originally requested path as a `next` URL query parameter.
8. WHEN an unauthenticated user attempts to access a protected API endpoint, THE Auth_System SHALL return HTTP 401 with a JSON body containing an error message indicating authentication is required.
9. WHEN an authenticated user attempts to access a resource above their role, THE Auth_System SHALL return HTTP 403 with a JSON error body for API endpoints or render an error page for HTML page requests, indicating insufficient permissions.
10. WHEN a user's role is changed, THE Auth_System SHALL enforce the new role's permissions on the next request made by that user without requiring re-authentication.

### Requirement 4: Page-Level Access Control

**User Story:** As a system operator, I want HTML pages to reflect the user's permissions, so that users see only the actions they can perform.

#### Acceptance Criteria

1. THE Auth_System SHALL inject the current user's email, display name, and role as template context variables into every Jinja2 template render.
2. WHILE the current user is unauthenticated, THE Auth_System SHALL omit from the rendered HTML all interactive elements for edit, delete, scan, queue, trigger, requeue, mode-switch, and admin actions.
3. WHILE the current user has the `data_entry` role, THE Auth_System SHALL omit from the rendered HTML all interactive elements for record deletion and user management actions.
4. WHILE the current user is unauthenticated, THE Auth_System SHALL display a login link in the navigation bar.
5. WHILE the current user is authenticated, THE Auth_System SHALL display the current user's display name and a logout link in the navigation bar.
6. WHILE the application is in read_only mode, THE Auth_System SHALL omit from the rendered HTML all interactive elements for scan, edit, delete, trigger, requeue, mode-switch, and admin actions regardless of the current user's role.
7. THE Auth_System SHALL enforce element hiding by excluding the HTML elements from the server-rendered response rather than hiding them with client-side styling.

### Requirement 5: Admin User Management

**User Story:** As an admin, I want to create, edit, and deactivate user accounts, so that I can control who has access to the system.

#### Acceptance Criteria

1. THE Auth_System SHALL provide an admin page at GET /admin/users listing all user accounts with their email, display name, role, and active status.
2. WHEN an admin submits a create-user request with a valid email (max 254 characters), display name (max 100 characters), role (one of the system's defined roles), and password (8 to 128 characters), THE Auth_System SHALL create a new User with those values and a hashed password, and return the created user record.
3. WHEN an admin submits an update-user request for an existing user, THE Auth_System SHALL update only the specified fields (display name, role, active status, or password) while preserving all unspecified fields unchanged.
4. IF an admin submits a request that would remove their own admin role or deactivate their own account, THEN THE Auth_System SHALL reject the request with an error indicating that self-demotion or self-deactivation is not allowed, and leave the account unchanged.
5. WHEN a user account is deactivated, THE Auth_System SHALL invalidate all active sessions for that user so that subsequent requests using those sessions are rejected.
6. THE Auth_System SHALL expose user management via API endpoints (GET /api/admin/users, POST /api/admin/users, PUT /api/admin/users/{id}) restricted to the admin role.
7. IF a create-user request specifies an email that already exists, THEN THE Auth_System SHALL return an error indicating the email is already in use without creating a duplicate account.
8. IF a non-admin user or unauthenticated request attempts to access any user management endpoint, THEN THE Auth_System SHALL reject the request with an error indicating insufficient permissions and not expose user data.
9. IF an update-user or deactivation request references a user ID that does not exist, THEN THE Auth_System SHALL return an error indicating the user was not found.
10. IF a create-user or update-user request contains an invalid email format, a display name exceeding 100 characters, a password outside the 8-128 character range, or a role value not in the set of defined system roles, THEN THE Auth_System SHALL reject the request with an error indicating which field failed validation.

### Requirement 6: Audit Logging

**User Story:** As a system operator, I want a persistent audit trail of user actions, so that I can review who changed what and when.

#### Acceptance Criteria

1. THE Audit_Logger SHALL write audit entries to a dedicated log file on disk, separate from web server access logs.
2. THE Audit_Logger SHALL configure the audit log file path via config.json (key: `audit_log_path`), defaulting to `{event_data_path}/audit.log`.
3. WHEN a mutating action occurs (record creation via scan upload, record field update via the review UI or API, record deletion, or extraction status change), THE Audit_Logger SHALL record a single JSON object containing: timestamp (ISO 8601 with timezone), actor identifier (the authenticated user's email, or the string "anonymous" when unauthenticated), action verb (one of: "created", "updated", "deleted", "extracted", "requeued"), object type (one of: "flight_record", "user"), object identifier (the integer record ID or user ID), and a details object.
4. WHEN a record is edited, THE Audit_Logger SHALL include in the details object a "changes" key whose value is an object mapping each changed field name to an object with "old" and "new" keys containing the previous and new values respectively, serialized as JSON-compatible primitives (strings, numbers, booleans, null, arrays, or objects).
5. WHEN a user logs in, logs out, or fails a login attempt, THE Audit_Logger SHALL record the event with the actor email (or attempted email for failed logins), action verb (one of: "login", "logout", "login_failed"), and a details object containing the result (one of: "success", "failed").
6. WHEN a data_entry user makes a request from a client IP address that differs from the IP address recorded in their session, THE Audit_Logger SHALL record the event with the actor email, action verb "ip_changed", object type "session", object identifier (the session ID), and a details object containing "old_ip" and "new_ip" keys with the respective IP addresses.
7. THE Audit_Logger SHALL write one JSON object per line (JSON Lines format), with no pretty-printing or multi-line entries, ensuring each line is independently parseable.
8. THE Audit_Logger SHALL use Python's `logging` module with the logger name "audit" and a dedicated `FileHandler` attached only to that logger, ensuring audit entries are not emitted to the application root logger or any other handler.
9. IF the audit log file cannot be written (permission error, disk full, or path not writable), THEN THE Audit_Logger SHALL log an error to the application logger (including the original exception message), not raise an exception, and not block or delay the original request.
10. THE Audit_Logger SHALL append to the existing audit log file on application startup without truncating previous entries, and SHALL create the file if it does not exist.

### Requirement 7: Configuration

**User Story:** As a system operator, I want auth-related settings in config.json and environment variables, so that I can configure the system for different deployments.

#### Acceptance Criteria

1. THE Auth_System SHALL read the auth database path from config.json (key: `auth_db_path`), defaulting to `./auth.db` resolved relative to the config file directory.
2. THE Auth_System SHALL read the session secret from the `FCS_SESSION_SECRET` environment variable.
3. IF the `FCS_SESSION_SECRET` environment variable is not set, is empty, or contains only whitespace, THEN THE Auth_System SHALL refuse to start and log an error indicating that a non-empty session secret is required. IF the value is set but fewer than 16 characters in length, THEN THE Auth_System SHALL refuse to start and log an error indicating the minimum length requirement.
4. THE Auth_System SHALL read the session idle timeout from config.json (key: `session_timeout_hours`) as a numeric value between 0.25 and 8 inclusive, defaulting to 8 hours. This value controls only the idle timeout (time since last activity); it does not affect the role-specific Hard_Max_Lifetime, which is fixed at 8 hours for admin and 120 hours for data_entry and is not configurable. IF the value is not a number or is outside the valid range, THEN THE Auth_System SHALL refuse to start and log an error indicating the accepted range.
5. THE Audit_Logger SHALL read the audit log path from config.json (key: `audit_log_path`), defaulting to `{event_data_path}/audit.log`.

### Requirement 8: Security Hardening

**User Story:** As a system operator, I want the auth system to follow security best practices, so that user credentials and sessions are protected.

#### Acceptance Criteria

1. THE Auth_System SHALL store passwords using Argon2id and SHALL never store plaintext passwords in the Auth_Database or in logs.
2. THE Auth_System SHALL rate-limit login attempts to a maximum of 5 failed attempts per email address within a sliding 15-minute window, where the window is measured from the most recent failed attempt backward.
3. WHEN the rate limit is exceeded for an email, THE Auth_System SHALL return HTTP 429 with a response body indicating the number of seconds remaining until the earliest failed attempt expires from the window, and SHALL reject further login attempts for that email until the window contains fewer than 5 failed attempts.
4. THE Auth_System SHALL generate session IDs using `secrets.token_urlsafe(32)`, producing tokens with at least 256 bits of entropy.
5. THE Auth_System SHALL not expose user enumeration through login error messages or timing differences; the observable response time for a non-existent account SHALL NOT differ from that of an existing account by more than 100 milliseconds under equivalent server load.
6. THE Audit_Logger SHALL never log plaintext passwords, even in failed login attempt entries.
7. WHEN a user successfully authenticates, THE Auth_System SHALL reset the failed-attempt counter for that email address to zero.
8. WHILE a user has the `admin` role, THE Auth_System SHALL enforce strict IP_Binding: WHEN a request is made from a client IP address that differs from the IP address recorded in the session, THE Auth_System SHALL immediately invalidate the session, clear the session cookie, and treat the request as unauthenticated.
9. WHILE a user has the `data_entry` role, THE Auth_System SHALL enforce soft IP_Binding: WHEN a request is made from a client IP address that differs from the IP address recorded in the session, THE Auth_System SHALL allow the request to proceed with the session still valid, and THE Audit_Logger SHALL record the IP change event.
10. THE Auth_System SHALL NOT allow the Hard_Max_Lifetime values (admin: 8 hours, data_entry: 120 hours) or the IP_Binding behavior to be changed via configuration; these policies are fixed per role.
