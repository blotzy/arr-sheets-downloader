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
from dataclasses import dataclass

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# --- Config dataclasses ---

@dataclass
class GoogleConfig:
    spreadsheet_id: str
    range: str
    write_status: bool
    api_key: str | None = None
    service_account_file: str | None = None
    ebooks_range: str | None = None
    audiobooks_range: str | None = None

@dataclass
class RadarrConfig:
    api_key: str
    url: str
    quality_profile: int
    root_folder_path: str
    spreadsheet_id: str
    range: str

@dataclass
class SonarrConfig:
    api_key: str
    url: str
    quality_profile: int
    root_folder_path: str
    spreadsheet_id: str
    range: str

@dataclass
class LazyLibrarianConfig:
    api_key: str | None
    url: str | None
    spreadsheet_id: str


# --- Load config ---

_script_dir = os.path.dirname(os.path.abspath(__file__))
_config_path = os.path.join(_script_dir, 'env.toml')
if not os.path.exists(_config_path):
    raise FileNotFoundError("env.toml not found. Copy env.example.toml to env.toml and fill in your values.")
with open(_config_path, 'rb') as f:
    _config = tomllib.load(f)

google_cfg = GoogleConfig(
    api_key=_config['google'].get('api_key'),
    service_account_file=_config['google'].get('service_account_file'),
    spreadsheet_id=_config['google']['spreadsheet_id'],
    range=_config['google']['spreadsheet_range'],
    ebooks_range=_config['google'].get('ebooks_range'),
    audiobooks_range=_config['google'].get('audiobooks_range'),
    write_status=_config['google'].get('write_status', True),
)

radarr_cfg = RadarrConfig(
    api_key=_config['radarr']['api_key'],
    url=_config['radarr']['url'],
    quality_profile=_config['radarr']['quality_profile'],
    root_folder_path=_config['radarr']['root_folder_path'],
    spreadsheet_id=_config['radarr'].get('spreadsheet_id', google_cfg.spreadsheet_id),
    range=_config['radarr'].get('spreadsheet_range', google_cfg.range),
)

sonarr_cfg = SonarrConfig(
    api_key=_config['sonarr']['api_key'],
    url=_config['sonarr']['url'],
    quality_profile=_config['sonarr']['quality_profile'],
    root_folder_path=_config['sonarr']['root_folder_path'],
    spreadsheet_id=_config['sonarr'].get('spreadsheet_id', google_cfg.spreadsheet_id),
    range=_config['sonarr'].get('spreadsheet_range', google_cfg.range),
)

_ll = _config.get('lazylibrarian', {})
ll_cfg = LazyLibrarianConfig(
    api_key=_ll.get('api_key'),
    url=_ll.get('url'),
    spreadsheet_id=_ll.get('spreadsheet_id', google_cfg.spreadsheet_id),
)

radarr_session = requests.Session()
radarr_session.headers.update({'X-Api-Key': radarr_cfg.api_key})

sonarr_session = requests.Session()
sonarr_session.headers.update({'X-Api-Key': sonarr_cfg.api_key})

lazylibrarian_session = requests.Session()


# --- Google Sheets ---

def build_sheets_service():
    if google_cfg.service_account_file:
        # Read+write scope so we can update the status column
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        credentials = service_account.Credentials.from_service_account_file(
            google_cfg.service_account_file, scopes=scopes)
        return build('sheets', 'v4', credentials=credentials)
    elif google_cfg.api_key:
        return build('sheets', 'v4', developerKey=google_cfg.api_key)
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
            spreadsheetId=spreadsheet_id or google_cfg.spreadsheet_id,
            range=get_read_range(range_name)).execute()
        return result.get('values', [])
    except HttpError as error:
        print(f"Failed to read sheet: {error}")
        raise


def format_date(date_str):
    # Trim ISO timestamp to just the date portion, e.g. "2024-03-15T00:00:00Z" -> "2024-03-15"
    return date_str[:10] if date_str else ""


def update_sheet_statuses(service, rows, range_name, spreadsheet_id=None):
    # rows is a list of [status, release_date] pairs
    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id or google_cfg.spreadsheet_id,
            range=get_output_range(range_name),
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()
    except HttpError as error:
        if google_cfg.api_key and not google_cfg.service_account_file:
            raise RuntimeError("Writing to sheets requires a service account, not an API key.") from error
        raise


# --- TMDb / Radarr / Sonarr ---

def get_tmdb_id(url):
    if 'themoviedb.org/movie/' in url:
        return url.split('/movie/')[1].split('-')[0]
    elif 'themoviedb.org/tv/' in url:
        return url.split('/tv/')[1].split('-')[0]
    return None


# Returns (in_radarr, status_str, digital_release_date). status_str is None if not in Radarr.
def get_radarr_status(tmdb_id):
    response = radarr_session.get(f"{radarr_cfg.url}/movie?tmdbId={tmdb_id}")
    movies = response.json()
    if not movies:
        # Not in Radarr yet - look up release date from Radarr's TMDb cache
        lookup = radarr_session.get(f"{radarr_cfg.url}/movie/lookup/tmdb?tmdbId={tmdb_id}")
        release_date = format_date(lookup.json().get('digitalRelease')) if lookup.status_code == 200 else ""
        return False, None, release_date
    movie = movies[0]
    status = "Downloaded" if movie.get('hasFile') else "Monitored"
    return True, status, format_date(movie.get('digitalRelease'))


