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
import re
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
EBOOKS_RANGE = config['google'].get('ebooks_range')
AUDIOBOOKS_RANGE = config['google'].get('audiobooks_range')

RADARR_API_KEY = config['radarr']['api_key']
RADARR_URL = config['radarr']['url']
RADARR_QUALITY_PROFILE = config['radarr']['quality_profile']
RADARR_ROOT_FOLDER_PATH = config['radarr']['root_folder_path']
RADARR_SPREADSHEET_ID = config['radarr'].get('spreadsheet_id', SPREADSHEET_ID)

SONARR_API_KEY = config['sonarr']['api_key']
SONARR_URL = config['sonarr']['url']
SONARR_QUALITY_PROFILE = config['sonarr']['quality_profile']
SONARR_ROOT_FOLDER_PATH = config['sonarr']['root_folder_path']
SONARR_SPREADSHEET_ID = config['sonarr'].get('spreadsheet_id', SPREADSHEET_ID)

_ll_config = config.get('lazylibrarian', {})
LAZYLIBRARIAN_API_KEY = _ll_config.get('api_key')
LAZYLIBRARIAN_URL = _ll_config.get('url')
LAZYLIBRARIAN_SPREADSHEET_ID = _ll_config.get('spreadsheet_id', SPREADSHEET_ID)

radarr_session = requests.Session()
radarr_session.headers.update({'X-Api-Key': RADARR_API_KEY})

sonarr_session = requests.Session()
sonarr_session.headers.update({'X-Api-Key': SONARR_API_KEY})

lazylibrarian_session = requests.Session()


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


def get_read_range(range_name):
    # Read URL (A), current status (B), and current date (C) in one call
    # e.g. "Sheet1!A2:A" -> "Sheet1!A2:C"
    sheet_name, cell_part = range_name.split('!')
    start_row = ''.join(c for c in cell_part.split(':')[0] if c.isdigit())
    return f"{sheet_name}!A{start_row}:C"


def get_output_range(range_name):
    # Derive the 2-column output range (B=status, C=release date)
    # e.g. "Sheet1!A2:A" -> "Sheet1!B2:C"
    sheet_name, cell_part = range_name.split('!')
    start_row = ''.join(c for c in cell_part.split(':')[0] if c.isdigit())
    return f"{sheet_name}!B{start_row}:C"


def get_google_sheets_data(service, range_name, spreadsheet_id=None):
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id or SPREADSHEET_ID,
            range=get_read_range(range_name)).execute()
        return result.get('values', [])
    except HttpError as error:
        print(f"An error occurred: {error}")
        return []


def format_date(date_str):
    # Trim ISO timestamp to just the date portion, e.g. "2024-03-15T00:00:00Z" -> "2024-03-15"
    return date_str[:10] if date_str else ""


def update_sheet_statuses(service, rows, range_name, spreadsheet_id=None):
    # rows is a list of [status, release_date] pairs
    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id or SPREADSHEET_ID,
            range=get_output_range(range_name),
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()
    except HttpError as error:
        print(f"Failed to update status column: {error}")
        if GOOGLE_SHEETS_API_KEY and not GOOGLE_SERVICE_ACCOUNT_FILE:
            print("Note: writing to sheets requires a service account, not an API key.")


# --- TMDb / Radarr / Sonarr ---

def get_tmdb_id(url):
    if 'themoviedb.org/movie/' in url:
        return url.split('/movie/')[1].split('-')[0]
    elif 'themoviedb.org/tv/' in url:
        return url.split('/tv/')[1].split('-')[0]
    return None


# Returns (in_radarr, status_str, digital_release_date). status_str is None if not in Radarr.
def get_radarr_status(tmdb_id):
    response = radarr_session.get(f"{RADARR_URL}/movie?tmdbId={tmdb_id}")
    movies = response.json()
    if not movies:
        # Not in Radarr yet - look up release date from Radarr's TMDb cache
        lookup = radarr_session.get(f"{RADARR_URL}/movie/lookup/tmdb?tmdbId={tmdb_id}")
        release_date = format_date(lookup.json().get('digitalRelease')) if lookup.status_code == 200 else ""
        return False, None, release_date
    movie = movies[0]
    status = "Downloaded" if movie.get('hasFile') else "Monitored"
    return True, status, format_date(movie.get('digitalRelease'))


# Returns (in_sonarr, status_str, first_aired_date). status_str is None if not in Sonarr.
def get_sonarr_status(tmdb_id):
    lookup = sonarr_session.get(f"{SONARR_URL}/series/lookup?term=tmdb:{tmdb_id}").json()
    if not lookup:
        print(f"Could not find series with tmdbId {tmdb_id}")
        return True, "Not Found", ""
    first_aired = format_date(lookup[0].get('firstAired'))
    tvdb_id = lookup[0]['tvdbId']
    series = sonarr_session.get(f"{SONARR_URL}/series?tvdbId={tvdb_id}").json()
    if not series:
        return False, None, first_aired
    stats = series[0].get('statistics', {})
    pct = stats.get('percentOfEpisodes', 0)
    if pct == 100:
        return True, "Downloaded", first_aired
    elif pct > 0:
        return True, f"Partial ({pct:.0f}%)", first_aired
    return True, "Monitored", first_aired


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
    add_response = radarr_session.post(f"{RADARR_URL}/movie", json=payload)
    return add_response.status_code == 201


def add_to_sonarr(tmdb_id):
    response = sonarr_session.get(f"{SONARR_URL}/series/lookup?term=tmdb:{tmdb_id}")
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
        add_response = sonarr_session.post(f"{SONARR_URL}/series", json=payload)
        print(add_response.json())
        return add_response.status_code == 201
    return False


