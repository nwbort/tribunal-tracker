#!/bin/bash
set -e

# Cloudflare protects this site with a "managed challenge", so a plain curl
# only ever captures the "Just a moment..." interstitial. Drive a real Chrome
# (headful, under Xvfb) via nodriver to solve the challenge and grab the real
# pages. xvfb-run gives Chrome a display so it runs headful, which is far less
# likely to be flagged than headless.
#
# The scraper refreshes current-matters.json and, for each matter listed in
# matters.txt, matters/<slug>/documents.json and matters/<slug>/documents/.
# Changes are pushed to ntfy.sh when NTFY_TOPIC is set.
xvfb-run -a python scrape.py
