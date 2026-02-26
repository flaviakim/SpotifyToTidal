#!/usr/bin/env python3
"""
spotify_to_tidal.py
────────────────────────────────────────────────────────────────────
Import a Spotify playlist export (CSV) into a new TIDAL playlist.

Supported CSV format (exported by tools like Exportify):
  Track URI, Track Name, Artist URI(s), Artist Name(s), Album URI,
  Album Name, Album Artist URI(s), Album Artist Name(s),
  Album Release Date, Album Image URL, Disc Number, Track Number,
  Track Duration (ms), Track Preview URL, Explicit, Popularity,
  ISRC, Added By, Added At

Requirements:
  pip install tidalapi requests pygame rich

Usage:
  python spotify_to_tidal.py <path_to_csv>
  python spotify_to_tidal.py <path_to_csv> --name "My Playlist"
  python spotify_to_tidal.py <path_to_csv> --session-file tidal_session.json
  python spotify_to_tidal.py --folder <path_to_folder>
  python spotify_to_tidal.py --folder <path_to_folder> --session-file tidal_session.json
────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import tempfile
import time
import warnings
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── third-party ──────────────────────────────────────────────────
try:
    import tidalapi
except ImportError:
    sys.exit("❌  tidalapi not found. Run: pip install tidalapi")

try:
    import requests
except ImportError:
    sys.exit("❌  requests not found. Run: pip install requests")

try:
    import pygame
    _PYGAME_AVAILABLE = True
except ImportError:
    _PYGAME_AVAILABLE = False

# term-image warning must be filtered before the module loads
warnings.filterwarnings("ignore", category=UserWarning, message=".*not running within a terminal.*")
try:
    from term_image.image import AutoImage, from_url  # type: ignore
    _TERM_IMAGE_AVAILABLE = True
except ImportError:
    _TERM_IMAGE_AVAILABLE = False

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.prompt import Prompt, Confirm
    from rich import print as rprint
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────
# Minimal fallback console when rich is not installed
# ─────────────────────────────────────────────────────────────────
class _FallbackConsole:
    def print(self, *args, **kwargs):
        # strip rich markup for plain output
        import re
        text = " ".join(str(a) for a in args)
        text = re.sub(r"\[.*?\]", "", text)
        print(text)

    def rule(self, title=""):
        print(f"\n{'─' * 60}  {title}")

    def log(self, *args, **kwargs):
        self.print(*args)


if _RICH_AVAILABLE:
    console = Console()
else:
    console = _FallbackConsole()


# ─────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────
@dataclass
class SpotifyTrack:
    name: str
    artists: str
    album: str
    isrc: str
    preview_url: str
    image_url: str
    duration_ms: int
    explicit: bool

    def display_name(self) -> str:
        return f"{self.name} — {self.artists}"


@dataclass
class ImportResult:
    spotify_track: SpotifyTrack
    tidal_track: Optional[object] = None  # tidalapi.Track
    status: str = "pending"  # pending | added | skipped | not_found


# ─────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────
def load_csv(path: Path) -> list[SpotifyTrack]:
    """Parse the Spotify export CSV and return a list of SpotifyTrack objects."""
    tracks: list[SpotifyTrack] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter="\t")

        # Support both tab- and comma-delimited exports
        if reader.fieldnames and len(reader.fieldnames) == 1:
            fh.seek(0)
            reader = csv.DictReader(fh, delimiter=",")

        for row in reader:
            # Normalise header names (strip whitespace)
            row = {k.strip(): v.strip() if v else "" for k, v in row.items()}

            isrc = row.get("ISRC", "").strip()
            name = row.get("Track Name", "Unknown")
            artists = row.get("Artist Name(s)", "Unknown")
            album = row.get("Album Name", "Unknown")
            preview_url = row.get("Track Preview URL", "")
            image_url = row.get("Album Image URL", "")
            explicit = row.get("Explicit", "False").lower() in ("true", "yes", "1")

            try:
                duration_ms = int(row.get("Track Duration (ms)", 0))
            except ValueError:
                duration_ms = 0

            tracks.append(SpotifyTrack(
                name=name,
                artists=artists,
                album=album,
                isrc=isrc,
                preview_url=preview_url,
                image_url=image_url,
                duration_ms=duration_ms,
                explicit=explicit,
            ))

    return tracks


def discover_csv_files(folder: Path) -> list[Path]:
    """Return all CSV files found directly within the given folder (non-recursive)."""
    csv_files = sorted(folder.glob("*.csv"))
    return csv_files


# ─────────────────────────────────────────────────────────────────
# TIDAL session
# ─────────────────────────────────────────────────────────────────
def get_tidal_session(session_file: Optional[str] = None) -> tidalapi.Session:
    """Authenticate with TIDAL, reusing a stored session when available.

    login_session_file() handles both cases transparently:
    - If the session file exists and is valid, it restores the session.
    - If the file doesn't exist or the session has expired, it performs a
      fresh OAuth login and writes the new session to the file.
    """
    session = tidalapi.Session()
    session_path = Path(session_file) if session_file else Path("tidal_session.json")

    console.print("\n[bold cyan]🎵  TIDAL Login[/bold cyan]")
    if session_path.exists():
        console.print(f"[cyan]🔑  Found session file {session_path}, attempting restore…[/cyan]")
    else:
        console.print("No saved session found. A browser link will appear — open it and log in.")
    console.print("─" * 60)

    def _print(msg: str) -> None:
        console.print(f"[cyan]{msg}[/cyan]")

    success = session.login_session_file(session_path, fn_print=_print)

    if success and session.check_login():
        console.print("[green]✅  Logged in successfully.[/green]")
    else:
        sys.exit("❌  TIDAL login failed. Please try again.")

    return session


# ─────────────────────────────────────────────────────────────────
# TIDAL search helpers
# ─────────────────────────────────────────────────────────────────
def find_tidal_track(
    session: tidalapi.Session,
    track: SpotifyTrack,
    retry_delay: float = 0.3,
) -> Optional[object]:
    """
    Look up a track on TIDAL, preferring ISRC match.
    Falls back to title + artist text search.
    """
    # 1. ISRC lookup (exact match)
    if track.isrc:
        try:
            results = session.get_tracks_by_isrc(track.isrc)
            if results:
                return results[0]
        except Exception:
            pass

    # Throttle to avoid rate-limiting
    time.sleep(retry_delay)

    # 2. Text search fallback
    query = f"{track.name} {track.artists.split(',')[0]}"
    try:
        results = session.search(query, models=[tidalapi.Track], limit=5)
        hits: list = results.get("tracks", [])
        if hits:
            return hits[0]
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────────────────────────
# Media / UI helpers
# ─────────────────────────────────────────────────────────────────
def display_cover_art(image_url: str) -> None:
    """Download and render album cover art inline in the terminal (requires term-image).

    The image is scaled to the full current terminal width.
    """
    if not image_url:
        return

    if not _TERM_IMAGE_AVAILABLE:
        console.print("  [dim](install term-image to see cover art inline: pip install term-image)[/dim]")
        return

    try:
        term_img = from_url(image_url)
        term_img.draw()
    except Exception as exc:
        console.print(f"  [dim]Could not display cover art: {exc}[/dim]")


def play_preview(url: str, duration_s: float = 30.0) -> None:
    """Stream and play a 30-second preview snippet (requires pygame)."""
    if not url:
        console.print("  [dim]No preview available.[/dim]")
        return

    if not _PYGAME_AVAILABLE:
        console.print(f"  [dim]Preview URL (install pygame to play): {url}[/dim]")
        return

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        pygame.mixer.init()
        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()

        console.print(f"  [green]▶  Playing preview… (press Enter to stop)[/green]")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
            os.unlink(tmp_path)

    except Exception as exc:
        console.print(f"  [yellow]Could not play preview: {exc}[/yellow]")


# ─────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────
def print_track_list(tracks: list[SpotifyTrack]) -> None:
    """Print a summary table of all tracks in the CSV."""
    if _RICH_AVAILABLE:
        table = Table(
            title=f"📋  Playlist – {len(tracks)} tracks",
            show_lines=False,
            header_style="bold cyan",
        )
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Track", style="bold white", no_wrap=False, max_width=40)
        table.add_column("Artist(s)", style="cyan", max_width=30)
        table.add_column("Album", style="dim", max_width=30)
        table.add_column("ISRC", style="dim", width=14)
        table.add_column("E", justify="center", width=3)

        for i, t in enumerate(tracks, 1):
            table.add_row(
                str(i),
                t.name,
                t.artists,
                t.album,
                t.isrc or "—",
                "🔞" if t.explicit else "",
            )
        console.print(table)
    else:
        print(f"\n{'#':>4}  {'Track':<40} {'Artist':<30} ISRC")
        print("─" * 100)
        for i, t in enumerate(tracks, 1):
            print(f"{i:>4}  {t.name[:39]:<40} {t.artists[:29]:<30} {t.isrc}")


def print_results_summary(results: list[ImportResult]) -> None:
    """Print the final import summary."""
    added = [r for r in results if r.status == "added"]
    skipped = [r for r in results if r.status == "skipped"]
    not_found = [r for r in results if r.status == "not_found"]

    console.print("\n")
    console.rule("[bold cyan]Import Summary[/bold cyan]" if _RICH_AVAILABLE else "Import Summary")

    if _RICH_AVAILABLE:
        summary = Table(show_header=False, box=None, padding=(0, 2))
        summary.add_column(style="bold")
        summary.add_column()
        summary.add_row("✅  Added", f"[green]{len(added)}[/green]")
        summary.add_row("⏭️  Skipped", f"[yellow]{len(skipped)}[/yellow]")
        summary.add_row("❌  Not found on TIDAL", f"[red]{len(not_found)}[/red]")
        console.print(summary)
    else:
        print(f"  Added:              {len(added)}")
        print(f"  Skipped by you:     {len(skipped)}")
        print(f"  Not found on TIDAL: {len(not_found)}")

    if added:
        console.print("\n[bold green]🎶  Tracks added to playlist:[/bold green]" if _RICH_AVAILABLE else "\nTracks added:")
        for r in added:
            t = r.tidal_track
            tidal_name = f"{t.name} — {t.artist.name}" if t and hasattr(t, "artist") else r.spotify_track.display_name()
            console.print(f"  [green]✓[/green]  {tidal_name}" if _RICH_AVAILABLE else f"  ✓  {tidal_name}")

    if not_found:
        console.print("\n[bold red]⚠️  Could not find on TIDAL:[/bold red]" if _RICH_AVAILABLE else "\nNot found on TIDAL:")
        for r in not_found:
            console.print(f"  [red]✗[/red]  {r.spotify_track.display_name()}" if _RICH_AVAILABLE else f"  ✗  {r.spotify_track.display_name()}")


def print_folder_summary(
    csv_files: list[Path],
    imported: list[str],
    skipped: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Print an overall summary after processing all CSVs in a folder."""
    console.print("\n")
    console.rule("[bold cyan]Folder Import Summary[/bold cyan]" if _RICH_AVAILABLE else "Folder Import Summary")

    if _RICH_AVAILABLE:
        summary = Table(show_header=False, box=None, padding=(0, 2))
        summary.add_column(style="bold")
        summary.add_column()
        summary.add_row("📁  Total CSVs found", str(len(csv_files)))
        summary.add_row("✅  Imported", f"[green]{len(imported)}[/green]")
        summary.add_row("⏭️  Skipped by user", f"[yellow]{len(skipped)}[/yellow]")
        summary.add_row("❌  Failed", f"[red]{len(failed)}[/red]")
        console.print(summary)
    else:
        print(f"  Total CSVs found: {len(csv_files)}")
        print(f"  Imported:         {len(imported)}")
        print(f"  Skipped by user:  {len(skipped)}")
        print(f"  Failed:           {len(failed)}")

    if imported:
        console.print("\n[bold green]Imported playlists:[/bold green]" if _RICH_AVAILABLE else "\nImported playlists:")
        for name in imported:
            console.print(f"  [green]✓[/green]  {name}" if _RICH_AVAILABLE else f"  ✓  {name}")

    if skipped:
        console.print("\n[bold yellow]Skipped playlists:[/bold yellow]" if _RICH_AVAILABLE else "\nSkipped playlists:")
        for name in skipped:
            console.print(f"  [yellow]–[/yellow]  {name}" if _RICH_AVAILABLE else f"  -  {name}")

    if failed:
        console.print("\n[bold red]Failed playlists:[/bold red]" if _RICH_AVAILABLE else "\nFailed playlists:")
        for name, reason in failed:
            console.print(f"  [red]✗[/red]  {name}: {reason}" if _RICH_AVAILABLE else f"  ✗  {name}: {reason}")


