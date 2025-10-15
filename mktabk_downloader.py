#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Dict

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
from tqdm import tqdm


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    )
}


@dataclass
class Lesson:
    title: str
    url: str
    index: int


def read_netscape_cookies(cookie_file: Path) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    with cookie_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                # Not a valid netscape cookie row
                continue
            domain, _flag, path, secure, expiry, name, value = parts[:7]
            secure_flag = secure.upper() == "TRUE"
            jar.set(name, value, domain=domain, path=path, secure=secure_flag)
    return jar


def normalize_filename(name: str) -> str:
    cleaned = re.sub(r"[\s\n\r\t]+", " ", name).strip()
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", cleaned)
    return cleaned[:180]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def list_existing_titles(output_dir: Path) -> set:
    """Return a set of normalized lesson titles already downloaded in output_dir.
    We strip leading index, quality suffix, and extension, keeping the normalized title core.
    """
    existing = set()
    if not output_dir.exists():
        return existing
    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        # Remove extension
        base, _sep, _ext = name.rpartition('.')
        if not base:
            base = name
        # Remove leading "NN - " index if present
        m = re.match(r"^\s*\d+\s*-\s*(.+)$", base)
        if m:
            base = m.group(1)
        # Remove optional quality suffix " [high]" or " [low]"
        base = re.sub(r"\s*\[(?:high|low)\]$", "", base, flags=re.IGNORECASE)
        existing.add(base.strip().lower())
    return existing


def request_get(session: requests.Session, url: str) -> requests.Response:
    for attempt in range(5):
        try:
            resp = session.get(url, headers=DEFAULT_HEADERS, timeout=30)
            if resp.status_code in (200, 206):
                return resp
            if resp.status_code in (403, 401):
                raise RuntimeError(f"Unauthorized or forbidden: {resp.status_code} at {url}")
        except requests.RequestException as e:
            if attempt == 4:
                raise
        time.sleep(1 + attempt * 0.5)
    raise RuntimeError(f"Failed GET after retries: {url}")


def parse_course_lessons(html: str, base_url: str) -> List[Lesson]:
    soup = BeautifulSoup(html, "html.parser")
    lessons: List[Lesson] = []

    # Determine the stable course prefix like /course/<slug>
    parsed = urlparse(base_url)
    # Compute both encoded and decoded course path prefixes for robust matching
    base_path_enc = parsed.path
    base_path_dec = unquote(parsed.path)
    m_enc = re.match(r"^(/course/[^/]+)", base_path_enc)
    m_dec = re.match(r"^(/course/[^/]+)", base_path_dec)
    course_path_prefix_enc = m_enc.group(1) if m_enc else "/course/"
    course_path_prefix_dec = m_dec.group(1) if m_dec else "/course/"

    # Detect elements (not only anchors) with href attributes under the same course slug
    href_elems = soup.select("[href]")
    idx = 1
    for elem in href_elems:
        href = elem.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        full = urljoin("https://maktabkhooneh.org", href)

        # Constrain to same course slug (match against both encoded and decoded forms)
        full_path_enc = urlparse(full).path
        full_path_dec = unquote(full_path_enc)
        if not (
            full_path_enc.startswith(course_path_prefix_enc)
            or full_path_dec.startswith(course_path_prefix_dec)
        ):
            continue

        # Accept common lesson URL patterns: include Persian words (encoded or not) or chapter markers like "ch123"
        path_lower = urlparse(full).path.lower()
        path_decoded = unquote(path_lower)
        if (
            ("/ch" in path_lower) or
            ("/ویدیو" in path_decoded) or
            ("/فیلم" in path_decoded) or
            ("%d9%88%db%8c%d8%af%db%8c%d9%88" in path_lower) or
            ("%d9%81%db%8c%d9%84%d9%85" in path_lower)
        ):
            # Try to extract a meaningful title
            title_text = (elem.get_text(strip=True) or elem.get("title") or Path(path_decoded).name)
            if not title_text:
                title_text = Path(path_decoded).name
            lessons.append(Lesson(title=title_text, url=full, index=idx))
            idx += 1

    # Deduplicate by URL while preserving order
    seen = set()
    unique: List[Lesson] = []
    for l in lessons:
        if l.url in seen:
            continue
        seen.add(l.url)
        unique.append(l)
    
    # Skip first 3 lessons (duplicates) and renumber starting from 1
    if len(unique) > 3:
        unique = unique[3:]  # Skip first 3 lessons
        # Renumber lessons starting from 1
        for i, lesson in enumerate(unique, start=1):
            lesson.index = i
    
    return unique


