#!/usr/bin/env python3
"""Print a Cognito ID token so the API can be exercised with curl.

Uses USER_PASSWORD_AUTH, which the app client only enables in dev. Against a
prod pool this will fail with NotAuthorizedException -- by design.

    python scripts/get_token.py --email me@example.com \
        --outputs-file cdk-outputs.json

    curl -H "Authorization: Bearer $(python scripts/get_token.py -e me@example.com)" \
        "$API_URL/account"

The app client has no secret, so no SECRET_HASH is required.

Pool and client ids come from, in order of precedence:
  1. --user-pool-client-id
  2. --outputs-file (the JSON written by `cdk deploy --outputs-file`)
  3. COGNITO_CLIENT_ID / COGNITO_USER_POOL_ID environment variables
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "ap-southeast-2"
CLIENT_ID_OUTPUT_KEY = "UserPoolClientId"


def client_id_from_outputs(path: Path) -> str | None:
    """Pull the app client id out of a `cdk deploy --outputs-file` document.

    The file maps stack name -> {output key: value}; the auth stack is found by
    the output key rather than by stack name so dev/prod both work.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"outputs file not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"outputs file is not valid JSON: {path} ({exc})")

    for outputs in document.values():
        if isinstance(outputs, dict) and CLIENT_ID_OUTPUT_KEY in outputs:
            return str(outputs[CLIENT_ID_OUTPUT_KEY])
    return None


def resolve_client_id(args: argparse.Namespace) -> str:
    if args.user_pool_client_id:
        return args.user_pool_client_id
    if args.outputs_file:
        found = client_id_from_outputs(Path(args.outputs_file))
        if found:
            return found
        sys.exit(f"no {CLIENT_ID_OUTPUT_KEY} output found in {args.outputs_file}")
    from_env = os.environ.get("COGNITO_CLIENT_ID")
    if from_env:
        return from_env
    sys.exit(
        "app client id not found. Pass --user-pool-client-id, --outputs-file, "
        "or set COGNITO_CLIENT_ID."
    )


def resolve_password(args: argparse.Namespace) -> str:
    """Prefer an env var or an interactive prompt over argv.

    A password on the command line lands in shell history and in the process
    list, so there is deliberately no --password flag.
    """
    from_env = os.environ.get("COGNITO_PASSWORD")
    if from_env:
        return from_env
    return getpass.getpass(f"Password for {args.email}: ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-e", "--email", required=True, help="user's email address")
    parser.add_argument("--user-pool-client-id", help="Cognito app client id")
    parser.add_argument("--outputs-file", help="JSON from `cdk deploy --outputs-file`")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    parser.add_argument(
        "--token",
        choices=("id", "access", "refresh"),
        default="id",
        help="which token to print (default: id -- the one the API accepts)",
    )
    args = parser.parse_args()

    client_id = resolve_client_id(args)
    password = resolve_password(args)

    cognito = boto3.client("cognito-idp", region_name=args.region)
    try:
        response = cognito.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": args.email, "PASSWORD": password},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "NotAuthorizedException":
            sys.exit(
                "Authentication failed. Check the credentials, and note that "
                "USER_PASSWORD_AUTH is only enabled on the dev app client."
            )
        if code == "UserNotConfirmedException":
            sys.exit("User has not confirmed their email address yet.")
        raise

    if "ChallengeName" in response:
        sys.exit(
            f"Cognito returned challenge {response['ChallengeName']}, which this "
            "script does not handle. Resolve it in the console and retry."
        )

    result = response["AuthenticationResult"]
    key = {"id": "IdToken", "access": "AccessToken", "refresh": "RefreshToken"}[args.token]
    print(result[key])


if __name__ == "__main__":
    main()
