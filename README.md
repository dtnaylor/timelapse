# Timelapse

Tools for doing time lapse with GoPro.

## Importing Photos

To download photos from a GoPro without removing the SD card (e.g., because
removing the SD card would disturb its positioning), the `download_gopro.py`
TUI script downloads all un-imported photos from the GoPro's internal web
server.

1. Induce the GoPro to enable its internal web server by accessing one of the
   WiFi-based features in the Quik app (e.g., "View Media" or "Live Preview.")

2. Connect to the GoPro's WiFi network from your computer.

3. Run the downloader:

   ```bash
   ./download_gopro.py
   ```
