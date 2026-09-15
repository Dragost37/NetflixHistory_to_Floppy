# Netflix to Floppy

This project converts Netflix viewing history (`NetflixViewingHistory.csv`) into a Floppy-compatible CSV import.

## Simple setup

1. Open or create the `.env` file.
2. (Required) Add your TMDB API key:

```env
TMDB_API_KEY=YOUR_TMDB_API_KEY
```

An example file is available at `.env.example`.

## What the script does

- Reads your Netflix export (`Title`, `Date`)
- Keeps each Netflix viewing entry as its own line
- Detects whether each item is a `movie` or `tv` title
- Enriches titles from TMDB when possible, including:
  - TMDB ID
  - TMDB page URL
  - poster image
  - localized title
  - rating
  - runtime / watch duration
- Refines the final type using TMDB results
- Preserves individual episodes instead of collapsing them into a single series row
- Writes a CSV that matches the Floppy template columns:
  - 1 `list` row
  - 1 `media` row per viewing entry
  - 1 `list_item` row per viewing entry

## Run it

From the project folder:

```powershell
python netflix_to_floppy.py
```

The default output file is:

- `floppy_import_from_netflix.csv`

## Useful options

```powershell
python netflix_to_floppy.py --config .env --input NetflixViewingHistory.csv --template floppy_import_template.csv --output floppy_import_from_netflix.csv --list-name "Netflix History" --list-uid "netflix-history" --tmdb-language "fr-FR"
```

To test without TMDB:

```powershell
python netflix_to_floppy.py --disable-tmdb
```

To export only movies:

```powershell
python netflix_to_floppy.py --movies-only --output floppy_movies_only.csv
```

To export only series-related entries:

```powershell
python netflix_to_floppy.py --series-only --output floppy_series_only.csv
```

Other useful debug filters:

```powershell
python netflix_to_floppy.py --tv-only --output floppy_tv_only.csv
python netflix_to_floppy.py --seasons-only --output floppy_seasons_only.csv
python netflix_to_floppy.py --episodes-only --output floppy_episodes_only.csv
```

You can also use the generic filter form:

```powershell
python netflix_to_floppy.py --entry-filter episodes --output floppy_episodes_only.csv
```

## Import into Floppy

Use Floppy's CSV import and select `floppy_import_from_netflix.csv`.

## Note about `.clz` / Floppy backups

This script generates a Floppy import CSV based on the provided template. The `.clz` format or internal Floppy backup format is proprietary and not documented here, so this converter targets the most reliable path: CSV import.