# ─────────────────────────────────────────────────────────────────
# Core import logic
# ─────────────────────────────────────────────────────────────────
def import_all(
    session: tidalapi.Session,
    tracks: list[SpotifyTrack],
    playlist_name: str,
) -> tuple[list[ImportResult], object]:
    """Search for all tracks on TIDAL and add them to a new playlist at once."""
    results: list[ImportResult] = []

    console.print("\n[cyan]🔍  Searching TIDAL for all tracks…[/cyan]" if _RICH_AVAILABLE else "\nSearching TIDAL…")

    if _RICH_AVAILABLE:
        progress_ctx = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        )
        with progress_ctx as progress:
            task = progress.add_task("Searching…", total=len(tracks))
            for track in tracks:
                progress.update(task, description=f"[cyan]{track.name[:50]}[/cyan]")
                tidal_track = find_tidal_track(session, track)
                results.append(ImportResult(
                    spotify_track=track,
                    tidal_track=tidal_track,
                    status="added" if tidal_track else "not_found",
                ))
                progress.advance(task)
    else:
        for i, track in enumerate(tracks, 1):
            print(f"  [{i}/{len(tracks)}] {track.name}…")
            tidal_track = find_tidal_track(session, track)
            results.append(ImportResult(
                spotify_track=track,
                tidal_track=tidal_track,
                status="added" if tidal_track else "not_found",
            ))

    # Create the playlist and add found tracks
    track_ids = [r.tidal_track.id for r in results if r.tidal_track]
    playlist = _create_and_populate_playlist(session, playlist_name, track_ids)

    return results, playlist


