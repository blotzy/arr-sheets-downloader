# arr-sheets-downloader

Reads a Google Sheet containing TMDb and GoodReads links, adds any untracked items to Radarr, Sonarr, and/or LazyLibrarian, and writes the current download status back to the sheet.

This is an alternative to things like Seerr without having to host/protect public services.

## How it works

Each row in your spreadsheet contains a URL in column A. The script reads those URLs, checks whether the item is already being tracked by the relevant \*arr app, adds it if not, and writes a status (`Monitored`, `Downloaded`, `Partial (N%)`, etc.) to column B and a release date to column C.

Supported URL types:

| URL | App |
|-----|-----|
| `themoviedb.org/movie/…` | Radarr |
| `themoviedb.org/tv/…` | Sonarr |
| `goodreads.com/book/show/…` | LazyLibrarian (ebook or audiobook tab) |
| `https://themoviedb.org/movie/…` | Radarr |
| `https://www.themoviedb.org/movie/…` | Radarr |

Items already marked `Downloaded` in the sheet are skipped without hitting any API.

## Requirements

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) (handles dependencies automatically — no manual `pip install` needed)
- A Google Sheet with TMDb/GoodReads links in column A
- The GoodReads links must be on a separate tab from TMDb content (TV and Movies can be on the same tab if desired)
- Google API credentials (API key **or** service account — see below)
- Radarr and/or Sonarr with API access
- LazyLibrarian (optional, for ebooks/audiobooks)

## Setup

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Configure the script

Copy the example config and fill in your values:

```bash
cp env.example.toml env.toml
```


### 3. Set up Google credentials

The benefit of writing the status to the sheet is that the script won't bother checking with the service if the status is already marked Downloaded.

You have two options:

**API key (read-only)** — sufficient when `write_status = false`. The script checks and adds items but does not update the sheet.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > APIs & Services > Credentials.
2. Create an API key and restrict it to the Google Sheets API.
3. Share your spreadsheet publicly (view only) or with "anyone with the link".
4. Set `api_key` in `env.toml`.

**Service account (read + write)** — required when `write_status = true` (the default). Allows the script to update columns B and C with the current status and release date.

1. Go to Google Cloud Console > APIs & Services > Credentials.
2. Create a service account and download the JSON key file.
3. Share your spreadsheet with the service account's email address (give it Editor access).
4. Set `service_account_file` in `env.toml` (and remove or comment out `api_key`).

### 4. Set up your spreadsheet

Column A of each configured range should contain URLs — one per row. Example:

| A | B | C |
|---|---|---|
| https://www.themoviedb.org/movie/550 | Downloaded | 2024-09-13 |
| https://www.themoviedb.org/tv/1396 | Monitored | 2008-01-20 |
| https://www.goodreads.com/book/show/5107.The_Road | Monitored | 2006-09-26 |

Columns B and C are written by the script — you don't need to populate them.

### 5. Find your quality profile ID

The `quality_profile` in each \*arr section is the numeric ID, not the name. To find it, open Radarr/Sonarr in your browser and go to **Settings > Profiles**. The ID appears in the URL when you click on a profile (e.g. `/settings/profiles/1`). Alternatively, hit the API directly:

```bash
curl http://localhost:7878/api/v3/qualityprofile -H "X-Api-Key: YOUR_KEY"
```

## Usage

```bash
./arr-sheets-downloader.py
```

uv automatically installs dependencies on the first run. No virtual environment setup needed.

To run on a schedule (e.g. every 5 minutes with cron):

```cron
*/5 * * * * /path/to/arr-sheets-downloader/arr-sheets-downloader.py > /var/log/arr-sheets.log 2>&1
```

## Multiple spreadsheets and ranges

By default, all \*arr apps use `[google].spreadsheet_id` and `[google].spreadsheet_range`. You can override either per-service:

```toml
[radarr]
spreadsheet_id = "movies-sheet-id"
spreadsheet_range = "Movies!A2:A"

[sonarr]
spreadsheet_id = "tv-sheet-id"
spreadsheet_range = "TV!A2:A"

[lazylibrarian]
spreadsheet_id = "books-sheet-id"
```

## Status values

| Status | Meaning |
|--------|---------|
| `Monitored` | Added and being watched for a download |
| `Downloaded` | File is present in the library |
| `Partial (N%)` | Some episodes downloaded (Sonarr only) |
| `Skipped` | Item exists in LazyLibrarian but was skipped; the script will re-queue it |
| `Failed to Add` | The API call to add the item failed |
| `Not Found` | Sonarr lookup returned no results for the TMDb ID |
