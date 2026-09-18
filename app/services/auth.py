import os
import jwt
import requests
from functools import lru_cache

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://pxvtvuwlpzwlkdoxjrep.supabase.co",
)
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")


@lru_cache(maxsize=1)
def get_jwks():
    url = SUPABASE_URL + "/auth/v1/.well-known/jwks.json"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def verify_supabase_token(token):
    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "HS256")

    if alg == "HS256":
        if SUPABASE_JWT_SECRET:
            return jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
            )
        # Fallback for local dev only
        return jwt.decode(token, options={"verify_signature": False})

    jwks = get_jwks()
    key = next(
        (k for k in jwks.get("keys", []) if k["kid"] == header["kid"]),
        None,
    )
    if not key:
        raise RuntimeError("Signing key not found in JWKS")

    if alg == "ES256":
        public_key = jwt.algorithms.ECAlgorithm.from_jwk(key)
    else:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)

    return jwt.decode(
        token,
        public_key,
        algorithms=[alg],
        audience="authenticated",
    )