def import_individually(
    session: tidalapi.Session,
    tracks: list[SpotifyTrack],
    playlist_name: str,
) -> tuple[list[ImportResult], object]:
    """Let the user review each track before adding it."""
    results: list[ImportResult] = []
    total = len(tracks)

    console.print()
    for idx, track in enumerate(tracks, 1):
        console.rule(
            f"[bold cyan]Track {idx}/{total}[/bold cyan]"
            if _RICH_AVAILABLE else f"Track {idx}/{total}"
        )

        # Show track info
        if _RICH_AVAILABLE:
            console.print(Panel(
                f"[bold white]{track.name}[/bold white]\n"
                f"[cyan]Artist:[/cyan]  {track.artists}\n"
                f"[cyan]Album:[/cyan]   {track.album}\n"
                f"[cyan]ISRC:[/cyan]    {track.isrc or '—'}\n"
                f"[cyan]Duration:[/cyan] {track.duration_ms // 60000}:{(track.duration_ms % 60000) // 1000:02d}"
                + (" [red]🔞 Explicit[/red]" if track.explicit else ""),
                title=f"[bold]{idx}/{total}[/bold]",
                border_style="cyan",
            ))
        else:
            print(f"\n  {track.name}")
            print(f"  Artist:   {track.artists}")
            print(f"  Album:    {track.album}")
            print(f"  ISRC:     {track.isrc}")

        # Cover art
        if track.image_url:
            show_cover = _ask_yes_no("  Show cover art?", default=False)
            if show_cover:
                display_cover_art(track.image_url)

        # Preview snippet
        if track.preview_url:
            play = _ask_yes_no("  Play preview?", default=False)
            if play:
                play_preview(track.preview_url)

        # Search TIDAL
        console.print("  [cyan]Searching TIDAL…[/cyan]" if _RICH_AVAILABLE else "  Searching TIDAL…")
        tidal_track = find_tidal_track(session, track)

        if tidal_track:
            tidal_info = f"[green]Found:[/green] {tidal_track.name} — {tidal_track.artist.name}" if _RICH_AVAILABLE else f"Found: {tidal_track.name} — {tidal_track.artist.name}"
            console.print(f"  {tidal_info}")
            add = _ask_yes_no("  Add to playlist?", default=True)
            if add:
                results.append(ImportResult(track, tidal_track, "added"))
            else:
                results.append(ImportResult(track, tidal_track, "skipped"))
        else:
            console.print("  [red]❌  Not found on TIDAL.[/red]" if _RICH_AVAILABLE else "  Not found on TIDAL.")
            results.append(ImportResult(track, None, "not_found"))

        console.print()

    # Create playlist and add tracks
    track_ids = [r.tidal_track.id for r in results if r.status == "added" and r.tidal_track]
    playlist = _create_and_populate_playlist(session, playlist_name, track_ids)

    return results, playlist


