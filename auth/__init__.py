"""Authentication: sessions (cookie), API tokens (bearer), Google OIDC, bootstrap, principal resolution.

Tenant is ALWAYS derived from the authenticated user (CONTRACTS.md "Authentication and tenant isolation").
Nothing in this package logs a token, cookie, id_token or secret.
"""
