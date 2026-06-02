import os
import sys
import signal
import sqlite3
import hashlib
import logging
import time
import html
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

# Let the threads know if the user hit Ctrl+C so they can wrap up cleanly
shutdown_event = threading.Event()

def handle_exit(sig, frame):
    logging.info("Caught termination signal. Finishing up active downloads, please wait...")
    shutdown_event.set()

signal.signal(signal.SIGINT, handle_exit)

@dataclass(frozen=True)
class MediaAsset:
    primary_url: str
    fallback_url: str | None
    media_type: str

@dataclass
class Config:
    username: str
    session_cookie: str
    media_type: str = "both"
    max_workers: int = 15
    download_dir: str = field(init=False)
    db_path: str = field(init=False)

    def __post_init__(self):
        # Clean up the username just in case they typed "u/username"
        clean_name = self.username.replace('u/', '').strip()
        object.__setattr__(self, 'username', clean_name)
        object.__setattr__(self, 'download_dir', f"reddit_{clean_name}_archive")
        object.__setattr__(self, 'db_path', os.path.join(self.download_dir, "sync_state.sqlite"))

class RedditArchiver:
    def __init__(self, config: Config):
        self.config = config
        
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        self.logger = logging.getLogger(__name__)
        
        os.makedirs(self.config.download_dir, exist_ok=True)
        self._init_db()
        self.session = self._build_session()

    def _init_db(self):
        # Using WAL (Write-Ahead Logging) allows our thread pool to read and write 
        # to the database simultaneously without getting "database locked" errors.
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    file_hash TEXT,
                    filename TEXT,
                    media_type TEXT
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_file_hash ON downloads(file_hash)')

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        # Ensure our connection pool is large enough for our max workers + a buffer
        adapter = HTTPAdapter(pool_connections=self.config.max_workers + 5, pool_maxsize=self.config.max_workers + 5)
        
        session.mount("https://", adapter)
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
        })
        session.cookies.set('reddit_session', self.config.session_cookie, domain='.reddit.com')
        return session

    def clean_url(self, url: str) -> str:
        """Strips out Reddit's tracking wrappers and preview constraints."""
        url = html.unescape(url)
        
        if "reddit.com/media" in url:
            qs = parse_qs(urlparse(url).query)
            url = unquote(qs["url"][0]) if "url" in qs else url
                
        base_url = url.split("?")[0]
        if "preview.redd.it" in urlparse(base_url).netloc:
            return f"https://i.redd.it/{Path(urlparse(base_url).path).name}"
            
        return base_url

    def parse_node(self, post: dict) -> set[MediaAsset]:
        """Digs through a post's JSON dict to find media URLs. Handles crossposts recursively."""
        assets = set()
        wants_videos = self.config.media_type in ('videos', 'both')
        wants_images = self.config.media_type in ('images', 'both')

        if wants_videos:
            # 1. Standard Videos
            secure_media = post.get("secure_media") or {}
            vid_url = secure_media.get("reddit_video", {}).get("fallback_url")
            if vid_url:
                raw_url = html.unescape(vid_url)
                assets.add(MediaAsset(raw_url.split("?")[0], raw_url, 'video'))
            
            # 2. Video Previews (often GIFs converted to mp4)
            preview = post.get("preview") or {}
            vid_prev = preview.get("reddit_video_preview", {}).get("fallback_url")
            if vid_prev:
                raw_url = html.unescape(vid_prev)
                assets.add(MediaAsset(raw_url.split("?")[0], raw_url, 'video'))

        if wants_images:
            # 3. Galleries
            metadata = post.get("media_metadata") or {}
            for item in metadata.values():
                if type(item) is dict and item.get("status") == "valid":
                    s_node = item.get("s", {})
                    raw_url = html.unescape(s_node.get("u") or s_node.get("gif") or "")
                    if raw_url:
                        assets.add(MediaAsset(self.clean_url(raw_url), raw_url, 'image'))
                            
            # 4. Standard Single Images
            dest_url = post.get("url_overridden_by_dest", post.get("url", ""))
            if dest_url and any(dest_url.lower().split("?")[0].endswith(e) for e in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                raw_url = html.unescape(dest_url)
                assets.add(MediaAsset(self.clean_url(raw_url), raw_url, 'image'))
                
            # 5. Preview Fallbacks
            preview_images = (post.get("preview") or {}).get("images", [])
            if not assets and preview_images:
                source_url = preview_images[0].get("source", {}).get("url")
                if source_url:
                    raw_url = html.unescape(source_url)
                    assets.add(MediaAsset(self.clean_url(raw_url), raw_url, 'image'))

        # Dig into crossposts if they exist
        for parent in post.get("crosspost_parent_list", []):
            if type(parent) is dict:
                assets.update(self.parse_node(parent))
                    
        return assets

    def resolve_filename(self, asset: MediaAsset, file_hash: str) -> str:
        """Generates a safe filename, preventing Reddit videos from overwriting each other."""
        base_name = asset.primary_url.split("?")[0].split("/")[-1]
        
        # Reddit videos are often just named 'DASH_1080.mp4', so we prepend their unique folder ID
        if asset.media_type == 'video' and ('DASH_' in base_name or 'CMAF_' in base_name):
            url_parts = asset.primary_url.split("?")[0].split("/")
            if len(url_parts) >= 2:
                return f"{url_parts[-2]}_{base_name}"
                
        # If the URL is totally weird and has no filename, fallback to the hash
        if not base_name or len(base_name) > 50:
            ext = ".mp4" if asset.media_type == 'video' else ".jpg"
            return f"{file_hash}{ext}"
            
        return base_name

    def download_worker(self, asset: MediaAsset):
        if shutdown_event.is_set():
            return

        try:
            # Skip if we already downloaded this exact URL
            with sqlite3.connect(self.config.db_path) as conn:
                if conn.execute("SELECT 1 FROM downloads WHERE url = ?", (asset.primary_url,)).fetchone():
                    return

            resp = self.session.get(asset.primary_url, stream=True, timeout=15)
            
            # If the primary clean URL 404s, try the fallback preview URL (which includes signature params)
            if resp.status_code != 200 and asset.fallback_url and asset.fallback_url != asset.primary_url:
                resp = self.session.get(asset.fallback_url, stream=True, timeout=15)
                
            if resp.status_code != 200:
                self.logger.warning(f"Skipping (HTTP {resp.status_code}): {asset.primary_url}")
                return

            hasher = hashlib.sha256()
            tmp_path = os.path.join(self.config.download_dir, f"{threading.get_ident()}.tmp")
            
            # Write chunks to a temp file. This keeps memory usage tiny even on massive video files.
            with open(tmp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if shutdown_event.is_set():
                        break
                    f.write(chunk)
                    hasher.update(chunk)

            # Cleanup if the user cancelled mid-download
            if shutdown_event.is_set():
                if os.path.exists(tmp_path): os.remove(tmp_path)
                return

            file_hash = hasher.hexdigest()
            filename = self.resolve_filename(asset, file_hash)
            final_path = os.path.join(self.config.download_dir, filename)

            with sqlite3.connect(self.config.db_path, timeout=10) as conn:
                existing = conn.execute("SELECT filename FROM downloads WHERE file_hash = ?", (file_hash,)).fetchone()
                
                if existing:
                    # Duplicate found! Delete the temp file, but log the new URL to the DB
                    os.remove(tmp_path)
                    saved_name = existing[0]
                else:
                    # Atomic rename (prevents corrupted half-files from lingering on the drive)
                    os.replace(tmp_path, final_path)
                    saved_name = filename
                    self.logger.info(f"[{asset.media_type.upper()}] {filename}")

                conn.execute(
                    "INSERT OR IGNORE INTO downloads (url, file_hash, filename, media_type) VALUES (?, ?, ?, ?)", 
                    (asset.primary_url, file_hash, saved_name, asset.media_type)
                )

        except requests.RequestException as e:
            if not shutdown_event.is_set():
                self.logger.error(f"Network hitch on {asset.primary_url}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")

    def run(self):
        self.logger.info(f"Scanning profile u/{self.config.username} for {self.config.media_type}...")
        endpoint = f"https://www.reddit.com/user/{self.config.username}/submitted.json"
        after = None
        all_assets: set[MediaAsset] = set()
        
        while not shutdown_event.is_set():
            params = {"limit": 100}
            if after: 
                params["after"] = after
            
            try:
                response = self.session.get(endpoint, params=params, timeout=10)
            except requests.RequestException:
                self.logger.error("Network issue hitting the API. Retrying in 3 seconds...")
                time.sleep(3)
                continue
                
            if response.status_code == 403:
                self.logger.critical("403 Forbidden. Your session cookie is invalid, expired, or missing.")
                return
            elif response.status_code != 200:
                self.logger.warning(f"API Error {response.status_code}. Retrying in 3 seconds...")
                time.sleep(3)
                continue
                
            data = response.json()
            children = data.get("data", {}).get("children", [])
            
            if not children: 
                break
                
            for child in children:
                if type(child) is dict:
                    all_assets.update(self.parse_node(child.get("data", {})))
                
            after = data.get("data", {}).get("after")
            if not after: 
                break
                
            self.logger.info(f"Aggregated {len(all_assets)} media items so far...")
            time.sleep(1) # Be polite to Reddit's API rate limits
            
        if not all_assets or shutdown_event.is_set():
            self.logger.info("Nothing to download or operation cancelled.")
            return

        self.logger.info(f"Extraction done. Handing off {len(all_assets)} items to {self.config.max_workers} download workers...")
        
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            futures = [executor.submit(self.download_worker, asset) for asset in all_assets]
            for _ in as_completed(futures):
                if shutdown_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

        self.logger.info(f"All done! Files saved to: {os.path.abspath(self.config.download_dir)}")

if __name__ == "__main__":
    print("\n--- Reddit Media Archiver ---")
    
    username = input("Target Username (e.g. PeachSonnet): ").strip()
    if not username:
        print("Username is required. Exiting.")
        sys.exit(1)
        
    cookie = input("Reddit Session Cookie: ").strip()
    if not cookie:
        print("Session cookie is required to bypass the 403 API blocks. Exiting.")
        sys.exit(1)
        
    media_choice = input("Download (1) Images, (2) Videos, or (3) Both? [Default: 3]: ").strip()
    media_type = {'1': 'images', '2': 'videos'}.get(media_choice, 'both')
    
    config = Config(username=username, session_cookie=cookie, media_type=media_type)
    
    print("\nStarting pipeline...\n")
    archiver = RedditArchiver(config)
    archiver.run()