def _create_and_populate_playlist(
    session: tidalapi.Session,
    name: str,
    track_ids: list[int],
) -> object:
    """Create a new TIDAL user playlist and add track IDs to it."""
    console.print(f"\n[cyan]📋  Creating playlist '[bold]{name}[/bold]'…[/cyan]" if _RICH_AVAILABLE else f"\nCreating playlist '{name}'…")

    playlist = session.user.create_playlist(
        name,
        f"Imported from Spotify — {len(track_ids)} tracks",
    )

    if track_ids:
        console.print(f"[cyan]➕  Adding {len(track_ids)} tracks…[/cyan]" if _RICH_AVAILABLE else f"Adding {len(track_ids)} tracks…")
        # tidalapi supports adding a list of IDs in one call
        try:
            playlist.add(track_ids)
        except Exception as exc:
            # Fallback: add one at a time if batch fails
            console.print(f"[yellow]Batch add failed ({exc}), trying one-by-one…[/yellow]" if _RICH_AVAILABLE else f"Batch add failed, trying one-by-one…")
            for tid in track_ids:
                try:
                    playlist.add([tid])
                    time.sleep(0.2)
                except Exception as inner:
                    console.print(f"[red]  Could not add track {tid}: {inner}[/red]" if _RICH_AVAILABLE else f"  Could not add track {tid}: {inner}")

    console.print(f"[green]✅  Playlist created![/green]" if _RICH_AVAILABLE else "Playlist created!")
    return playlist


