#!/usr/bin/env python3

import requests
import json
import os
import secrets
import urllib.parse
import webbrowser
import http.server
import socketserver
import threading
import sys
import subprocess

# --- Monzo API Endpoints ---
MONZO_AUTH_URL = "https://auth.monzo.com/"
MONZO_TOKEN_URL = "https://api.monzo.com/oauth2/token"
MONZO_PING_WHOAMI_URL = "https://api.monzo.com/ping/whoami"
MONZO_ACCOUNTS_URL = "https://api.monzo.com/accounts"
MONZO_BALANCE_URL = "https://api.monzo.com/balance"

# --- Configuration from Environment Variables ---
CLIENT_ID = os.getenv("MONZO_CLIENT_ID")
CLIENT_SECRET = os.getenv("MONZO_CLIENT_SECRET")
# This variable specifies the path for the token file:
# - Local path on macOS/Windows (e.g., "/Path/To/File/monzo_tokens.json")
# - rclone remote path on Raspberry Pi (e.g., "onedrive:Path/To/File/monzo_tokens.json")
MONZO_TOKEN_FILE = os.getenv("MONZO_TOKEN_FILE")

# --- Local HTTP Server for OAuth Callback ---
REDIRECT_URI_PORT = 8000
REDIRECT_URI_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_URI_PORT}{REDIRECT_URI_PATH}"
AUTH_CODE = None
AUTH_STATE = None
AUTH_SERVER_STARTED = threading.Event()
AUTH_SERVER_STOPPED = threading.Event()


# --- Environment Detection Helper ---
def is_running_on_raspberry_pi():
    """
    Detects if the script is running on a Raspberry Pi (Linux ARM architecture).
    """
    return sys.platform.startswith("linux") and (
        os.uname().machine.startswith("arm") or os.uname().machine.startswith("aarch64")
    )


# --- Token Management (Local File) ---


def save_tokens_local(tokens, file_path):
    """Saves tokens to a local JSON file."""
    try:
        with open(file_path, "w") as f:
            json.dump(tokens, f, indent=4)
        print(f"Tokens saved to {file_path}")
    except IOError as e:
        print(f"Error Local Save: {e}")


def load_tokens_local(file_path):
    """Loads tokens from a local JSON file."""
    try:
        with open(file_path, "r") as f:
            tokens = json.load(f)
        print(f"Tokens loaded from {file_path}")
        return tokens
    except (IOError, json.JSONDecodeError):
        print(f"Error Local Load: No existing tokens found or file malformed.")
        return None


# --- Token Management (OneDrive via rclone - Direct Streaming) ---


def load_tokens_onedrive(file_path):
    """Loads JSON data from OneDrive using rclone cat."""

    try:
        # Use 'rclone cat' to get the file content directly from the remote
        result = subprocess.run(
            ["rclone", "cat", file_path],
            capture_output=True,
            text=True,  # Decode stdout/stderr as text
            check=True,  # Raise CalledProcessError if rclone returns non-zero exit code
        )
        json_content = result.stdout.strip()
        data = json.loads(json_content)
        print(f"Tokens loaded from {file_path}")
        return data

    except Exception as e:
        print(
            f"Error OneDrive Load for {file_path}: Failed to retrieve or parse tokens. Details: {e}"
        )
        return None


def save_tokens_onedrive(tokens, file_path):
    """Saves a dictionary as JSON directly to OneDrive using rclone rcat."""

    # Convert dictionary to JSON string
    try:
        json_content = json.dumps(tokens, indent=4)

        # Use 'rclone rcat' to upload the JSON content via stdin
        subprocess.run(
            ["rclone", "rcat", file_path],
            input=json_content,  # Pass the JSON string to stdin
            capture_output=True,
            text=True,
            check=True,
        )

        print(f"Tokens saved to {file_path}")
        return True

    except Exception as e:
        print(
            f"Error OneDrive Save for {file_path}: Failed to save tokens. Details: {e}"
        )
        return False


# --- Unified Load/Save Wrappers ---


def load_monzo_tokens():
    """
    Loads Monzo tokens from either a local file or OneDrive, based on the environment.
    """
    if not MONZO_TOKEN_FILE:
        print("Error: MONZO_TOKEN_FILE environment variable not set.")
        return None

    if is_running_on_raspberry_pi():
        # On Pi, load from OneDrive (streaming directly)
        return load_tokens_onedrive(MONZO_TOKEN_FILE)
    else:
        # On local computer, load directly from local file path
        return load_tokens_local(MONZO_TOKEN_FILE)