# --- GoodReads / LazyLibrarian ---

def get_goodreads_id(url):
    if 'goodreads.com/book/show/' in url:
        id_part = url.split('/book/show/')[1]
        return re.split(r'[-\.?/]', id_part)[0]
    return None


def fetch_lazylibrarian_books():
    """Fetch all books from LazyLibrarian once, keyed by GoodReads BookID."""
    response = lazylibrarian_session.get(
        f"{LAZYLIBRARIAN_URL}/api",
        params={'apikey': LAZYLIBRARIAN_API_KEY, 'cmd': 'getAllBooks'}
    )
    if response.status_code != 200:
        return {}
    data = response.json()
    books = data if isinstance(data, list) else data.get('books', [])
    return {str(b['BookID']): b for b in books if b.get('BookID')}


# Returns (in_ll, status_str, pub_date). status_str is None if not in LazyLibrarian.
def get_book_status(goodreads_id, book_type, ll_books):
    book = ll_books.get(str(goodreads_id))
    if not book:
        return False, None, ""
    pub_date = format_date(book.get('BookDate', ''))
    library_field = 'AudioLibrary' if book_type == 'audiobook' else 'BookLibrary'
    status_field = 'AudioStatus' if book_type == 'audiobook' else 'Status'
    if book.get(library_field):
        return True, 'Downloaded', pub_date
    ll_status = book.get(status_field, '')
    if ll_status == 'Skipped':
        return True, 'Skipped', pub_date
    return True, 'Monitored', pub_date


def add_to_lazylibrarian(goodreads_id):
    response = lazylibrarian_session.get(
        f"{LAZYLIBRARIAN_URL}/api",
        params={'apikey': LAZYLIBRARIAN_API_KEY, 'cmd': 'addBook', 'id': goodreads_id}
    )
    return response.status_code == 200


def process_books_tab(sheets_service, range_name, book_type, ll_books, spreadsheet_id=None):
    links = get_google_sheets_data(sheets_service, range_name, spreadsheet_id)
    rows = []

    for row_data in links:
        url = row_data[0] if row_data else ""
        current_status = row_data[1] if len(row_data) > 1 else ""
        current_date = row_data[2] if len(row_data) > 2 else ""

        if not url:
            rows.append(["", ""])
            continue

        if current_status == "Downloaded":
            rows.append([current_status, current_date])
            continue

        goodreads_id = get_goodreads_id(url)
        if not goodreads_id:
            rows.append(["", ""])
            continue

        in_ll, status, pub_date = get_book_status(goodreads_id, book_type, ll_books)
        if not in_ll:
            if add_to_lazylibrarian(goodreads_id):
                print(f"Added {book_type} with GoodReads ID {goodreads_id} to LazyLibrarian")
                ll_books[str(goodreads_id)] = {}
                status = "Monitored"
            else:
                print(f"Failed to add {book_type} with GoodReads ID {goodreads_id} to LazyLibrarian")
                status = "Failed to Add"
        else:
            print(f"{book_type.capitalize()} with GoodReads ID {goodreads_id} is in LazyLibrarian ({status})")

        rows.append([status, pub_date])

    if rows:
        update_sheet_statuses(sheets_service, rows, range_name, spreadsheet_id)


def process_media_tab(sheets_service, range_name, spreadsheet_id, url_type=None):
    """Process movie/TV rows. url_type: 'movie', 'tv', or None (both)."""
    links = get_google_sheets_data(sheets_service, range_name, spreadsheet_id)
    rows = []

    for row_data in links:
        url = row_data[0] if row_data else ""
        current_status = row_data[1] if len(row_data) > 1 else ""
        current_date = row_data[2] if len(row_data) > 2 else ""

        if not url:
            rows.append(["", ""])
            continue

        if current_status == "Downloaded":
            rows.append([current_status, current_date])
            continue

        tmdb_id = get_tmdb_id(url)
        if not tmdb_id:
            rows.append(["", ""])
            continue

        if 'themoviedb.org/movie/' in url and url_type != 'tv':
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
            rows.append([status, release_date])
        elif 'themoviedb.org/tv/' in url and url_type != 'movie':
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
            rows.append([status, release_date])
        else:
            rows.append(["", ""])

    if rows:
        update_sheet_statuses(sheets_service, rows, range_name, spreadsheet_id)


# Main function
def main():
    sheets_service = build_sheets_service()

    # Movies and TV shows
    if RADARR_SPREADSHEET_ID == SONARR_SPREADSHEET_ID:
        # Same spreadsheet: process both URL types in one read/write pass
        process_media_tab(sheets_service, RANGE_NAME, RADARR_SPREADSHEET_ID)
    else:
        # Different spreadsheets: process each separately
        process_media_tab(sheets_service, RANGE_NAME, RADARR_SPREADSHEET_ID, url_type='movie')
        process_media_tab(sheets_service, RANGE_NAME, SONARR_SPREADSHEET_ID, url_type='tv')

    # Ebooks and audiobooks
    if LAZYLIBRARIAN_URL and LAZYLIBRARIAN_API_KEY:
        ll_books = fetch_lazylibrarian_books()
        if EBOOKS_RANGE:
            process_books_tab(sheets_service, EBOOKS_RANGE, 'ebook', ll_books, LAZYLIBRARIAN_SPREADSHEET_ID)
        if AUDIOBOOKS_RANGE:
            process_books_tab(sheets_service, AUDIOBOOKS_RANGE, 'audiobook', ll_books, LAZYLIBRARIAN_SPREADSHEET_ID)


if __name__ == "__main__":
    main()
