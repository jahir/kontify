# kontify
Poll your bank accounts and get notifications for new transactions (while storing them in a database)

This is a (more or less) simple python script that retrieves statements for your bank accounts (via FinTS/HBCI 3.0), stores them in a database and notifies you of new ones.

Currently supported/required:
- python 3.x (tested with 3.6.5, but 3.3 onwards should be ok)
- *SQLite* for storing accounts and statements
- *Telegram* for notifications (if you want)

## Installation

- clone repo or download archive and unpack
- install python-FinTS _fints_ module (e.g. with `pip3 install`), requires _mt940_. install _yaml_ module.
- copy kontify.yaml.example to kontify.yaml and put in your banking data and access settings
- create the database with `sqlite3 kontify.sqlite < sqlite.txt`
- run `DEBUG=1 DUMMY=1 ./kontify.py 1` to see if it works
- run `./kontify.py 100` to fetch some data (most banks will only let you retrieve one or three months)
- add add cronjob (with `crontab -e`), e.g. `13 * * * * cd ~/kontify && ./kontify.py`
- by default, new statements are just printed out, so your cron daemon will send you an email
- if you want Telegram notifications, you need 
   - a bot token, see https://core.telegram.org/bots#6-botfather. Note that the _Username_ of you bot has to be globally unique, so use something specific like JakobsKontifyBot
   - your chat id. start a chat with your new bot and send it some random message talk (you can delete it afterwards). fetch the message with `curl -s  'https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates` (you can pipe it to something like _json_pp_ to get a nicer output) and find the json field named "id" in result, message.chat or message.from, where also your name should appear (otherwise somebody else sent your bot a message, but that's quite unlikely).
