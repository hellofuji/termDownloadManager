#!/usr/bin/env python3
import argparse
import os
import threading
import time
import curses
import urllib.request
import multiprocessing
import socket
import signal
import sys
import shutil
import base64

MAX_RETRIES = 5
RETRY_DELAY = 5

# Color pairs definition
COLOR_PAIRS = {
    'success': 1,    # Green
    'warning': 2,    # Yellow  
    'error': 3,      # Red
    'info': 4,       # Blue
    'progress': 5,   # Cyan
    'normal': 6      # White
}

class DownloadManager:
    def __init__(self):
        self.shutdown = False
        self.lock = threading.Lock()
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def signal_handler(self, signum, frame):
        """Handle graceful shutdown"""
        with self.lock:
            self.shutdown = True
        print("\n\nShutdown requested... Waiting for threads to finish.", flush=True)

    def init_colors(self):
        """Initialize color pairs"""
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(COLOR_PAIRS['success'], curses.COLOR_GREEN, -1)
        curses.init_pair(COLOR_PAIRS['warning'], curses.COLOR_YELLOW, -1)
        curses.init_pair(COLOR_PAIRS['error'], curses.COLOR_RED, -1)
        curses.init_pair(COLOR_PAIRS['info'], curses.COLOR_BLUE, -1)
        curses.init_pair(COLOR_PAIRS['progress'], curses.COLOR_CYAN, -1)
        curses.init_pair(COLOR_PAIRS['normal'], curses.COLOR_WHITE, -1)

    def colored_text(self, stdscr, text, color_type, y=0, x=0):
        """Display colored text"""
        color_pair = COLOR_PAIRS.get(color_type, COLOR_PAIRS['normal'])
        stdscr.addstr(y, x, text, curses.color_pair(color_pair))

    def get_temp_dir(self, download_dir, filename):
        """Get the hidden temp directory for this download"""
        temp_dir = os.path.join(download_dir, f".{filename}.temp")
        return temp_dir

    def cleanup_previous_temp_files(self, download_dir, filename):
        """Clean up any existing temp files from previous downloads"""
        temp_dir = self.get_temp_dir(download_dir, filename)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

    def check_existing_download(self, download_dir, filename, num_threads):
        """Check if there's an existing download that can be resumed"""
        temp_dir = self.get_temp_dir(download_dir, filename)
        
        if not os.path.exists(temp_dir):
            return None, [0] * num_threads
        
        # Check if all temp files exist and get their sizes
        temp_files = [os.path.join(temp_dir, f"chunk_{i}") for i in range(num_threads)]
        resume_sizes = []
        total_resume_size = 0
        
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                size = os.path.getsize(temp_file)
                resume_sizes.append(size)
                total_resume_size += size
            else:
                # If any temp file is missing, can't resume
                return None, [0] * num_threads
        
        return temp_dir, resume_sizes

    def ask_resume_or_fresh(self, total_resume_size, filename):
        """Ask user whether to resume or start fresh"""
        if total_resume_size == 0:
            return False
        
        print(f"\nFound existing download for '{filename}'")
        print(f"Resume size: {total_resume_size / (1024**2):.2f} MB")
        
        while True:
            choice = input("Do you want to resume download? (y/n): ").strip().lower()
            if choice in ['y', 'yes']:
                return True
            elif choice in ['n', 'no']:
                return False
            else:
                print("Please enter 'y' for yes or 'n' for no")

    def download_chunk(self, url, start, end, temp_file, progress, lock, resume_size=0, user=None, password=None):
        """Download a chunk of file with shutdown awareness"""
        req = urllib.request.Request(url)
        if user and password:
            credentials = f"{user}:{password}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            req.add_header('Authorization', f'Basic {encoded_credentials}')

        range_header = f'bytes={start + resume_size}-' if end == '' else f'bytes={start + resume_size}-{end}'
        req.add_header('Range', range_header)
        
        retries = 0
        while retries < MAX_RETRIES and not self.shutdown:
            try:
                with urllib.request.urlopen(req, timeout=30) as response, open(temp_file, 'ab' if resume_size > 0 else 'wb') as f:
                    while not self.shutdown:
                        data = response.read(4096)
                        if not data:
                            break
                        f.write(data)
                        with lock:
                            progress['total_downloaded'] += len(data)
                break  # Success, exit retry loop
            except (urllib.error.URLError, socket.timeout, ConnectionResetError) as e:
                retries += 1
                if retries < MAX_RETRIES and not self.shutdown:
                    time.sleep(RETRY_DELAY)
                else:
                    with lock:
                        progress['errors'] = progress.get('errors', 0) + 1
                    raise Exception(f"Failed to download chunk after {MAX_RETRIES} retries: {e}")

    def tui(self, stdscr, progress, file_size, filename):
        """Enhanced TUI with colors and better layout"""
        self.init_colors()
        curses.curs_set(0)
        stdscr.clear()
        
        while progress['total_downloaded'] < file_size and not self.shutdown:
            stdscr.clear()
            
            # Header
            self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'info', 0, 0)
            self.colored_text(stdscr, "║              TERM DOWNLOAD MANAGER                ║", 'info', 1, 0)
            self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'info', 2, 0)
            
            # File info
            self.colored_text(stdscr, f"File: {filename}", 'normal', 4, 2)
            self.colored_text(stdscr, f"Size: {file_size / (1024**2):.2f} MB", 'normal', 5, 2)
            
            # Progress
            downloaded = progress['total_downloaded']
            percent = (downloaded / file_size) * 100 if file_size > 0 else 0
            elapsed = time.time() - progress['start_time']
            speed = downloaded / elapsed if elapsed > 0 else 0
            remaining_time = (file_size - downloaded) / speed if speed > 0 else 0
            
            # Progress bar with colors
            bar_width = 50
            filled = int(bar_width * (downloaded / file_size)) if file_size > 0 else 0
            bar = '█' * filled + '░' * (bar_width - filled)
            
            self.colored_text(stdscr, "Progress:", 'progress', 7, 2)
            stdscr.addstr(8, 2, f"[{bar}] {percent:.1f}%", curses.color_pair(COLOR_PAIRS['progress']))
            
            # Statistics
            self.colored_text(stdscr, f"Downloaded: {downloaded / (1024**2):.2f} MB", 'normal', 10, 2)
            self.colored_text(stdscr, f"Speed: {speed / 1024:.2f} KB/s", 'normal', 11, 2)
            
            if remaining_time > 0:
                if remaining_time > 3600:
                    time_str = f"{remaining_time / 3600:.1f} hours"
                elif remaining_time > 60:
                    time_str = f"{remaining_time / 60:.1f} minutes"
                else:
                    time_str = f"{remaining_time:.0f} seconds"
                self.colored_text(stdscr, f"Time left: {time_str}", 'normal', 12, 2)
            else:
                self.colored_text(stdscr, "Time left: Calculating...", 'warning', 12, 2)
            
            # Thread info
            self.colored_text(stdscr, f"Threads: {progress.get('threads', 1)}", 'info', 14, 2)
            
            # Status info
            if progress.get('resumed', False):
                self.colored_text(stdscr, "Status: Resumed download", 'info', 15, 2)
            else:
                self.colored_text(stdscr, "Status: New download", 'info', 15, 2)
            
            # Errors
            if progress.get('errors', 0) > 0:
                self.colored_text(stdscr, f"Errors: {progress['errors']}", 'error', 16, 2)
            
            # Shutdown message
            if self.shutdown:
                self.colored_text(stdscr, "⚠️  Shutting down gracefully...", 'warning', 18, 2)
            
            # Footer
            self.colored_text(stdscr, "Press Ctrl+C to cancel", 'normal', 20, 2)
            
            stdscr.refresh()
            time.sleep(0.5)
        
        # Completion message
        if not self.shutdown and progress['total_downloaded'] >= file_size:
            stdscr.clear()
            self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'success', 5, 0)
            self.colored_text(stdscr, "║               DOWNLOAD COMPLETE!                  ║", 'success', 6, 0)
            self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'success', 7, 0)
            self.colored_text(stdscr, f"File saved to: {progress.get('filepath', 'unknown')}", 'normal', 9, 2)
            self.colored_text(stdscr, "Press any key to exit...", 'normal', 11, 2)
        elif self.shutdown:
            stdscr.clear()
            self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'warning', 5, 0)
            self.colored_text(stdscr, "║             DOWNLOAD INTERRUPTED!                 ║", 'warning', 6, 0)
            self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'warning', 7, 0)
            self.colored_text(stdscr, f"Temp files saved in: {progress.get('temp_dir', 'unknown')}", 'normal', 9, 2)
            self.colored_text(stdscr, "Download can be resumed later", 'normal', 10, 2)
            self.colored_text(stdscr, "Press any key to exit...", 'normal', 12, 2)
        
        stdscr.refresh()
        stdscr.getkey()

    def cleanup_temp_files(self, temp_dir, keep=False):
        """Clean up temporary directory"""
        if not keep and temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass  # Directory might be in use

    def main(self):
        parser = argparse.ArgumentParser(description="Enhanced download manager with colored TUI")
        parser.add_argument('--link', required=True, help="The URL to download from (HTTP/HTTPS).")
        parser.add_argument('--path', default='.', help="The directory to save the file to.")
        parser.add_argument('--resume', choices=['ask', 'yes', 'no'], default='ask', 
                          help="Resume behavior: ask (default), yes, or no")
        parser.add_argument('--user', help="Username for basic authentication.")
        parser.add_argument('--password', help="Password for basic authentication.")
        args = parser.parse_args()

        url = args.link
        download_dir = args.path
        filename = url.split('/')[-1] if '/' in url else 'download'
        filepath = os.path.join(download_dir, filename)
        user = args.user
        password = args.password

        # Check if directory exists
        os.makedirs(download_dir, exist_ok=True)

        try:
            # Get file size and check range support
            head_req = urllib.request.Request(url, method='HEAD')
            if user and password:
                credentials = f"{user}:{password}"
                encoded_credentials = base64.b64encode(credentials.encode()).decode()
                head_req.add_header('Authorization', f'Basic {encoded_credentials}')
            with urllib.request.urlopen(head_req) as response:
                accepts_ranges = response.headers.get('Accept-Ranges') == 'bytes'
                file_size = int(response.headers.get('Content-Length', 0))

            if file_size == 0:
                print("❌ Unable to determine file size. Aborting.")
                return

            if not accepts_ranges:
                print("⚠️  Server does not support range requests. Using single-threaded download.")
                num_threads = 1
                chunks = [(0, '')]
            else:
                num_threads = max(1, multiprocessing.cpu_count())
                chunk_size = file_size // num_threads
                chunks = [(i * chunk_size, (i + 1) * chunk_size - 1) for i in range(num_threads)]
                chunks[-1] = (chunks[-1][0], '')

            # Handle resume logic
            temp_dir, resume_sizes = self.check_existing_download(download_dir, filename, num_threads)
            total_resume_size = sum(resume_sizes)
            
            should_resume = False
            if args.resume == 'yes':
                should_resume = total_resume_size > 0
            elif args.resume == 'no':
                if temp_dir:
                    self.cleanup_temp_files(temp_dir)
                    resume_sizes = [0] * num_threads
                    total_resume_size = 0
            else:  # 'ask'
                if total_resume_size > 0:
                    should_resume = self.ask_resume_or_fresh(total_resume_size, filename)
                    if not should_resume:
                        self.cleanup_temp_files(temp_dir)
                        resume_sizes = [0] * num_threads
                        total_resume_size = 0

            # Create temp directory if needed
            if not temp_dir or not os.path.exists(temp_dir):
                temp_dir = self.get_temp_dir(download_dir, filename)
                os.makedirs(temp_dir, exist_ok=True)

            # Create temp file paths
            temp_files = [os.path.join(temp_dir, f"chunk_{i}") for i in range(num_threads)]

            # Progress tracking
            progress = {
                'total_downloaded': total_resume_size, 
                'start_time': time.time(), 
                'file_size': file_size,
                'threads': num_threads,
                'filepath': filepath,
                'temp_dir': temp_dir,
                'resumed': total_resume_size > 0
            }

            # Start download threads
            threads = []
            for i, (start, end) in enumerate(chunks):
                resume_size = resume_sizes[i]
                t = threading.Thread(target=self.download_chunk, args=(url, start, end, temp_files[i], progress, self.lock, resume_size, user, password))
                threads.append(t)
                t.daemon = True
                t.start()

            # Run TUI
            curses.wrapper(lambda stdscr: self.tui(stdscr, progress, file_size, filename))

            # Wait for threads to finish
            for t in threads:
                t.join(timeout=5)  # Timeout for graceful shutdown

            # Only merge if not shutdown and download completed
            if not self.shutdown and progress['total_downloaded'] >= file_size:
                print(f"\nMerging chunks...", end='', flush=True)
                with open(filepath, 'wb') as f:
                    for temp_file in temp_files:
                        if os.path.exists(temp_file):
                            with open(temp_file, 'rb') as tf:
                                f.write(tf.read())
                self.cleanup_temp_files(temp_dir)
                print(" ✅")
                print(f"✅ File successfully downloaded to: {filepath}")
            else:
                print(f"⚠️  Download interrupted. Temporary files saved in: {temp_dir}")
                print("You can resume the download later using the same command.")
                
        except Exception as e:
            print(f"❌ Error: {e}")
            sys.exit(1)

if __name__ == "__main__":
    manager = DownloadManager()
    manager.main()
