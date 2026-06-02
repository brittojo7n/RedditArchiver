# RedditArchiver

This repository contains the source code for **RedditArchiver**, a high-performance, multithreaded command-line utility for archiving high-resolution images and videos from Reddit profiles without requiring an API key.

## Installation & Usage

To run the project, you need **Python 3.10+** installed on your system.

1. **Clone the repository.**
2. **Install the required dependencies** from your terminal or command prompt:

   ```sh
   pip install -r requirements.txt
   ```

3. **Run the script:**

   ```sh
   python reddit_archiver.py
   ```

4. The CLI will interactively ask for the target username and your active `reddit_session` cookie. The downloaded media will be automatically organized into a local `reddit_<username>_archive/` directory.

**NOTE:** The `reddit_session` cookie acts as a master key to your Reddit account and is required to natively bypass Reddit's 403 Forbidden firewall blocks. Treat this string exactly like a password. Never commit it to a public repository, share it online, or show it on a live stream.

<br>
<div align="center">
  <a href="https://britto.is-a.dev" target="_blank">
    <img src="https://img.shields.io/badge/Reddit%20Archiver-Made%20By%20Britto-3776AB.svg?style=for-the-badge&logo=python&logoColor=white" alt="RedditArchiver Project Badge" />
  </a>
</div>
