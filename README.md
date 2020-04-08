# sosig: Slack-tO-diScord brIdGe

## Configuration

Currently you have to pass the path to the config file to sosig when you
run it (as in `sosig sosig.cfg`). The config file uses Python's
configparser format, and should look something like this:

```ini
[DiscordBridge]
token = ...

[SlackBridge]
token = ...
```

It's not yet possible to configure message routing in the config file.

## To do

- formalise the concept of "locations"
- move message routing configuration into the config file
- canonicalise received messages before enqueueing them
  - formatting
  - mentions
  - channel references
  - emoji
- decanonicalise messages before sending them
- consider whether it's possible to deduplicate some of the message
  ignoring logic
- support more kinds of messages
  - edits
  - deletions
  - files/attachments
- pre-commit hook for isort
  - blocked on isort 5 being released
- tests???

## Copyright

Copyright © 2020 Ash Holland. Licensed under the EUPL (1.2 or later).
