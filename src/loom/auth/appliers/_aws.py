"""AWS SigV4 credential applier — signs HTTP requests with AWS Signature Version 4."""

from __future__ import annotations

from loom.store.secrets import AwsSigV4Secret


class SigV4Applier:
    """Signs HTTP requests with AWS Signature Version 4."""

    secret_type: str = "aws_sigv4"

    async def apply(self, secret: AwsSigV4Secret, context: dict) -> dict[str, str]:  # type: ignore[override]
        try:
            import botocore.auth
            import botocore.awsrequest
            import botocore.credentials
        except ImportError as exc:
            raise ImportError(
                'botocore is required for SigV4 signing. '
                'Install it with: pip install "loom[aws]"'
            ) from exc

        method: str = context.get("method", "GET").upper()
        url: str = context.get("url", "")
        body = context.get("body") or b""
        if isinstance(body, str):
            body = body.encode("utf-8")

        service: str = context.get("service", "execute-api")
        region: str = context.get("region") or secret.get("region") or "us-east-1"

        creds = botocore.credentials.Credentials(
            access_key=secret["access_key_id"],
            secret_key=secret["secret_access_key"],
            token=secret.get("session_token"),
        )

        incoming_headers: dict[str, str] = dict(context.get("headers") or {})
        request = botocore.awsrequest.AWSRequest(
            method=method,
            url=url,
            data=body,
            headers=incoming_headers,
        )

        signer = botocore.auth.SigV4Auth(creds, service, region)
        signer.add_auth(request)

        return dict(request.headers)
