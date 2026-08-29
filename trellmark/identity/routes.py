LOGIN_OPERATION = ("POST", "/api/auth/login")
SESSION_OPERATION = ("GET", "/api/auth/session")
LOGOUT_OPERATION = ("POST", "/api/auth/logout")
HEALTH_OPERATION = ("GET", "/api/health")

PUBLIC_OPERATIONS = frozenset(
    {
        LOGIN_OPERATION,
        SESSION_OPERATION,
        LOGOUT_OPERATION,
        HEALTH_OPERATION,
    }
)
