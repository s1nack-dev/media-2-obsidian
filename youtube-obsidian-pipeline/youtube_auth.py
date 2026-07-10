#!/usr/bin/env python3
"""
One-time setup script: authorizes this app to read your private YouTube
playlist and saves a refresh token (token.json) that pipeline.py reuses
on every future run without prompting you again.

Usage:
    python youtube_auth.py --config config.yaml

If you're running this ON a headless server (no browser), instead run this
script on your own laptop (same client_secret.json), let it open the browser
there, then copy the resulting token.json over to the server.
"""
import argparse
import subprocess
import yaml
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def main():
    """
    Authorize access to YouTube and save the resulting credentials for reuse.
    
    Parameters:
        --config: Path to the YAML configuration file.
    
    The authorization flow opens a browser and requires a local redirect listener. The configuration must provide the token file path and 1Password references for the OAuth client credentials.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    token_file = cfg["youtube"]["token_file"]

    def op_read(ref):
        """
        Read a secret value from 1Password using its reference.
        
        Parameters:
            ref (str): The 1Password reference to read.
        
        Returns:
            str: The trimmed secret value.
        """
        return subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True).stdout.strip()

    client_config = {
        "installed": {
            "client_id": op_read(cfg["youtube"]["client_id_op_ref"]),
            "client_secret": op_read(cfg["youtube"]["client_secret_op_ref"]),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # run_local_server opens a browser and spins up a temporary local
    # redirect listener on localhost. If you're on a truly headless box
    # with no browser at all, run this script on your laptop instead.
    creds = flow.run_local_server(port=0)

    with open(token_file, "w") as f:
        f.write(creds.to_json())

    print(f"Saved credentials to {token_file}. You can now run pipeline.py.")


if __name__ == "__main__":
    main()
