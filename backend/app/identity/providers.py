from app.identity.contracts import IdentityContext


class DevelopmentIdentityProvider:
    """Local identity provider. Production must replace this at the API gateway."""

    def __init__(
        self,
        *,
        default_user_id: str,
        default_tenant_id: str,
        default_org_code: str,
        default_roles: list[str] | None = None,
    ) -> None:
        self.default_user_id = default_user_id
        self.default_tenant_id = default_tenant_id
        self.default_org_code = default_org_code
        self.default_roles = default_roles or ["procurement_manager"]

    def resolve(
        self,
        *,
        user_id: str | None,
        tenant_id: str | None,
        org_code: str | None,
        roles: list[str] | None = None,
        bearer_token: str | None = None,
    ) -> IdentityContext:
        del bearer_token
        return IdentityContext(
            user_id=(user_id or self.default_user_id).strip(),
            display_name=(user_id or self.default_user_id).strip(),
            tenant_id=(tenant_id or self.default_tenant_id).strip(),
            org_code=(org_code or self.default_org_code).strip(),
            roles=roles or self.default_roles,
            auth_source="development_headers",
            trusted=False,
        )


class JwtIdentityProvider:
    """Verifies an enterprise JWT and maps configured claims to platform scope."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        algorithms: list[str],
        user_claim: str = "sub",
        tenant_claim: str = "tenant_id",
        org_claim: str = "org_code",
        roles_claim: str = "roles",
        display_name_claim: str = "name",
        email_claim: str = "email",
    ) -> None:
        import jwt

        self.jwt = jwt
        self.jwks_client = jwt.PyJWKClient(jwks_url)
        self.issuer = issuer
        self.audience = audience
        self.algorithms = algorithms
        self.user_claim = user_claim
        self.tenant_claim = tenant_claim
        self.org_claim = org_claim
        self.roles_claim = roles_claim
        self.display_name_claim = display_name_claim
        self.email_claim = email_claim

    def resolve(
        self,
        *,
        user_id: str | None,
        tenant_id: str | None,
        org_code: str | None,
        roles: list[str] | None = None,
        bearer_token: str | None = None,
    ) -> IdentityContext:
        del user_id, tenant_id, org_code, roles
        if not bearer_token:
            raise ValueError("a verified bearer token is required")
        signing_key = self.jwks_client.get_signing_key_from_jwt(bearer_token)
        claims = self.jwt.decode(
            bearer_token,
            signing_key.key,
            algorithms=self.algorithms,
            audience=self.audience,
            issuer=self.issuer,
            options={
                "require": [
                    "exp",
                    "nbf",
                    self.user_claim,
                    self.tenant_claim,
                    self.org_claim,
                    self.roles_claim,
                ]
            },
        )
        required_strings = {
            "user_id": claims.get(self.user_claim),
            "tenant_id": claims.get(self.tenant_claim),
            "org_code": claims.get(self.org_claim),
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in required_strings.values()
        ):
            raise ValueError("OIDC identity claims must be non-empty strings")
        resolved_roles = claims.get(self.roles_claim)
        if not isinstance(resolved_roles, list) or not all(
            isinstance(item, str) and item.strip() for item in resolved_roles
        ):
            raise ValueError("OIDC roles claim must be an array of non-empty strings")
        optional_strings = {
            "display_name": claims.get(self.display_name_claim),
            "email": claims.get(self.email_claim),
        }
        if any(
            value is not None and (not isinstance(value, str) or not value.strip())
            for value in optional_strings.values()
        ):
            raise ValueError("OIDC optional identity claims must be non-empty strings")
        return IdentityContext(
            user_id=required_strings["user_id"].strip(),
            display_name=(optional_strings["display_name"] or required_strings["user_id"]).strip(),
            email=(optional_strings["email"].strip() if optional_strings["email"] else None),
            tenant_id=required_strings["tenant_id"].strip(),
            org_code=required_strings["org_code"].strip(),
            roles=[item.strip() for item in resolved_roles],
            auth_source="oidc_jwt",
            trusted=True,
            delegated_access_token=bearer_token,
        )
