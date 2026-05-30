#!/bin/bash
python -m playwright install chromium
python -m playwright install-deps chromium
echo "Playwright Chromium installed successfully"
