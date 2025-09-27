#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-api-python-client",
#     "google-auth",
#     "requests",
# ]
# ///
import csv
import io
import os
import tomllib

import requests
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Configuration
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'env.toml'), 'rb') as f:
    config = tomllib.load(f)
    
GOOGLE_SHEETS_API_KEY = config['google']['api_key']
SPREADSHEET_ID = config['google']['spreadsheet_id']
RANGE_NAME = config['google']['spreadsheet_range']

RADARR_API_KEY = config['radarr']['api_key']
RADARR_URL = config['radarr']['url']
RADARR_QUALITY_PROFILE = config['radarr']['quality_profile']
RADARR_ROOT_FOLDER_PATH = config['radarr']['root_folder_path']

SONARR_API_KEY = config['sonarr']['api_key']
SONARR_URL = config['sonarr']['url']
SONARR_QUALITY_PROFILE = config['sonarr']['quality_profile']
SONARR_ROOT_FOLDER_PATH = config['sonarr']['root_folder_path']

# Initialize Google Sheets API
def get_google_sheets_data():
    try:
        service = build('sheets', 'v4', developerKey=GOOGLE_SHEETS_API_KEY)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
        values = result.get('values', [])
        return values
    except HttpError as error:
        print(f"An error occurred: {error}")
        return []

# Get TMDb ID from URL
def get_tmdb_id(url):
    if 'themoviedb.org/movie/' in url:
        return url.split('/movie/')[1].split('-')[0]
    elif 'themoviedb.org/tv/' in url:
        return url.split('/tv/')[1].split('-')[0]
    return None

# Check if movie/show is in Radarr/Sonarr
def is_in_radarr(tmdb_id):
    response = requests.get(f"{RADARR_URL}/movie?tmdbId={tmdb_id}", headers={'X-Api-Key': RADARR_API_KEY})
    return len(response.json()) > 0

def is_in_sonarr(tmdb_id):
    lookup_response = requests.get(f"{SONARR_URL}/series/lookup?term=tmdb:{tmdb_id}", headers={'X-Api-Key': SONARR_API_KEY}).json()
    if len(lookup_response) == 0:
        print(f"Could not find series with tmdbId {tmdb_id}")
        return True
    tvdbId = lookup_response[0]['tvdbId']
    response = requests.get(f"{SONARR_URL}/series?tvdbId={tvdbId}", headers={'X-Api-Key': SONARR_API_KEY})
    return len(response.json()) > 0

# Add movie to Radarr
def add_to_radarr(tmdb_id):
    payload = {
        "qualityProfileId": RADARR_QUALITY_PROFILE,  # Adjust as needed
        "tmdbId": int(tmdb_id),
        "rootFolderPath": RADARR_ROOT_FOLDER_PATH,
        "monitored": True,
        "addOptions": {
            "searchForMovie": True
        }
    }
    add_response = requests.post(f"{RADARR_URL}/movie", json=payload, headers={'X-Api-Key': RADARR_API_KEY})
    return add_response.status_code == 201

# Add show to Sonarr
def add_to_sonarr(tmdb_id):
    response = requests.get(f"{SONARR_URL}/series/lookup?term=tmdb:{tmdb_id}", headers={'X-Api-Key': SONARR_API_KEY})
    if response.status_code == 200 and response.json():
        show_data = response.json()[0]
        payload = {
            "title": show_data['title'],
            "qualityProfileId": SONARR_QUALITY_PROFILE,  # Adjust as needed
            "tvdbId": int(show_data['tvdbId']),
            "rootFolderPath": SONARR_ROOT_FOLDER_PATH,  # Adjust as needed
            "monitored": True,
            "addOptions": {
                "searchForMissingEpisodes": True
            }
        }
        add_response = requests.post(f"{SONARR_URL}/series", json=payload, headers={'X-Api-Key': SONARR_API_KEY})
        print(add_response.json())
        return add_response.status_code == 201
    return False

# Main function
def main():
    links = get_google_sheets_data()
    for link in links:
        if link:
            url = link[0]
            tmdb_id = get_tmdb_id(url)
            if tmdb_id:
                if 'themoviedb.org/movie/' in url:
                    if not is_in_radarr(tmdb_id):
                        if add_to_radarr(tmdb_id):
                            print(f"Added movie with TMDb ID {tmdb_id} to Radarr")
                        else:
                            print(f"Failed to add movie with TMDb ID {tmdb_id} to Radarr")
                    else:
                        print(f"Movie with TMDb ID {tmdb_id} is already in Radarr")
                elif 'themoviedb.org/tv/' in url:
                    if not is_in_sonarr(tmdb_id):
                        if add_to_sonarr(tmdb_id):
                            print(f"Added show with TMDb ID {tmdb_id} to Sonarr")
                        else:
                            print(f"Failed to add show with TMDb ID {tmdb_id} to Sonarr")
                    else:
                        print(f"Show with TMDb ID {tmdb_id} is already in Sonarr")

if __name__ == "__main__":
    main()
