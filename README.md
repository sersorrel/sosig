# sosig: Slack-tO-diScord brIdGe

## Configuration

Currently you have to pass the path to the config file to sosig when you
run it (as in `sosig sosig.cfg`). The config file uses Python's
configparser format, and should look something like this:

```ini
[DiscordEndpoint]
token = ...

[SlackEndpoint]
token = ...
```

It's not yet possible to configure message routing in the config file.

## Copyright

Copyright © 2020 Ash Holland. Licensed under the EUPL (1.2 or later).
