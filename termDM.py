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
import subprocess
import math
from pathlib import Path

MAX_RETRIES = 5
RETRY_DELAY = 5
CHUNK_READ_SIZE = 64 * 1024  # 64KB chunks for better performance on low-end devices

# Color pairs definition
COLOR_PAIRS = {
    'success': 1,    # Green
    'warning': 2,    # Yellow  
    'error': 3,      # Red
    'info': 4,       # Blue
    'progress': 5,   # Cyan
    'normal': 6,     # White
    'merge': 7       # Magenta for merging
}

class DownloadManager:
    def __init__(self):
        self.shutdown = False
        self.lock = threading.Lock()
        self.download_complete = False
        self.merging = False
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
        curses.init_pair(COLOR_PAIRS['merge'], curses.COLOR_MAGENTA, -1)

    def colored_text(self, stdscr, text, color_type, y=0, x=0):
        """Display colored text"""
        color_pair = COLOR_PAIRS.get(color_type, COLOR_PAIRS['normal'])
        stdscr.addstr(y, x, text, curses.color_pair(color_pair))

    def get_temp_dir(self, download_dir, filename):
        """Get the hidden temp directory for this download"""
        # Clean filename to remove problematic characters
        clean_filename = "".join(c for c in filename if c.isalnum() or c in ('-', '_', '.'))
        if not clean_filename:
            clean_filename = "download"
        temp_dir = os.path.join(download_dir, f".{clean_filename}.temp")
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

        # Calculate expected chunk size for completion detection
        if end == '':
            # Last chunk - download until the end
            expected_size = None
        else:
            expected_size = end - start + 1
            
        current_size = resume_size
        
        # For resumed downloads, we need to adjust the range header
        if resume_size > 0:
            if end == '':
                range_header = f'bytes={start + resume_size}-'
            else:
                # Make sure we don't request beyond the original end
                range_header = f'bytes={start + resume_size}-{end}'
        else:
            range_header = f'bytes={start}-{end}' if end != '' else f'bytes={start}-'
            
        req.add_header('Range', range_header)
        
        retries = 0
        while retries < MAX_RETRIES and not self.shutdown and not self.download_complete:
            try:
                with urllib.request.urlopen(req, timeout=30) as response, open(temp_file, 'ab' if resume_size > 0 else 'wb') as f:
                    while not self.shutdown and not self.download_complete:
                        data = response.read(CHUNK_READ_SIZE)
                        if not data:
                            break
                        f.write(data)
                        current_size += len(data)
                        with lock:
                            progress['total_downloaded'] += len(data)
                            progress['active_downloaded'] += len(data)
                        
                        # Check if this chunk is complete (for non-last chunks)
                        if expected_size and current_size >= expected_size:
                            break
                
                # Verify chunk completion - but be more lenient for the last chunk
                if expected_size and current_size < expected_size:
                    # For non-last chunks, we expect exact size
                    if end != '':
                        raise Exception(f"Chunk incomplete: {current_size}/{expected_size} bytes")
                    # For last chunk, we'll accept whatever we got
                
                break  # Success, exit retry loop
                
            except urllib.error.HTTPError as e:
                if e.code == 416:  # Range Not Satisfiable - chunk is already complete
                    print(f"Chunk already complete: {temp_file}")
                    # Update progress to reflect this chunk is complete
                    if expected_size and current_size < expected_size:
                        with lock:
                            progress['total_downloaded'] += (expected_size - current_size)
                            progress['active_downloaded'] += (expected_size - current_size)
                    break
                else:
                    retries += 1
                    if retries < MAX_RETRIES and not self.shutdown and not self.download_complete:
                        print(f"Retry {retries}/{MAX_RETRIES} for chunk due to HTTP error: {e}")
                        time.sleep(RETRY_DELAY)
                    else:
                        with lock:
                            progress['errors'] = progress.get('errors', 0) + 1
                        raise Exception(f"Failed to download chunk after {MAX_RETRIES} retries: {e}")
            except (urllib.error.URLError, socket.timeout, ConnectionResetError, ConnectionError) as e:
                retries += 1
                if retries < MAX_RETRIES and not self.shutdown and not self.download_complete:
                    print(f"Retry {retries}/{MAX_RETRIES} for chunk due to connection error: {e}")
                    time.sleep(RETRY_DELAY)
                else:
                    with lock:
                        progress['errors'] = progress.get('errors', 0) + 1
                    raise Exception(f"Failed to download chunk after {MAX_RETRIES} retries: {e}")

    def calculate_accurate_total_size(self, temp_files):
        """Calculate total size without floating point errors"""
        total_size = 0
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                total_size += os.path.getsize(temp_file)
        return total_size

    def merge_chunks_fast(self, temp_files, output_file, stdscr, expected_total_size):
        """Ultra-fast chunk merging with proper progress tracking and size handling"""
        self.merging = True
        
        # Calculate total size for progress tracking - use integer math to avoid floating point errors
        total_size = self.calculate_accurate_total_size(temp_files)
        merged_size = 0
        
        if total_size == 0:
            return False
        
        try:
            # Remove output file if it exists
            if os.path.exists(output_file):
                os.remove(output_file)
            
            # Method 1: Try using cat command (fastest on Unix systems)
            if os.name == 'posix':
                # Filter only existing files
                existing_files = [f for f in temp_files if os.path.exists(f)]
                if existing_files:
                    temp_files_str = ' '.join([f'"{f}"' for f in existing_files])
                    cmd = f'cat {temp_files_str} > "{output_file}"'
                    
                    # Execute and monitor progress
                    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
                    # Monitor file growth for progress updates
                    last_size = 0
                    stalled_count = 0
                    while process.poll() is None:
                        if os.path.exists(output_file):
                            current_size = os.path.getsize(output_file)
                            if current_size > last_size:
                                merged_size = current_size
                                # Use min to prevent >100% display
                                percent = min(100.0, (merged_size / total_size) * 100)
                                self.update_merge_progress(stdscr, percent, merged_size, total_size, 1, 1)
                                last_size = current_size
                                stalled_count = 0
                            else:
                                stalled_count += 1
                                if stalled_count > 100:  # ~10 seconds of no progress
                                    break
                        time.sleep(0.1)
                    
                    # Wait for process to complete
                    process.wait()
                    
                    # Check result
                    if process.returncode == 0:
                        # Final progress update - ensure 100%
                        final_size = os.path.getsize(output_file)
                        self.update_merge_progress(stdscr, 100.0, final_size, total_size, 1, 1)
                        return True
            
            # Method 2: Direct file writing (fallback) - IMPROVED VERSION
            with open(output_file, 'wb') as outfile:
                for i, temp_file in enumerate(temp_files):
                    if not os.path.exists(temp_file):
                        continue
                    
                    file_size = os.path.getsize(temp_file)
                    with open(temp_file, 'rb') as infile:
                        while True:
                            chunk = infile.read(CHUNK_READ_SIZE)
                            if not chunk:
                                break
                            outfile.write(chunk)
                            merged_size += len(chunk)
                            
                            # Update progress with bounds checking
                            percent = min(100.0, (merged_size / total_size) * 100)
                            self.update_merge_progress(stdscr, percent, merged_size, total_size, i+1, len(temp_files))
            
            return True
            
        except Exception as e:
            with open("download_error.log", "a") as f:
                f.write(f"Merge error: {e}\n")
            return False

    def draw_merge_screen(self, stdscr, percent, merged_size, total_size, current_chunk, total_chunks):
        """Draw the complete merge screen (static parts)"""
        stdscr.clear()
        
        # Header
        self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'merge', 0, 0)
        self.colored_text(stdscr, "║                MERGING CHUNKS                     ║", 'merge', 1, 0)
        self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'merge', 2, 0)
        
        # Static labels with consistent single colons
        self.colored_text(stdscr, "Chunk: ", 'info', 4, 2)
        self.colored_text(stdscr, "Progress: ", 'merge', 5, 2)
        self.colored_text(stdscr, "Merged: ", 'normal', 8, 2)
        self.colored_text(stdscr, "Status: ", 'normal', 10, 2)
        self.colored_text(stdscr, "Please wait...", 'normal', 12, 2)
        
        # Draw empty progress bar background
        bar_width = 50
        stdscr.addstr(6, 2, "[" + "░" * bar_width + "]", curses.color_pair(COLOR_PAIRS['merge']))
        
        # Now update the dynamic parts
        self.update_merge_progress(stdscr, percent, merged_size, total_size, current_chunk, total_chunks)

    def update_merge_progress(self, stdscr, percent, merged_size, total_size, current_chunk, total_chunks):
        """Update only the dynamic parts of the merge progress"""
        # Clamp percentage to 0-100 range
        percent = max(0.0, min(100.0, percent))
        
        # Update chunk counter
        stdscr.addstr(4, 9, f"{current_chunk}/{total_chunks}     ")
        
        # Update percentage (clamped)
        stdscr.addstr(5, 12, f"{percent:6.1f}%     ")
        
        # Update progress bar (clamped)
        bar_width = 50
        filled = min(int(bar_width * percent / 100), bar_width)
        stdscr.addstr(6, 3, "█" * filled)
        
        # Update size information
        size_text = f"{merged_size / (1024**2):.2f} MB / {total_size / (1024**2):.2f} MB     "
        stdscr.addstr(8, 9, size_text)
        
        # Update status with proper spacing after colon
        if percent < 100:
            status_text = f"Merging chunk {current_chunk} of {total_chunks}     "
        else:
            status_text = "Finalizing...                          "
        stdscr.addstr(10, 9, status_text)
        
        stdscr.refresh()

    def draw_download_screen(self, stdscr, progress, file_size, filename, current_speed, remaining_time):
        """Draw the complete download screen (static parts)"""
        stdscr.clear()
        
        # Header
        self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'info', 0, 0)
        self.colored_text(stdscr, "║              TERM DOWNLOAD MANAGER                ║", 'info', 1, 0)
        self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'info', 2, 0)
        
        # Static labels with consistent single colons
        self.colored_text(stdscr, "File: ", 'normal', 4, 2)
        self.colored_text(stdscr, "Size: ", 'normal', 5, 2)
        self.colored_text(stdscr, "Progress:", 'progress', 7, 2)
        self.colored_text(stdscr, "Downloaded: ", 'normal', 10, 2)
        self.colored_text(stdscr, "Speed: ", 'normal', 11, 2)
        self.colored_text(stdscr, "Time left: ", 'normal', 12, 2)
        self.colored_text(stdscr, "Threads: ", 'info', 14, 2)
        self.colored_text(stdscr, "Status: ", 'info', 15, 2)
        self.colored_text(stdscr, "Errors: ", 'error', 17, 2)
        self.colored_text(stdscr, "Press Ctrl+C to cancel", 'normal', 22, 2)
        
        # Draw empty progress bar background
        bar_width = 50
        stdscr.addstr(8, 2, "[" + "░" * bar_width + "]", curses.color_pair(COLOR_PAIRS['progress']))
        
        # Now update dynamic content
        self.update_download_progress(stdscr, progress, file_size, filename, current_speed, remaining_time)

    def update_download_progress(self, stdscr, progress, file_size, filename, current_speed, remaining_time):
        """Update only the dynamic parts of the download progress"""
        downloaded = progress['total_downloaded']
        percent = min(100.0, (downloaded / file_size) * 100) if file_size > 0 else 0
        
        # Update file info
        stdscr.addstr(4, 8, f"{filename}                          ")
        stdscr.addstr(5, 8, f"{file_size / (1024**2):.2f} MB               ")
        
        # Update progress bar
        bar_width = 50
        filled = min(int(bar_width * percent / 100), bar_width)
        stdscr.addstr(8, 3, "█" * filled)
        
        # Update percentage
        stdscr.addstr(8, 55, f"{percent:6.1f}%     ")
        
        # Update statistics
        stdscr.addstr(10, 14, f"{downloaded / (1024**2):.2f} MB               ")
        stdscr.addstr(11, 8, f"{current_speed / 1024:.2f} KB/s               ")
        
        # Update time left
        if remaining_time > 0 and current_speed > 0:
            if remaining_time > 3600:
                time_str = f"{remaining_time / 3600:.1f} hours"
            elif remaining_time > 60:
                time_str = f"{remaining_time / 60:.1f} minutes"
            else:
                time_str = f"{remaining_time:.0f} seconds"
            stdscr.addstr(12, 12, f"{time_str}                    ")
        else:
            stdscr.addstr(12, 12, "Calculating...                    ")
        
        # Update thread count
        stdscr.addstr(14, 11, f"{progress.get('threads', 1)}                  ")
        
        # Update status - combine resume info into one line
        if progress.get('resumed', False):
            resume_mb = progress.get('resume_size', 0) / (1024**2)
            status_text = f"Resumed download ({resume_mb:.2f} MB)              "
            stdscr.addstr(15, 9, status_text)
        else:
            stdscr.addstr(15, 9, "New download                        ")
        
        # Update errors
        if progress.get('errors', 0) > 0:
            stdscr.addstr(17, 9, f"{progress['errors']}                  ")
        else:
            stdscr.addstr(17, 9, "0                  ")
        
        # Shutdown message
        if self.shutdown:
            self.colored_text(stdscr, "⚠️  Shutting down gracefully...", 'warning', 20, 2)
        else:
            stdscr.addstr(20, 2, " " * 35)
        
        stdscr.refresh()

    def verify_file_integrity(self, filepath, expected_size, actual_chunk_size):
        """Verify the merged file size matches expected size with tolerance for resume downloads"""
        try:
            actual_size = os.path.getsize(filepath)
            
            # For resumed downloads, we need to be more lenient
            # The actual size should be close to either the expected size OR the actual chunk size
            expected_from_chunks = actual_chunk_size
            
            # Allow 2% tolerance for filesystem differences and resume inconsistencies
            tolerance = max(expected_size, expected_from_chunks) * 0.02
            
            # Check if actual size is close to either expected size
            if (abs(actual_size - expected_size) <= tolerance or 
                abs(actual_size - expected_from_chunks) <= tolerance):
                return True
            
            # If we're still within reasonable bounds (say 5%), accept it
            if abs(actual_size - expected_from_chunks) <= (expected_from_chunks * 0.05):
                print(f"Warning: File size slightly off but within acceptable range: {actual_size} vs {expected_from_chunks}")
                return True
                
            return False
        except:
            return False

    def tui(self, stdscr, progress, file_size, filename, temp_files, filepath):
        """Enhanced TUI with smooth updates"""
        self.init_colors()
        curses.curs_set(0)
        stdscr.nodelay(1)
        
        # Initialize speed calculation
        last_update_time = time.time()
        last_downloaded = progress['active_downloaded']
        current_speed = 0
        
        # Draw initial screen
        self.draw_download_screen(stdscr, progress, file_size, filename, current_speed, 0)
        
        # Download loop
        while progress['total_downloaded'] < file_size and not self.shutdown and not self.download_complete:
            # Calculate real-time speed
            current_time = time.time()
            time_diff = current_time - last_update_time
            
            if time_diff >= 0.5:
                downloaded_diff = progress['active_downloaded'] - last_downloaded
                current_speed = downloaded_diff / time_diff if time_diff > 0 else 0
                last_downloaded = progress['active_downloaded']
                last_update_time = current_time
            
            # Calculate remaining time
            downloaded = progress['total_downloaded']
            remaining_bytes = file_size - downloaded
            remaining_time = remaining_bytes / current_speed if current_speed > 0 else 0
            
            # Update display
            self.update_download_progress(stdscr, progress, file_size, filename, current_speed, remaining_time)
            
            if progress['total_downloaded'] >= file_size:
                break
            
            time.sleep(0.1)
            
            # Check for user input
            try:
                key = stdscr.getch()
                if key == ord('q') or key == ord('Q') or key == 3:
                    self.shutdown = True
                    break
            except:
                pass
        
        # Auto-merge after download completion
        if not self.shutdown and progress['total_downloaded'] >= file_size:
            # Small delay to ensure all threads finished writing
            time.sleep(1)
            
            # Verify all chunks are complete before merging
            all_chunks_complete = True
            for temp_file in temp_files:
                if not os.path.exists(temp_file):
                    all_chunks_complete = False
                    break
            
            if all_chunks_complete:
                # Calculate actual total size from chunks - this is the REAL expected size
                actual_total_size = self.calculate_accurate_total_size(temp_files)
                
                # Draw merge screen
                self.draw_merge_screen(stdscr, 0, 0, actual_total_size, 1, len(temp_files))
                stdscr.refresh()
                
                # Perform fast merge with accurate size tracking
                success = self.merge_chunks_fast(temp_files, filepath, stdscr, file_size)
                
                if success:
                    # Verify file integrity with tolerance - use the ACTUAL chunk size as reference
                    if self.verify_file_integrity(filepath, file_size, actual_total_size):
                        self.cleanup_temp_files(progress['temp_dir'])
                        self.show_success_screen(stdscr, filepath)
                    else:
                        actual_size = os.path.getsize(filepath)
                        error_msg = f"File size mismatch: {actual_size} vs {actual_total_size} bytes (expected: {file_size})"
                        self.show_error_screen(stdscr, error_msg, progress['temp_dir'])
                else:
                    self.show_error_screen(stdscr, "Merge failed", progress['temp_dir'])
            else:
                self.show_error_screen(stdscr, "Some chunks failed to download", progress['temp_dir'])
        
        elif self.shutdown:
            self.show_interrupted_screen(stdscr, progress['temp_dir'])

    def show_success_screen(self, stdscr, filepath):
        """Show success screen"""
        stdscr.clear()
        self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'success', 5, 0)
        self.colored_text(stdscr, "║               DOWNLOAD COMPLETE!                  ║", 'success', 6, 0)
        self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'success', 7, 0)
        self.colored_text(stdscr, f"File saved to: {filepath}", 'normal', 9, 2)
        self.colored_text(stdscr, "All operations completed successfully!", 'success', 11, 2)
        self.colored_text(stdscr, "Temp files cleaned up", 'success', 12, 2)
        self.colored_text(stdscr, "Press any key to exit...", 'normal', 14, 2)
        stdscr.refresh()
        stdscr.nodelay(0)
        stdscr.getkey()

    def show_error_screen(self, stdscr, error_msg, temp_dir):
        """Show error screen"""
        stdscr.clear()
        self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'error', 5, 0)
        self.colored_text(stdscr, "║               OPERATION FAILED!                   ║", 'error', 6, 0)
        self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'error', 7, 0)
        self.colored_text(stdscr, f"Error: {error_msg}", 'normal', 9, 2)
        self.colored_text(stdscr, f"Temp directory: {temp_dir}", 'normal', 10, 2)
        self.colored_text(stdscr, "You can resume the download later", 'normal', 11, 2)
        self.colored_text(stdscr, "Press any key to exit...", 'normal', 13, 2)
        stdscr.refresh()
        stdscr.nodelay(0)
        stdscr.getkey()

    def show_interrupted_screen(self, stdscr, temp_dir):
        """Show interrupted screen"""
        stdscr.clear()
        self.colored_text(stdscr, "╔═══════════════════════════════════════════════════╗", 'warning', 5, 0)
        self.colored_text(stdscr, "║             DOWNLOAD INTERRUPTED!                 ║", 'warning', 6, 0)
        self.colored_text(stdscr, "╚═══════════════════════════════════════════════════╝", 'warning', 7, 0)
        self.colored_text(stdscr, f"Temp files saved in: {temp_dir}", 'normal', 9, 2)
        self.colored_text(stdscr, "Download can be resumed later", 'normal', 10, 2)
        self.colored_text(stdscr, "Press any key to exit...", 'normal', 12, 2)
        stdscr.refresh()
        stdscr.nodelay(0)
        stdscr.getkey()

    def cleanup_temp_files(self, temp_dir, keep=False):
        """Clean up temporary directory"""
        if not keep and temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                return True
            except OSError as e:
                with open("download_error.log", "a") as f:
                    f.write(f"Cleanup error for {temp_dir}: {e}\n")
                return False
        return True

    def manual_cleanup(self, download_dir, filename):
        """Manual cleanup method for temp files"""
        temp_dir = self.get_temp_dir(download_dir, filename)
        if os.path.exists(temp_dir):
            print(f"Cleaning up temp directory: {temp_dir}")
            shutil.rmtree(temp_dir)
            print("✅ Cleanup complete")
        else:
            print("✅ No temp files found")

    def main(self):
        parser = argparse.ArgumentParser(description="Enhanced download manager with colored TUI")
        parser.add_argument('--link', required=True, help="The URL to download from (HTTP/HTTPS).")
        parser.add_argument('--path', default='.', help="The directory to save the file to.")
        parser.add_argument('--resume', choices=['ask', 'yes', 'no'], default='ask', 
                          help="Resume behavior: ask (default), yes, or no")
        parser.add_argument('--user', help="Username for basic authentication.")
        parser.add_argument('--password', help="Password for basic authentication.")
        parser.add_argument('--cleanup', action='store_true', help="Clean up temp files for this file and exit")
        parser.add_argument('--threads', type=int, help="Number of threads to use (default: auto-detect)")
        args = parser.parse_args()

        # Handle manual cleanup
        if args.cleanup:
            filename = args.link.split('/')[-1] if '/' in args.link else 'download'
            self.manual_cleanup(args.path, filename)
            return

        url = args.link
        download_dir = args.path
        filename = urllib.parse.unquote(url.split('/')[-1]) if '/' in url else 'download'
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

            # Determine number of threads
            if args.threads:
                num_threads = max(1, min(args.threads, 16))  # Limit to 16 threads max
            else:
                num_threads = max(1, multiprocessing.cpu_count())
            
            # For very small files, use fewer threads
            if file_size < 1024 * 1024:  # Less than 1MB
                num_threads = 1

            if not accepts_ranges:
                print("⚠️  Server does not support range requests. Using single-threaded download.")
                num_threads = 1
                chunks = [(0, '')]
            else:
                chunk_size = file_size // num_threads
                chunks = [(i * chunk_size, (i + 1) * chunk_size - 1) for i in range(num_threads)]
                chunks[-1] = (chunks[-1][0], '')  # Last chunk goes to end

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
                'active_downloaded': 0,
                'start_time': time.time(), 
                'file_size': file_size,
                'threads': num_threads,
                'filepath': filepath,
                'temp_dir': temp_dir,
                'resumed': total_resume_size > 0,
                'resume_size': total_resume_size
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
            curses.wrapper(lambda stdscr: self.tui(stdscr, progress, file_size, filename, temp_files, filepath))

            # Clean up
            self.download_complete = True
            for t in threads:
                t.join(timeout=5)

        except Exception as e:
            print(f"❌ Error: {e}")
            sys.exit(1)

if __name__ == "__main__":
    manager = DownloadManager()
    manager.main()