# Returns (in_sonarr, status_str, first_aired_date). status_str is None if not in Sonarr.
def get_sonarr_status(tmdb_id):
    lookup = sonarr_session.get(f"{sonarr_cfg.url}/series/lookup?term=tmdb:{tmdb_id}").json()
    if not lookup:
        print(f"Could not find series with tmdbId {tmdb_id}")
        return True, "Not Found", ""
    first_aired = format_date(lookup[0].get('firstAired'))
    tvdb_id = lookup[0]['tvdbId']
    series = sonarr_session.get(f"{sonarr_cfg.url}/series?tvdbId={tvdb_id}").json()
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
        "qualityProfileId": radarr_cfg.quality_profile,
        "tmdbId": int(tmdb_id),
        "rootFolderPath": radarr_cfg.root_folder_path,
        "monitored": True,
        "addOptions": {
            "searchForMovie": True
        }
    }
    add_response = radarr_session.post(f"{radarr_cfg.url}/movie", json=payload)
    return add_response.status_code == 201


def add_to_sonarr(tmdb_id):
    response = sonarr_session.get(f"{sonarr_cfg.url}/series/lookup?term=tmdb:{tmdb_id}")
    if response.status_code == 200 and response.json():
        show_data = response.json()[0]
        payload = {
            "title": show_data['title'],
            "qualityProfileId": sonarr_cfg.quality_profile,
            "tvdbId": int(show_data['tvdbId']),
            "rootFolderPath": sonarr_cfg.root_folder_path,
            "monitored": True,
            "addOptions": {
                "searchForMissingEpisodes": True
            }
        }
        add_response = sonarr_session.post(f"{sonarr_cfg.url}/series", json=payload)
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
        f"{ll_cfg.url}/api",
        params={'apikey': ll_cfg.api_key, 'cmd': 'getAllBooks'}
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


def _ll_api(params):
    lazylibrarian_session.get(f"{ll_cfg.url}/api",
                              params={'apikey': ll_cfg.api_key, **params})


def resume_authors(goodreads_id):
    """Resume any Paused authors associated with a book."""
    resp = lazylibrarian_session.get(
        f"{ll_cfg.url}/api",
        params={'apikey': ll_cfg.api_key, 'cmd': 'getBookAuthors', 'id': goodreads_id}
    )
    if resp.status_code == 200:
        for author in resp.json():
            author_id = author.get('AuthorID')
            if author_id:
                _ll_api({'cmd': 'resumeAuthor', 'id': author_id})


def want_and_search_lazylibrarian(goodreads_id, book_type):
    ll_type = 'AudioBook' if book_type == 'audiobook' else 'eBook'
    _ll_api({'cmd': 'queueBook', 'id': goodreads_id, 'type': ll_type})
    _ll_api({'cmd': 'searchBook', 'id': goodreads_id, 'type': ll_type})


def add_to_lazylibrarian(goodreads_id, book_type):
    response = lazylibrarian_session.get(
        f"{ll_cfg.url}/api",
        params={'apikey': ll_cfg.api_key, 'cmd': 'addBook', 'id': goodreads_id}
    )
    if response.status_code != 200:
        return False
    resume_authors(goodreads_id)
    want_and_search_lazylibrarian(goodreads_id, book_type)
    return True


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
        if status == "Downloaded":
            print(f"{book_type.capitalize()} with GoodReads ID {goodreads_id} is Downloaded")
        elif not in_ll:
            if add_to_lazylibrarian(goodreads_id, book_type):
                print(f"Added {book_type} with GoodReads ID {goodreads_id} to LazyLibrarian")
                status = "Monitored"
            else:
                print(f"Failed to add {book_type} with GoodReads ID {goodreads_id} to LazyLibrarian")
                status = "Failed to Add"
        elif status == "Skipped":
            # Book exists but Skipped — resume the author then re-queue and search
            author_id = ll_books.get(str(goodreads_id), {}).get('AuthorID')
            if author_id:
                _ll_api({'cmd': 'resumeAuthor', 'id': author_id})
            want_and_search_lazylibrarian(goodreads_id, book_type)
            print(f"Re-queued {book_type} with GoodReads ID {goodreads_id} (was Skipped)")
            status = "Monitored"
        else:
            print(f"{book_type.capitalize()} with GoodReads ID {goodreads_id} is in LazyLibrarian ({status})")

        rows.append([status, pub_date])

    if rows and google_cfg.write_status:
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

    if rows and google_cfg.write_status:
        update_sheet_statuses(sheets_service, rows, range_name, spreadsheet_id)


# Main function
def main():
    sheets_service = build_sheets_service()

    # Movies and TV shows
    if radarr_cfg.spreadsheet_id == sonarr_cfg.spreadsheet_id and radarr_cfg.range == sonarr_cfg.range:
        # Same spreadsheet and range: process both URL types in one read/write pass
        process_media_tab(sheets_service, radarr_cfg.range, radarr_cfg.spreadsheet_id)
    else:
        # Different spreadsheets or ranges: process each separately
        process_media_tab(sheets_service, radarr_cfg.range, radarr_cfg.spreadsheet_id, url_type='movie')
        process_media_tab(sheets_service, sonarr_cfg.range, sonarr_cfg.spreadsheet_id, url_type='tv')

    # Ebooks and audiobooks
    if ll_cfg.url and ll_cfg.api_key:
        ll_books = fetch_lazylibrarian_books()
        if google_cfg.ebooks_range:
            process_books_tab(sheets_service, google_cfg.ebooks_range, 'ebook', ll_books, ll_cfg.spreadsheet_id)
        if google_cfg.audiobooks_range:
            process_books_tab(sheets_service, google_cfg.audiobooks_range, 'audiobook', ll_books, ll_cfg.spreadsheet_id)


if __name__ == "__main__":
    main()
