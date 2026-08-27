"""Per-service operational routers.

Each infrastructure dependency gets its own CRUD surface under
`/api/v1/services/<name>`. These are powerful, operator-only endpoints intended
for local development, debugging and administration -- not customer traffic.
"""