def extract_quality_links(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    links: Dict[str, str] = {}
    # Find direct anchors with mp4
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".mp4" not in href:
            continue
        href_lower = href.lower()
        if "hq" in href_lower:
            links.setdefault("high", href)
        elif "lq" in href_lower or "low" in href_lower:
            links.setdefault("low", href)
        else:
            # Unlabeled; keep as generic
            links.setdefault("auto", href)

    # Also check <source> tags
    for source in soup.find_all("source"):
        src = source.get("src")
        if src and ".mp4" in src:
            links.setdefault("auto", src)

    # OpenGraph video meta as fallback
    og = soup.find("meta", attrs={"property": "og:video"})
    if og and og.get("content"):
        links.setdefault("auto", og["content"].strip())
    return links


def extract_video_url_from_video_page(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    # 1) Try OpenGraph video meta
    og = soup.find("meta", attrs={"property": "og:video"})
    if og and og.get("content"):
        return og["content"].strip()

    # 2) Look for common player configs or source tags
    # <source src="...mp4" ...>
    for source in soup.find_all("source"):
        src = source.get("src")
        if src and (".mp4" in src or ".m3u8" in src):
            return src

    # 3) Look for JSON data in scripts containing URLs
    for script in soup.find_all("script"):
        text = script.string or script.get_text("\n", strip=False)
        if not text:
            continue
        m = re.search(r"https?://[^\s'\"]+\.(?:m3u8|mp4)(?:\?[^'\"\s<>]*)?", text)
        if m:
            return m.group(0)

    return None


def looks_logged_in(html: str) -> bool:
    # Heuristics: presence of user menu or specific strings indicating authenticated state
    text = html.lower()
    return any(
        s in text for s in [
            "logout", "signout", "profile", "account",
            "خروج", "پروفایل", "حساب کاربری",
        ]
    )


def stream_download(session: requests.Session, url: str, dest_path: Path, chunk_size: int = 1024 * 1024) -> None:
    """Download with resume support using HTTP Range and .part files."""
    part_path = dest_path.with_suffix(dest_path.suffix + ".part")

    # If final file exists and is non-empty, skip
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return

    # Determine starting offset (resume)
    start_at = 0
    if part_path.exists():
        try:
            start_at = part_path.stat().st_size
        except OSError:
            start_at = 0

    headers = dict(DEFAULT_HEADERS)
    if start_at > 0:
        headers["Range"] = f"bytes={start_at}-"

    with session.get(url, headers=headers, stream=True, timeout=60) as r:
        # If server doesn't honor Range, it may send 200 and full content
        if start_at > 0 and r.status_code not in (206, 200):
            r.raise_for_status()
        elif start_at == 0:
            r.raise_for_status()

        # Compute expected total for progress
        total = None
        if r.status_code == 206:
            # Content-Range: bytes start-end/total
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                try:
                    total = int(cr.split("/")[-1])
                except ValueError:
                    total = None
        if total is None:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            if total == 0:
                total = None

        mode = "ab" if start_at > 0 else "wb"
        progress = tqdm(
            total=total if total is not None else None,
            initial=start_at,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=dest_path.name,
        )
        try:
            with part_path.open(mode) as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        progress.update(len(chunk))
        finally:
            progress.close()

    # Rename .part to final on success
    try:
        part_path.replace(dest_path)
    except OSError:
        # Fallback copy
        with part_path.open("rb") as src, dest_path.open("wb") as dst:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                dst.write(buf)
        try:
            part_path.unlink()
        except OSError:
            pass


def discover_and_download_course(
    course_url: str,
    output_dir: Path,
    session: requests.Session,
    max_workers: int = 3,
    debug: bool = False,
    course_html_override: Optional[str] = None,
    preferred_quality: str = "auto",
) -> None:
    ensure_dir(output_dir)
    existing_titles = list_existing_titles(output_dir)
    if course_html_override is not None:
        course_html = course_html_override
    else:
        r = request_get(session, course_url)
        course_html = r.text

    if debug:
        dbg_path = output_dir / "debug_course.html"
        try:
            dbg_path.write_text(course_html, encoding="utf-8")
            print(f"[debug] saved fetched course HTML to {dbg_path}")
        except Exception:
            pass
        cookie_names = [c.name for c in session.cookies]
        print(f"[debug] cookies in session: {cookie_names}")
        print(f"[debug] looks_logged_in={looks_logged_in(course_html)}")
    lessons = parse_course_lessons(course_html, course_url)

    # If lesson count is suspiciously small, try sidebar scraping by visiting the first lesson page
    if len(lessons) < 5:
        # Attempt to locate the first lesson link from the course page and load its sidebar
        first_link = None
        if lessons:
            first_link = lessons[0].url
        else:
            # Fallback: find any href that looks like a video page under this course
            soup = BeautifulSoup(course_html, "html.parser")
            for elem in soup.select("[href]"):
                href = elem.get("href", "").strip()
                if not href:
                    continue
                full = urljoin("https://maktabkhooneh.org", href)
                p = urlparse(full)
                if "/course/" in p.path and ("ch" in p.path or "ویدیو" in unquote(p.path) or "%D9%88%DB%8C%D8%AF%DB%8C%D9%88" in p.path):
                    first_link = full
                    break
        if first_link:
            try:
                first_html = request_get(session, first_link).text
                # Sidebar container hint from user; instead of XPath, find many anchors under a likely sidebar area
                sidebar_candidates = []
                soup_v = BeautifulSoup(first_html, "html.parser")
                # Prefer containers with many lesson-like anchors; filter strictly to avoid looping/pagination links
                for container in soup_v.find_all(True):
                    anchors = container.find_all("a", href=True)
                    if len(anchors) >= 8:
                        sidebar_candidates.append((container, anchors))
                sidebar_links: List[Lesson] = []
                idx_start = 1
                if lessons:
                    idx_start = len(lessons) + 1
                seen_urls = set()
                for _container, anchors in sidebar_candidates:
                    for a in anchors:
                        href = a["href"].strip()
                        full = urljoin("https://maktabkhooneh.org", href)
                        if full in seen_urls:
                            continue
                        if not full.startswith("https://maktabkhooneh.org/course/"):
                            continue
                        p = urlparse(full)
                        pp = unquote(p.path)
                        # Only accept lesson-like links, avoid course home and unrelated links
                        if not ("/ch" in p.path or "/ویدیو" in pp or "/فیلم" in pp or "-ویدیو-" in pp or "-فیلم-" in pp):
                            continue
                        title_text = a.get_text(strip=True) or Path(pp).name
                        if not title_text:
                            continue
                        seen_urls.add(full)
                        sidebar_links.append(Lesson(title=title_text, url=full, index=0))
                # Deduplicate and merge, then re-index
                if sidebar_links:
                    existing_urls = {l.url for l in lessons}
                    for l in sidebar_links:
                        if l.url not in existing_urls:
                            lessons.append(l)
                    # Skip first 3 lessons and reindex starting from 1
                    if len(lessons) > 3:
                        lessons = lessons[3:]  # Skip first 3 lessons
                    # Reindex in discovered order starting from 1
                    for i, l in enumerate(lessons, start=1):
                        l.index = i
                    if debug:
                        print(f"[debug] sidebar scraping added lessons; total={len(lessons)}")
            except Exception as e:
                if debug:
                    print(f"[debug] sidebar scrape failed: {e}")

    if not lessons:
        print("No lessons discovered from course page. If you're logged in in your browser, try exporting a fresh cookie file, or run with --course-html pointing to a saved course page HTML.")
        return

    print(f"Discovered {len(lessons)} lesson pages")

    def process_lesson(lesson: Lesson) -> Tuple[Lesson, Optional[Path], Optional[str]]:
        try:
            page_html = request_get(session, lesson.url).text
            # Prefer explicit quality links on the page
            quality_links = extract_quality_links(page_html)
            media_url = None
            if preferred_quality == "high" and "high" in quality_links:
                media_url = quality_links["high"]
            elif preferred_quality == "low" and "low" in quality_links:
                media_url = quality_links["low"]
            elif "auto" in quality_links:
                media_url = quality_links["auto"]
            # Fallback to general extraction (og:video, sources, scripts)
            if not media_url:
                media_url = extract_video_url_from_video_page(page_html)
            if not media_url:
                return (lesson, None, None)

            ext = ".mp4" if ".mp4" in media_url else (".m3u8" if ".m3u8" in media_url else "")
            q_suffix = ""
            if preferred_quality in ("high", "low"):
                q_suffix = f" [{preferred_quality}]"
            filename = f"{lesson.index:02d} - {normalize_filename(lesson.title)}{q_suffix}{ext or '.mp4'}"
            dest = output_dir / filename
            # Early skip based on existing titles (fast path without needing ext)
            core_title_key = normalize_filename(lesson.title).strip().lower()
            if core_title_key in existing_titles:
                print(f"[skip] already have title: {lesson.title}")
                return (lesson, dest, None)
            # Skip if already downloaded
            try:
                if dest.exists() and dest.stat().st_size > 0:
                    print(f"[skip] {dest.name} already exists")
                    return (lesson, dest, None)
            except OSError:
                pass
            if media_url.endswith(".m3u8"):
                # For HLS, try to use ffmpeg if available
                return (lesson, dest, media_url)
            else:
                stream_download(session, media_url, dest)
                return (lesson, dest, None)
        except Exception as e:
            print(f"[ERROR] {lesson.title}: {e}")
            return (lesson, None, None)

    # Download sequentially (one by one) for stability and better resume behavior
    results: List[Tuple[Lesson, Optional[Path], Optional[str]]] = []
    for lesson in lessons:
        res = process_lesson(lesson)
        results.append(res)

    # Handle any m3u8 entries via ffmpeg command line if present
    m3u8_entries = [(l, d, m3u8) for (l, d, m3u8) in results if m3u8]
    if m3u8_entries:
        print(f"{len(m3u8_entries)} HLS streams detected. Attempting ffmpeg...")
        for lesson, dest, m3u8_url in m3u8_entries:
            assert dest is not None and m3u8_url is not None
            # Build ffmpeg command. We pass cookies via header if needed by CDN.
            cookie_header = build_cookie_header(session.cookies)
            cmd = (
                f"ffmpeg -y -headers \"User-Agent: {DEFAULT_HEADERS['User-Agent']}\r\n{cookie_header}\r\n\" "
                f"-i \"{m3u8_url}\" -c copy -bsf:a aac_adtstoasc \"{dest}\""
            )
            rc = os.system(cmd)
            if rc != 0:
                print(f"[WARN] ffmpeg failed for {lesson.title}. You can manually run:\n{cmd}")

    print("Done.")


def build_cookie_header(jar: requests.cookies.RequestsCookieJar) -> str:
    # Build a Cookie: header line for ffmpeg
    cookie_pairs = [f"{c.name}={c.value}" for c in jar]
    return f"Cookie: {'; '.join(cookie_pairs)}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download Maktabkhooneh course videos with cookies")
    parser.add_argument("course_url", help="Course overview URL")
    parser.add_argument("--cookies", required=True, help="Path to Netscape cookie file (e.g., maktabkhooneh.org_cookies.txt)")
    parser.add_argument("--out", default="downloads", help="Output directory")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent workers")
    parser.add_argument("--debug", action="store_true", help="Enable debug output and save fetched HTML")
    parser.add_argument("--course-html", help="Path to local saved course overview HTML to parse instead of fetching", default=None)
    parser.add_argument("--quality", choices=["auto", "high", "low"], default="auto", help="Preferred quality for mp4 links on video pages")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    ensure_dir(out_dir)

    cookie_path = Path(args.cookies)
    if not cookie_path.exists():
        print(f"Cookie file not found: {cookie_path}")
        return 2

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.cookies = read_netscape_cookies(cookie_path)

    try:
        course_html_override = None
        if args.course_html:
            cp = Path(args.course_html)
            if cp.exists():
                course_html_override = cp.read_text(encoding="utf-8", errors="ignore")
            else:
                print(f"--course-html not found: {cp}")
        discover_and_download_course(
            args.course_url,
            out_dir,
            session,
            max_workers=args.workers,
            debug=args.debug,
            course_html_override=course_html_override,
            preferred_quality=args.quality,
        )
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


