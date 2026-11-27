#!/usr/bin/with-contenv bash
# linuxserver custom init: runs before SABnzbd starts, on every container start.
# Writes the .env values into /config/sabnzbd.ini (see render_sabnzbd_ini.py). Prints no secrets.
python3 /render/render_sabnzbd_ini.py /config/sabnzbd.ini || echo "render_sabnzbd_ini failed; SABnzbd starts with its existing settings"
exit 0
