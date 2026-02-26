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
  pip install tidalapi requests Pillow pygame rich

Usage:
  python spotify_to_tidal.py <path_to_csv>
  python spotify_to_tidal.py <path_to_csv> --name "My Playlist"
  python spotify_to_tidal.py <path_to_csv> --session-file tidal_session.json
────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
import webbrowser
import csv
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
    from PIL import Image
except ImportError:
    Image = None  # cover art display is optional

try:
    import pygame
    _PYGAME_AVAILABLE = True
except ImportError:
    _PYGAME_AVAILABLE = False

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


# ─────────────────────────────────────────────────────────────────
# TIDAL session
# ─────────────────────────────────────────────────────────────────
def get_tidal_session(session_file: Optional[str] = None) -> tidalapi.Session:
    """Authenticate with TIDAL, reusing a stored session when available."""
    session = tidalapi.Session()

    if session_file and Path(session_file).exists():
        console.print(f"[cyan]🔑  Loading saved session from {session_file}…[/cyan]")
        try:
            session.login_session_file(session_file)
            if session.check_login():
                console.print("[green]✅  Session restored successfully.[/green]")
                return session
            console.print("[yellow]⚠️  Saved session expired, re-authenticating…[/yellow]")
        except Exception as exc:
            console.print(f"[yellow]⚠️  Could not load session: {exc}[/yellow]")

    console.print("\n[bold cyan]🎵  TIDAL Login[/bold cyan]")
    console.print("A browser link will appear. Open it, log in, and confirm.")
    console.print("─" * 60)

    session.login_oauth_simple()

    if session_file:
        try:
            session.login_session_file(session_file, do_login=False)
            console.print(f"[green]💾  Session saved to {session_file}[/green]")
        except Exception:
            pass  # non-fatal

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
def display_cover_art(image_url: str, size: int = 320) -> None:
    """Download and display album cover art in the terminal (requires Pillow)."""
    if not image_url:
        return

    if Image is None:
        console.print(f"  [dim]Cover: {image_url}[/dim]")
        return

    try:
        response = requests.get(image_url, timeout=5)
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content))
        img.thumbnail((size, size))

        # Try to show inline (works in some terminals / IDEs)
        img.show()
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
    parser.add_argument("csv", help="Path to the Spotify export CSV file")
    parser.add_argument("--name", "-n", default=None, help="Name for the new TIDAL playlist")
    parser.add_argument(
        "--session-file",
        "-s",
        default="tidal_session.json",
        metavar="FILE",
        help="JSON file to persist/load TIDAL session (default: tidal_session.json)",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"❌  File not found: {csv_path}")

    # ── Banner ────────────────────────────────────────────────────
    if _RICH_AVAILABLE:
        console.print(Panel.fit(
            "[bold cyan]🎵  Spotify → TIDAL Playlist Importer[/bold cyan]",
            border_style="cyan",
        ))
    else:
        print("\n=== Spotify → TIDAL Playlist Importer ===\n")

    # ── Load CSV ──────────────────────────────────────────────────
    console.print(f"\n[cyan]📂  Loading:[/cyan] {csv_path}" if _RICH_AVAILABLE else f"\nLoading: {csv_path}")
    try:
        tracks = load_csv(csv_path)
    except Exception as exc:
        sys.exit(f"❌  Failed to read CSV: {exc}")

    if not tracks:
        sys.exit("❌  No tracks found in CSV.")

    console.print(f"[green]✅  Loaded {len(tracks)} tracks.[/green]" if _RICH_AVAILABLE else f"Loaded {len(tracks)} tracks.")

    # ── Show track list ───────────────────────────────────────────
    print_track_list(tracks)

    # ── Playlist name ─────────────────────────────────────────────
    if args.name:
        playlist_name = args.name
    else:
        default_name = csv_path.stem.replace("_", " ").replace("-", " ").title()
        if _RICH_AVAILABLE:
            playlist_name = Prompt.ask("\n[cyan]Playlist name[/cyan]", default=default_name)
        else:
            playlist_name = input(f"\nPlaylist name [{default_name}]: ").strip() or default_name

    # ── Import mode ───────────────────────────────────────────────
    console.print("\n[bold]How would you like to import?[/bold]" if _RICH_AVAILABLE else "\nHow would you like to import?")
    console.print("  [cyan]all[/cyan]       – Add all tracks automatically" if _RICH_AVAILABLE else "  all       - Add all tracks automatically")
    console.print("  [cyan]review[/cyan]    – Review each track individually" if _RICH_AVAILABLE else "  review    - Review each track individually")

    mode = _ask_choice("\nChoose mode", ["all", "review"], default="all")

    # ── Authenticate ──────────────────────────────────────────────
    session = get_tidal_session(args.session_file)

    # ── Import ────────────────────────────────────────────────────
    try:
        if mode == "review":
            results, playlist = import_individually(session, tracks, playlist_name)
        else:
            results, playlist = import_all(session, tracks, playlist_name)
    except KeyboardInterrupt:
        sys.exit("\n\n⛔  Import cancelled by user.")

    # ── Summary ───────────────────────────────────────────────────
    print_results_summary(results)

    # ── Open in browser ───────────────────────────────────────────
    if _ask_yes_no("\nOpen playlist in browser?", default=True):
        open_playlist(playlist)

    console.print("\n[bold green]🎉  Done![/bold green]\n" if _RICH_AVAILABLE else "\nDone!\n")


if __name__ == "__main__":
    main()
