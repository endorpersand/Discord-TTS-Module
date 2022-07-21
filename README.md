# Discord TTS Module

This is a bot (written using discord.py) that allows users chatting in text channels to output their messages to their voice channel.

This project was mainly developed from April 2021-June 2022.

**Note:** This project is different from Discord's built-in TTS, that outputs messages to Discord users' speakers.

## Usage

Install dependencies with:

```sh
python3 -m pip install -r requirements.txt
```

Then, start up the bot with

```sh
python3 bot.py
```

Theoretically, you should be able to plop `tts.py` and `helpers/` into a bot and have it work.

## Demonstrations

Basic functionality:

![Demonstrating basic functionality](./.github/assets/d1_basics.mp4)

The bot can also follow you through voice channels:

![Demonstrating VC following](./.github/assets/d2_follow.mp4)

The bot can also have custom voices applied:

![Demonstrating custom voices](./.github/assets/d3_voices.mp4)

Or even custom voice effects:

![Demonstrating custom effects](./.github/assets/d4_effects.mp4)