def save_monzo_tokens(tokens):
    """
    Saves Monzo tokens to either a local file or OneDrive, based on the environment.
    """
    if not MONZO_TOKEN_FILE:
        print("Error: MONZO_TOKEN_FILE environment variable not set.")
        return False

    if is_running_on_raspberry_pi():
        # On Pi, save to local file, then upload to OneDrive
        return save_tokens_onedrive(tokens, MONZO_TOKEN_FILE)
    else:
        # On local computer, save directly to local file
        save_tokens_local(tokens, MONZO_TOKEN_FILE)
        return True


# --- Local HTTP Server for OAuth Callback (Unchanged) ---


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the OAuth redirect to capture the authorization code."""

    def do_GET(self):
        global AUTH_CODE, AUTH_STATE

        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)

        if (
            parsed_url.path == REDIRECT_URI_PATH
            and "code" in query_params
            and "state" in query_params
        ):
            AUTH_CODE = query_params["code"][0]
            AUTH_STATE = query_params["state"][0]

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authentication Successful!</h1><p>You can now close this tab.</p></body></html>"
            )
            print("Authorization code received! Closing local server.")
            AUTH_SERVER_STOPPED.set()
        else:
            self.send_response(404)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>404 Not Found</h1></body></html>")

        if AUTH_SERVER_STOPPED.is_set():
            threading.Thread(target=self.server.shutdown).start()


def start_local_oauth_server():
    """Starts a local HTTP server for OAuth redirect."""
    server_address = ("localhost", REDIRECT_URI_PORT)
    with socketserver.ThreadingTCPServer(server_address, OAuthCallbackHandler) as httpd:
        print(f"Starting local server on {REDIRECT_URI}. Waiting for code...")
        AUTH_SERVER_STARTED.set()
        httpd.serve_forever()


# --- Monzo OAuth Flow Functions (Modified to always refresh/reauthenticate) ---


def get_authorization_code():
    """Initiates OAuth flow and captures authorization code."""
    global AUTH_CODE, AUTH_STATE

    AUTH_CODE = None
    AUTH_STATE = None
    AUTH_SERVER_STARTED.clear()
    AUTH_SERVER_STOPPED.clear()

    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("Client ID and Secret must be set.")

    state_token = secrets.token_urlsafe(32)

    auth_params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state_token,
    }
    auth_url = MONZO_AUTH_URL + "?" + urllib.parse.urlencode(auth_params)

    print("\nOpening browser for Monzo authentication. Please authorize.")
    print(f"If browser doesn't open, copy and paste: {auth_url}")

    server_thread = threading.Thread(target=start_local_oauth_server, daemon=True)
    server_thread.start()

    AUTH_SERVER_STARTED.wait(timeout=10)
    if not AUTH_SERVER_STARTED.is_set():
        print("Error: Local OAuth server failed to start.")
        return None

    webbrowser.open(auth_url)

    print("Waiting for user authorization (max 2 minutes)...")
    AUTH_SERVER_STOPPED.wait(timeout=120)

    if not AUTH_CODE:
        print("Error: Authorization code not received or timed out.")
        return None

    if AUTH_STATE != state_token:
        print("Security Warning: State token mismatch! Aborting.")
        AUTH_CODE = None
        return None

    print(f"Authorization code obtained.")
    return AUTH_CODE


def exchange_code_for_tokens(auth_code):
    """Exchanges the authorization code for access and refresh tokens."""
    print("Exchanging authorization code for tokens...")
    token_params = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
    }
    try:
        response = requests.post(MONZO_TOKEN_URL, data=token_params)
        response.raise_for_status()
        tokens = response.json()
        print("Tokens received.")
        return tokens
    except requests.exceptions.RequestException as e:
        print(f"Error exchanging code for tokens: {e}")
        return None


def refresh_access_token(refresh_token_value):
    """Refreshes the access token using the refresh token."""
    print("Refreshing access token...")
    refresh_params = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token_value,
    }
    try:
        response = requests.post(MONZO_TOKEN_URL, data=refresh_params)
        response.raise_for_status()
        new_tokens = response.json()
        print("Access token refreshed.")
        return new_tokens
    except requests.exceptions.RequestException as e:
        print(f"Error refreshing token: {e}")
        return None


def get_monzo_account_id(access_token_value):
    """Fetches the primary Monzo account ID."""
    print("Fetching account ID...")
    headers = {"Authorization": f"Bearer {access_token_value}"}
    try:
        response = requests.get(MONZO_ACCOUNTS_URL, headers=headers)
        response.raise_for_status()
        accounts_data = response.json()
        if accounts_data and "accounts" in accounts_data:
            for account in accounts_data["accounts"]:
                if (
                    account.get("type") == "uk_retail"
                    and account.get("closed") == False
                ):  # Prioritize active retail account
                    print(f"Account ID fetched: {account['id']}")
                    return account["id"]
            if accounts_data[
                "accounts"
            ]:  # Fallback to first available account if no active retail
                print(f"Account ID fetched: {accounts_data['accounts'][0]['id']}")
                return accounts_data["accounts"][0]["id"]
            print("No suitable accounts found.")
            return None
        else:
            print("No accounts found.")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching account ID: {e}")
        return None


def get_monzo_balance(account_id_value, access_token_value):
    """Fetches the current balance for a given account ID."""
    print("Fetching account balance...")
    headers = {"Authorization": f"Bearer {access_token_value}"}
    params = {"account_id": account_id_value}
    try:
        response = requests.get(MONZO_BALANCE_URL, headers=headers, params=params)
        response.raise_for_status()
        balance_data = response.json()
        balance_gbp = balance_data.get("balance") / 100
        currency = balance_data.get("currency")
        print(f"Current Balance: {balance_gbp:.2f} {currency}")
        return balance_data
    except requests.exceptions.RequestException as e:
        print(f"Error fetching balance: {e}")
        return None


# --- Main execution block ---
if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: MONZO_CLIENT_ID and MONZO_CLIENT_SECRET must be set.")
        exit(1)

    if not MONZO_TOKEN_FILE:
        print(
            "Error: MONZO_TOKEN_FILE environment variable not set. This is required for both local and Pi runs."
        )
        exit(1)

    # Ensure the local directory for MONZO_TOKEN_FILE exists on local computer
    # On Pi, this is not needed as we're not creating local files for rclone streaming
    if not is_running_on_raspberry_pi():
        os.makedirs(os.path.dirname(MONZO_TOKEN_FILE), exist_ok=True)

    print("\n--- Starting Monzo Token Management ---")
    if is_running_on_raspberry_pi():
        print("Running on Raspberry Pi. Will use OneDrive for token storage.")
    else:
        print("Running on Local Computer. Will use local file for token storage.")

    # Always attempt to load existing tokens first to use refresh_token if possible
    current_tokens = load_monzo_tokens()
    access_token_to_use = None
    account_id_to_use = None

    if current_tokens and current_tokens.get("refresh_token"):
        print("Attempting to refresh existing access token...")
        new_tokens = refresh_access_token(current_tokens["refresh_token"])
        if new_tokens:
            current_tokens.update(
                new_tokens
            )  # Update with new access_token and refresh_token
            access_token_to_use = current_tokens["access_token"]
            account_id_to_use = current_tokens.get(
                "account_id"
            )  # Keep existing account_id or get new if needed
            print("Tokens refreshed successfully.")
        else:
            print("Refresh failed. Initiating full authorization flow...")
            # If refresh fails, fall through to full reauthentication
            access_token_to_use = None
    else:
        print(
            "No valid refresh token or existing tokens found. Initiating full authorization flow..."
        )
        # Fall through to full reauthentication

    if not access_token_to_use:  # If refresh failed or no tokens existed
        auth_code = get_authorization_code()
        if auth_code:
            new_tokens = exchange_code_for_tokens(auth_code)
            if new_tokens:
                current_tokens = new_tokens  # Set current_tokens to the brand new set
                access_token_to_use = current_tokens["access_token"]
                account_id_to_use = current_tokens.get(
                    "account_id"
                )  # Account ID might be new or same
                print("Full authentication successful.")
            else:
                print("Token exchange failed after full authorization. Exiting.")
                exit(1)
        else:
            print("Authorization code not obtained. Cannot authenticate. Exiting.")
            exit(1)

    # Fetch account ID if not already available (e.g., first time auth)
    if access_token_to_use and not account_id_to_use:
        fetched_account_id = get_monzo_account_id(access_token_to_use)
        if fetched_account_id:
            current_tokens["account_id"] = fetched_account_id
            account_id_to_use = fetched_account_id
        else:
            print("Could not retrieve account ID. Some API calls may fail.")

    # Always save the latest tokens (including the new refresh_token and account_id)
    save_monzo_tokens(current_tokens)

    print("\n--- Authentication Established ---")
    print(f"Current valid Access Token: ...{access_token_to_use[::-10]}")
    print(f"Current Account ID: {account_id_to_use}")
    print("Script is now authenticated.")

    if account_id_to_use and access_token_to_use:
        print("\n--- Example: Getting Account Balance ---")
        get_monzo_balance(account_id_to_use, access_token_to_use)
    else:
        print("Account ID or Access Token missing. Cannot perform example API calls.")