# ─────────────────────────────────────────────────────────────────
# Folder import logic
# ─────────────────────────────────────────────────────────────────
def _csv_to_default_name(path: Path) -> str:
    """Derive a human-friendly playlist name from a CSV filename."""
    return path.stem.replace("_", " ").replace("-", " ").title()


def _prompt_playlist_name(default_name: str) -> str:
    """Ask the user to confirm or change a playlist name."""
    if _RICH_AVAILABLE:
        return Prompt.ask(
            "[cyan]  Playlist name[/cyan]",
            default=default_name,
        )
    raw = input(f"  Playlist name [{default_name}]: ").strip()
    return raw or default_name


def _ask_import_mode() -> str:
    """Ask the user whether to import all tracks automatically or review each one."""
    console.print(
        "\n[bold]How would you like to import?[/bold]"
        if _RICH_AVAILABLE else "\nHow would you like to import?"
    )
    console.print(
        "  [cyan]all[/cyan]       – Add all tracks automatically"
        if _RICH_AVAILABLE else "  all       - Add all tracks automatically"
    )
    console.print(
        "  [cyan]review[/cyan]    – Review each track individually"
        if _RICH_AVAILABLE else "  review    - Review each track individually"
    )
    return _ask_choice("\nChoose mode", ["all", "review"], default="all")


