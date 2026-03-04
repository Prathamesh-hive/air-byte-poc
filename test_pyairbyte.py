"""
Run Google Drive connector locally via PyAirbyte (Docker).
This bypasses Airbyte Cloud and shows the real connector error.
"""
import json, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

with open("tokens.json") as f:
    tok = json.load(f)
g = tok["google"]

FOLDER_URL = "https://drive.google.com/drive/folders/1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA"

import airbyte as ab

print("Testing connector locally with PyAirbyte...")
print(f"folder_url: {FOLDER_URL}")
print()

source = ab.get_source(
    "source-google-drive",
    config={
        "folder_url": FOLDER_URL,
        "credentials": {
            "auth_type": "Client",
            "client_id": g["client_id"],
            "client_secret": g["client_secret"],
            "refresh_token": g["refresh_token"],
        },
        "streams": [
            {
                "name": "documents",
                "globs": ["**"],
                "validation_policy": "Emit Record",
                "days_to_sync_if_history_is_full": 3,
                "format": {"filetype": "unstructured"},
            }
        ],
    },
)

print("Checking connection...")
source.check()
print("Connection check PASSED!")
print()
print("Getting available streams...")
streams = source.get_available_streams()
print("Streams:", streams)
