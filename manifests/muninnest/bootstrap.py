"""Idempotent Memos bootstrap; runs in a Kubernetes Job using only the stdlib."""

import base64
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path


class APIError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"API request failed with HTTP {status}")


def request(url, method="GET", body=None, token=None, context=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=context) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        # Never log response bodies, request bodies, headers, or credentials.
        raise APIError(error.code) from None


def decode(secret, key):
    return base64.b64decode(secret.get("data", {}).get(key, "")).decode()


def ensure_token(memos, username, password, old_token):
    if old_token:
        try:
            user = memos("/auth/me", token=old_token).get("user", {})
            if user.get("username") != username:
                raise RuntimeError("Existing token belongs to another Memos user")
            print("Reusing valid Memos token", flush=True)
            return old_token
        except APIError as error:
            if error.status != 401:
                raise

    profile = memos("/instance/profile")
    if profile.get("needsSetup") is True:
        memos(
            "/users",
            method="POST",
            body={"username": username, "password": password, "role": "ADMIN"},
        )
        print("Created initial Memos account", flush=True)
    # Existing installations must authenticate; never reset passwords or claim
    # another account when credentials are wrong.
    session = memos(
        "/auth/signin",
        method="POST",
        body={"passwordCredentials": {"username": username, "password": password}},
    )
    name = session["user"]["name"]
    result = memos(
        f"/{name}/personalAccessTokens",
        method="POST",
        token=session["accessToken"],
        body={"description": "Muninnest bootstrap", "expiresInDays": 0},
    )
    token = result["token"]
    user = memos("/auth/me", token=token).get("user", {})
    if user.get("username") != username:
        raise RuntimeError("Generated token failed account validation")
    print("Created Memos bot token", flush=True)
    return token


def reconcile(kube, memos, username, password, telegram):
    path = "/api/v1/namespaces/muninnest/secrets"
    try:
        secret = kube(path + "/muninnest-secrets")
    except APIError as error:
        if error.status != 404:
            raise
        secret = None
    token = ensure_token(
        memos, username, password, decode(secret or {}, "MEMOS_ACCESS_TOKEN")
    )
    data = {
        key: base64.b64encode(value.encode()).decode()
        for key, value in {
            "MEMOS_ACCESS_TOKEN": token,
            "TELEGRAM_BOT_TOKEN": telegram,
        }.items()
    }
    if secret is None:
        kube(
            path,
            method="POST",
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "muninnest-secrets", "namespace": "muninnest"},
                "type": "Opaque",
                "data": data,
            },
        )
    elif any(secret.get("data", {}).get(k) != v for k, v in data.items()):
        kube(path + "/muninnest-secrets", method="PUT", body={**secret, "data": data})

    # A deterministic revision makes retries safe, including a retry after the
    # Secret was saved but before the Deployment annotation was updated.
    revision = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    path = "/apis/apps/v1/namespaces/muninnest/deployments/muninnest"
    for _ in range(5):
        deployment = kube(path)
        annotations = deployment["spec"]["template"]["metadata"].setdefault(
            "annotations", {}
        )
        if annotations.get("secrets.gitops.io/revision") == revision:
            break
        annotations["secrets.gitops.io/revision"] = revision
        try:
            kube(path, method="PUT", body=deployment)
            break
        except APIError as error:
            if error.status != 409:
                raise
    else:
        raise RuntimeError("Deployment kept changing; retry bootstrap")
    print("Runtime Secret and bot credential revision reconciled", flush=True)


def main():
    username = os.environ["MEMOS_USERNAME"]
    password = os.environ["MEMOS_PASSWORD"]
    telegram = os.environ["TELEGRAM_BOT_TOKEN"]
    if not all((username, password, telegram)):
        raise RuntimeError("Bootstrap credentials must not be empty")
    memos_url = "http://memos:5230"
    for attempt in range(60):
        try:
            # /healthz returns plain text rather than JSON.
            with urllib.request.urlopen(memos_url + "/healthz", timeout=5):
                break
        except OSError, urllib.error.URLError:
            if attempt == 59:
                raise RuntimeError("Memos did not become ready") from None
            time.sleep(5)
    serviceaccount = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    context = ssl.create_default_context(cafile=str(serviceaccount / "ca.crt"))
    token = (serviceaccount / "token").read_text().strip()

    def kube(path, **kwargs):
        return request(
            "https://kubernetes.default.svc" + path,
            token=token,
            context=context,
            **kwargs,
        )

    def memos(path, **kwargs):
        return request(memos_url + "/api/v1" + path, **kwargs)

    reconcile(kube, memos, username, password, telegram)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- never expose secret-bearing tracebacks
        # Unexpected exception text can contain secrets: fail without a traceback.
        raise SystemExit(
            "Bootstrap failed. Verify credentials, Memos API compatibility, and RBAC. "
            "Sensitive error details withheld."
        ) from None