def process_folder(
    folder: Path,
    session: tidalapi.Session,
) -> None:
    """
    Discover all CSV files in *folder*, present each one to the user
    for confirmation, allow renaming, then import or skip each one.
    For each playlist the user is asked whether to import all tracks
    automatically or review them individually.
    """
    csv_files = discover_csv_files(folder)

    if not csv_files:
        console.print(
            f"[yellow]⚠️  No CSV files found in {folder}[/yellow]"
            if _RICH_AVAILABLE else f"No CSV files found in {folder}"
        )
        return

    console.print(
        f"\n[bold cyan]📂  Found {len(csv_files)} CSV file(s) in {folder}[/bold cyan]"
        if _RICH_AVAILABLE else f"\nFound {len(csv_files)} CSV file(s) in {folder}"
    )

    # Tracking for final summary
    imported_names: list[str] = []
    skipped_names: list[str] = []
    failed_entries: list[tuple[str, str]] = []

    for file_idx, csv_path in enumerate(csv_files, 1):
        console.print("\n")
        console.rule(
            f"[bold cyan]Playlist {file_idx}/{len(csv_files)}: {csv_path.name}[/bold cyan]"
            if _RICH_AVAILABLE else f"Playlist {file_idx}/{len(csv_files)}: {csv_path.name}"
        )

        # ── Load and validate CSV ─────────────────────────────────
        try:
            tracks = load_csv(csv_path)
        except Exception as exc:
            console.print(
                f"[red]❌  Failed to read {csv_path.name}: {exc}[/red]"
                if _RICH_AVAILABLE else f"Failed to read {csv_path.name}: {exc}"
            )
            failed_entries.append((csv_path.name, str(exc)))
            continue

        if not tracks:
            console.print(
                f"[yellow]⚠️  {csv_path.name} contains no tracks — skipping.[/yellow]"
                if _RICH_AVAILABLE else f"{csv_path.name} contains no tracks — skipping."
            )
            skipped_names.append(csv_path.name)
            continue

        # ── Show track overview ───────────────────────────────────
        print_track_list(tracks)

        # ── Ask: import or skip? ──────────────────────────────────
        do_import = _ask_yes_no(
            f"\n  Import this playlist ({len(tracks)} tracks)?",
            default=True,
        )
        if not do_import:
            console.print(
                "[yellow]  ⏭️  Skipped.[/yellow]" if _RICH_AVAILABLE else "  Skipped."
            )
            skipped_names.append(csv_path.name)
            continue

        # ── Allow renaming ────────────────────────────────────────
        default_name = _csv_to_default_name(csv_path)
        playlist_name = _prompt_playlist_name(default_name)

        # ── Choose import mode for this playlist ──────────────────
        mode = _ask_import_mode()

        # ── Import ────────────────────────────────────────────────
        try:
            if mode == "review":
                results, playlist = import_individually(session, tracks, playlist_name)
            else:
                results, playlist = import_all(session, tracks, playlist_name)
        except KeyboardInterrupt:
            console.print(
                "\n[yellow]⛔  Import of current playlist interrupted.[/yellow]"
                if _RICH_AVAILABLE else "\nImport of current playlist interrupted."
            )
            # Ask whether to continue with remaining files
            if not _ask_yes_no("  Continue with remaining playlists?", default=True):
                raise
            skipped_names.append(playlist_name)
            continue
        except Exception as exc:
            console.print(
                f"[red]❌  Import failed for '{playlist_name}': {exc}[/red]"
                if _RICH_AVAILABLE else f"Import failed for '{playlist_name}': {exc}"
            )
            failed_entries.append((playlist_name, str(exc)))
            continue

        print_results_summary(results)
        imported_names.append(playlist_name)

        # ── Optionally open in browser ────────────────────────────
        if _ask_yes_no("  Open this playlist in browser?", default=False):
            open_playlist(playlist)

    # ── Overall folder summary ────────────────────────────────────
    print_folder_summary(csv_files, imported_names, skipped_names, failed_entries)


# ─────────────────────────────────────────────────────────────────
# Input helpers
# ─────────────────────────────────────────────────────────────────
def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt the user for a yes/no answer, returning a bool."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            if _RICH_AVAILABLE:
                raw = Prompt.ask(prompt + suffix, default="y" if default else "n")
            else:
                raw = input(prompt + suffix + " ").strip()
            if not raw:
                return default
            return raw.lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return default


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    """Prompt the user to pick from a list of options."""
    options = "/".join(
        f"[bold]{c}[/bold]" if c == default else c
        for c in choices
    ) if _RICH_AVAILABLE else "/".join(choices)

    while True:
        try:
            if _RICH_AVAILABLE:
                raw = Prompt.ask(f"{prompt} [{options}]", default=default)
            else:
                raw = input(f"{prompt} ({'/'.join(choices)}) [{default}]: ").strip()
            raw = raw.lower() if raw else default.lower()
            if raw in [c.lower() for c in choices]:
                return raw
            console.print(f"[red]Please choose one of: {', '.join(choices)}[/red]" if _RICH_AVAILABLE else f"Please choose one of: {', '.join(choices)}")
        except (EOFError, KeyboardInterrupt):
            return default.lower()


