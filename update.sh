git pull origin "$(git branch --show-current)"
.venv/bin/pip install -r requirements.txt # if dependencies changed
# .venv/bin/playwright install chromium # browser for the book release scraper
# sudo .venv/bin/playwright install-deps chromium # OS libraries that browser needs
sudo systemctl daemon-reload
sudo systemctl restart bookish-hideaway
sudo systemctl status bookish-hideaway