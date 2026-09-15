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

## Useful commands

### Recommended commands

The commands below give the most reliable results.

```powershell
python netflix_to_floppy.py --disable-tmdb
```

This is the most stable option if you want a reproducible export without depending on TMDB matches.

```powershell
python netflix_to_floppy.py --movies-only --output floppy_movies_only.csv
```

```powershell
python netflix_to_floppy.py --series-only --output floppy_series_only.csv
```

These filters are useful for testing or isolating part of the viewing history.

### Commands with approximate results

The standard command with TMDB enrichment can produce better metadata fields, but the matches remain heuristic for some titles.

```powershell
python netflix_to_floppy.py
```

It uses TMDB if the key is available, and can sometimes make approximate matches for ambiguous titles, series, episodes, or names that differ slightly from Netflix's export.

If you want to force this mode with the full set of parameters:

```powershell
python netflix_to_floppy.py --config .env --input NetflixViewingHistory.csv --template floppy_import_template.csv --output floppy_import_from_netflix.csv --list-name "Netflix History" --list-uid "netflix-history" --tmdb-language "fr-FR"
```

Change `fr-FR` in `--tmdb-language` to the locale you want if you prefer another language.

### Additional options

To test without TMDB:

```powershell
python netflix_to_floppy.py --disable-tmdb
```

Other debug filters:

```powershell
python netflix_to_floppy.py --tv-only --output floppy_tv_only.csv
python netflix_to_floppy.py --seasons-only --output floppy_seasons_only.csv
python netflix_to_floppy.py --episodes-only --output floppy_episodes_only.csv
```

You can also use the generic filter form:

```powershell
python netflix_to_floppy.py --entry-filter episodes --output floppy_episodes_only.csv
```

The `--entry-filter`, `--movies-only`, `--series-only`, `--tv-only`, `--seasons-only`, and `--episodes-only` filters are mutually exclusive: only one of them can be used at a time.

## Notes on accuracy

Movie exports usually work relatively well because the title match is straightforward and Floppy can often map them automatically.

Series are more fragile in this script. Episode and season handling is based on the title text, and Netflix does not always include the episode number in the export. Sometimes it only includes the episode name, which makes automated matching harder and can lead to approximate results or missing episode-level details.

## Import into Floppy

Use Floppy's CSV import and select `floppy_import_from_netflix.csv`.

## Contributing

Forks are welcome and encouraged. If you have an idea, improvement, or fix, feel free to fork the project and propose a contribution back.