# ─────────────────────────────────────────────────────────────────
# Open playlist in browser / app
# ─────────────────────────────────────────────────────────────────
def open_playlist(playlist: object) -> None:
    """Open the newly created TIDAL playlist in the browser or app."""
    # tidalapi provides listen_url and share_url on playlists
    url: Optional[str] = None

    for attr in ("listen_url", "share_url"):
        candidate = getattr(playlist, attr, None)
        if candidate:
            url = candidate
            break

    # Construct URL from UUID as last resort
    if not url:
        uuid = getattr(playlist, "id", None) or getattr(playlist, "uuid", None)
        if uuid:
            url = f"https://tidal.com/browse/playlist/{uuid}"

    if url:
        console.print(f"\n[bold cyan]🌐  Opening playlist:[/bold cyan] {url}" if _RICH_AVAILABLE else f"\nOpening: {url}")
        webbrowser.open(url)
    else:
        console.print("[yellow]Could not determine playlist URL.[/yellow]" if _RICH_AVAILABLE else "Could not determine playlist URL.")


# ─────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a Spotify playlist CSV into TIDAL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mutually exclusive: single CSV file or a folder of CSVs
    source_group = parser.add_mutually_exclusive_group(required=False)
    source_group.add_argument(
        "csv",
        nargs="?",
        default=None,
        help="Path to a single Spotify export CSV file",
    )
    source_group.add_argument(
        "--folder",
        "-f",
        default=None,
        metavar="DIR",
        help="Path to a folder containing one or more Spotify export CSV files",
    )

    parser.add_argument("--name", "-n", default=None, help="Name for the new TIDAL playlist (single-file mode only)")
    parser.add_argument(
        "--session-file",
        "-s",
        default=None,
        metavar="FILE",
        help="JSON file to persist/load TIDAL session (default: tidal_session.json)",
    )
    args = parser.parse_args()

    # ── Validate arguments ────────────────────────────────────────
    if not args.csv and not args.folder:
        sys.exit(
            "❌  Please provide either a CSV file path or a folder with --folder.\n"
            "You can export your playlists using Exportify: https://exportify.app/"
        )

    if args.folder and args.name:
        console.print(
            "[yellow]⚠️  --name is ignored in folder mode. You will be prompted to name each playlist.[/yellow]"
            if _RICH_AVAILABLE else "Warning: --name is ignored in folder mode."
        )

    # ── Banner ────────────────────────────────────────────────────
    if _RICH_AVAILABLE:
        console.print(Panel.fit(
            "[bold cyan]🎵  Spotify → TIDAL Playlist Importer[/bold cyan]",
            border_style="cyan",
        ))
    else:
        print("\n=== Spotify → TIDAL Playlist Importer ===\n")

    # ── Authenticate once for all imports ─────────────────────────
    session = get_tidal_session(args.session_file)

    # ── Folder mode ───────────────────────────────────────────────
    if args.folder:
        folder_path = Path(args.folder)
        if not folder_path.is_dir():
            sys.exit(f"❌  Not a directory: {folder_path}")

        try:
            process_folder(folder_path, session)
        except KeyboardInterrupt:
            sys.exit("\n\n⛔  Import cancelled by user.")

        console.print("\n[bold green]🎉  Done![/bold green]\n" if _RICH_AVAILABLE else "\nDone!\n")
        return

    # ── Single-file mode ──────────────────────────────────────────
    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"❌  File not found: {csv_path}")

    console.print(f"\n[cyan]📂  Loading:[/cyan] {csv_path}" if _RICH_AVAILABLE else f"\nLoading: {csv_path}")
    try:
        tracks = load_csv(csv_path)
    except Exception as exc:
        sys.exit(f"❌  Failed to read CSV: {exc}")

    if not tracks:
        sys.exit("❌  No tracks found in CSV.")

    console.print(f"[green]✅  Loaded {len(tracks)} tracks.[/green]" if _RICH_AVAILABLE else f"Loaded {len(tracks)} tracks.")

    print_track_list(tracks)

    # Playlist name
    if args.name:
        playlist_name = args.name
    else:
        default_name = _csv_to_default_name(csv_path)
        playlist_name = _prompt_playlist_name(default_name)

    mode = _ask_import_mode()

    try:
        if mode == "review":
            results, playlist = import_individually(session, tracks, playlist_name)
        else:
            results, playlist = import_all(session, tracks, playlist_name)
    except KeyboardInterrupt:
        sys.exit("\n\n⛔  Import cancelled by user.")

    print_results_summary(results)

    if _ask_yes_no("\nOpen playlist in browser?", default=True):
        open_playlist(playlist)

    console.print("\n[bold green]🎉  Done![/bold green]\n" if _RICH_AVAILABLE else "\nDone!\n")


if __name__ == "__main__":
    main()