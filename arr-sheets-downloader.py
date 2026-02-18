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
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Configuration
_script_dir = os.path.dirname(os.path.abspath(__file__))
_config_path = os.path.join(_script_dir, 'env.toml')
if not os.path.exists(_config_path):
    raise FileNotFoundError("env.toml not found. Copy env.example.toml to env.toml and fill in your values.")
with open(_config_path, 'rb') as f:
    config = tomllib.load(f)

GOOGLE_SHEETS_API_KEY = config['google'].get('api_key')
GOOGLE_SERVICE_ACCOUNT_FILE = config['google'].get('service_account_file')
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


def build_sheets_service():
    if GOOGLE_SERVICE_ACCOUNT_FILE:
        # Read+write scope so we can update the status column
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes)
        return build('sheets', 'v4', credentials=credentials)
    elif GOOGLE_SHEETS_API_KEY:
        return build('sheets', 'v4', developerKey=GOOGLE_SHEETS_API_KEY)
    else:
        raise ValueError("No Google auth configured: set api_key or service_account_file in env.toml")


def get_google_sheets_data(service):
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=RANGE_NAME).execute()
        return result.get('values', [])
    except HttpError as error:
        print(f"An error occurred: {error}")
        return []


def get_output_range():
    # Derive the 2-column output range (B=status, C=release date) from RANGE_NAME
    # e.g. "Sheet1!A2:A" -> "Sheet1!B2:C"
    sheet_name, cell_part = RANGE_NAME.split('!')
    start_row = ''.join(c for c in cell_part.split(':')[0] if c.isdigit())
    return f"{sheet_name}!B{start_row}:C"


def format_date(date_str):
    # Trim ISO timestamp to just the date portion, e.g. "2024-03-15T00:00:00Z" -> "2024-03-15"
    return date_str[:10] if date_str else ""


def update_sheet_statuses(service, rows):
    # rows is a list of [status, release_date] pairs
    try:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=get_output_range(),
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()
    except HttpError as error:
        print(f"Failed to update status column: {error}")
        if GOOGLE_SHEETS_API_KEY and not GOOGLE_SERVICE_ACCOUNT_FILE:
            print("Note: writing to sheets requires a service account, not an API key.")


# Get TMDb ID from URL
def get_tmdb_id(url):
    if 'themoviedb.org/movie/' in url:
        return url.split('/movie/')[1].split('-')[0]
    elif 'themoviedb.org/tv/' in url:
        return url.split('/tv/')[1].split('-')[0]
    return None


# Returns (in_radarr, status_str, digital_release_date). status_str is None if not in Radarr.
def get_radarr_status(tmdb_id):
    response = requests.get(f"{RADARR_URL}/movie?tmdbId={tmdb_id}", headers={'X-Api-Key': RADARR_API_KEY})
    movies = response.json()
    if not movies:
        # Not in Radarr yet - look up release date from Radarr's TMDb cache
        lookup = requests.get(f"{RADARR_URL}/movie/lookup/tmdb?tmdbId={tmdb_id}", headers={'X-Api-Key': RADARR_API_KEY})
        release_date = format_date(lookup.json().get('digitalRelease')) if lookup.status_code == 200 else ""
        return False, None, release_date
    movie = movies[0]
    status = "Downloaded" if movie.get('hasFile') else "Monitored"
    return True, status, format_date(movie.get('digitalRelease'))


# Returns (in_sonarr, status_str, first_aired_date). status_str is None if not in Sonarr.
def get_sonarr_status(tmdb_id):
    lookup = requests.get(f"{SONARR_URL}/series/lookup?term=tmdb:{tmdb_id}", headers={'X-Api-Key': SONARR_API_KEY}).json()
    if not lookup:
        print(f"Could not find series with tmdbId {tmdb_id}")
        return True, "Not Found", ""
    first_aired = format_date(lookup[0].get('firstAired'))
    tvdb_id = lookup[0]['tvdbId']
    series = requests.get(f"{SONARR_URL}/series?tvdbId={tvdb_id}", headers={'X-Api-Key': SONARR_API_KEY}).json()
    if not series:
        return False, None, first_aired
    stats = series[0].get('statistics', {})
    pct = stats.get('percentOfEpisodes', 0)
    if pct == 100:
        return True, "Downloaded", first_aired
    elif pct > 0:
        return True, f"Partial ({pct:.0f}%)", first_aired
    return True, "Monitored", first_aired


# Add movie to Radarr
def add_to_radarr(tmdb_id):
    payload = {
        "qualityProfileId": RADARR_QUALITY_PROFILE,
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
            "qualityProfileId": SONARR_QUALITY_PROFILE,
            "tvdbId": int(show_data['tvdbId']),
            "rootFolderPath": SONARR_ROOT_FOLDER_PATH,
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
    sheets_service = build_sheets_service()
    links = get_google_sheets_data(sheets_service)
    rows = []

    for link in links:
        if not link:
            rows.append(["", ""])
            continue

        url = link[0]
        tmdb_id = get_tmdb_id(url)
        if not tmdb_id:
            rows.append(["", ""])
            continue

        if 'themoviedb.org/movie/' in url:
            in_radarr, status, release_date = get_radarr_status(tmdb_id)
            if not in_radarr:
                if add_to_radarr(tmdb_id):
                    print(f"Added movie with TMDb ID {tmdb_id} to Radarr")
                    status = "Monitored"
                else:
                    print(f"Failed to add movie with TMDb ID {tmdb_id} to Radarr")
                    status = "Failed to Add"
            else:
                print(f"Movie with TMDb ID {tmdb_id} is already in Radarr ({status})")
        elif 'themoviedb.org/tv/' in url:
            in_sonarr, status, release_date = get_sonarr_status(tmdb_id)
            if not in_sonarr:
                if add_to_sonarr(tmdb_id):
                    print(f"Added show with TMDb ID {tmdb_id} to Sonarr")
                    status = "Monitored"
                else:
                    print(f"Failed to add show with TMDb ID {tmdb_id} to Sonarr")
                    status = "Failed to Add"
            else:
                print(f"Show with TMDb ID {tmdb_id} is already in Sonarr ({status})")
        else:
            status = ""
            release_date = ""

        rows.append([status, release_date])

    if rows:
        update_sheet_statuses(sheets_service, rows)


if __name__ == "__main__":
    main()
