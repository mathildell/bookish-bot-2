git pull origin "$(git branch --show-current)"
.venv/bin/pip install -r requirements.txt # if dependencies changed
sudo systemctl daemon-reload
sudo systemctl restart bookish-hideaway
sudo systemctl status bookish-hideaway